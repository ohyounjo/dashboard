// web/shared/query.js의 순수 함수(node --test): 정렬 순환, 필터 → 요청, 커서 쌓기, 고치기 폼의 바뀐 열.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  nextSort, filterOps, needsValue, buildFilters, filterErrorIndex, buildQuery,
  pagerStart, pagerCursor, pagerIndex, pagerNext, pagerPrev, truncatedSet, valueToInput, changedColumns, editRow, pageRange,
} from "../../web/shared/query.js";

const COLS = [
  { name: "id", type: "text", primary_key: true },
  { name: "state", type: "enum", values: ["open", "done"] },
  { name: "amount", type: "number" },
  { name: "qty", type: "integer" },
  { name: "on_day", type: "date" },
  { name: "flag", type: "boolean" },
  { name: "memo", type: "text" },
];

test("정렬: 없음 → 오름 → 내림 → 없음, 다른 열은 오름부터", () => {
  let s = nextSort(null, "qty");
  assert.deepEqual(s, { column: "qty", dir: "asc" });
  s = nextSort(s, "qty");
  assert.deepEqual(s, { column: "qty", dir: "desc" });
  assert.equal(nextSort(s, "qty"), null);
  assert.deepEqual(nextSort({ column: "qty", dir: "desc" }, "memo"), { column: "memo", dir: "asc" });
});

test("유형별 연산: 글은 포함·같음·비어 있음, 숫자·날짜는 같음·이상·이하, 목록은 고르기", () => {
  assert.deepEqual(filterOps(COLS[6]), ["contains", "eq", "is_null"]);
  assert.deepEqual(filterOps(COLS[2]), ["eq", "ge", "le", "is_null"]);
  assert.deepEqual(filterOps(COLS[4]), ["eq", "ge", "le", "is_null"]);
  assert.deepEqual(filterOps({ name: "t", type: "timestamp" }), ["eq", "ge", "le", "is_null"]);
  assert.deepEqual(filterOps(COLS[1]), ["in", "is_null"]);
  assert.deepEqual(filterOps(COLS[5]), ["eq", "is_null"]);
  assert.ok(!needsValue("is_null") && needsValue("eq"));
});

test("필터 → 요청: 빈 값은 빼고, 값은 서버 표기로 다듬고, 보낸 자리를 기억한다", () => {
  const { filters, rows } = buildFilters([
    { column: "memo", op: "contains", value: " 메모 " },
    { column: "qty", op: "ge", value: "" },               // 값이 없어 빠진다
    { column: "amount", op: "le", value: "1,200" },
    { column: "on_day", op: "eq", value: "2026/11/20" },
    { column: "state", op: "in", value: "done" },
    { column: "flag", op: "is_null", value: "" },
    { column: "nope", op: "eq", value: "x" },             // 없는 열
  ], COLS);
  assert.deepEqual(filters, [
    { column: "memo", op: "contains", value: "메모" },
    { column: "amount", op: "le", value: "1200" },
    { column: "on_day", op: "eq", value: "2026-11-20" },
    { column: "state", op: "in", value: ["done"] },
    { column: "flag", op: "is_null" },
  ]);
  assert.deepEqual(rows, [0, 2, 3, 4, 5]);
  assert.equal(filterErrorIndex("filters[3]: '1x'는 number 값이 아닙니다"), 3);
  assert.equal(filterErrorIndex("cursor가 다릅니다"), null);
});

test("요청 본문", () => {
  assert.deepEqual(buildQuery({ filters: [], sort: { column: "qty", dir: "desc" }, cursor: "c1" }),
    { filters: [], sort: [{ column: "qty", dir: "desc" }], limit: 50, cursor: "c1" });
  assert.deepEqual(buildQuery({ count: true }), { filters: [], sort: [], limit: 50, cursor: null, count: true });
});

test("커서 쌓기: 다음은 쌓고 이전은 되돌아간다", () => {
  let p = pagerStart();
  assert.equal(pagerCursor(p), null);
  assert.equal(pagerIndex(p), 0);
  p = pagerNext(p, "a");
  p = pagerNext(p, "b");
  assert.equal(pagerCursor(p), "b");
  assert.equal(pagerIndex(p), 2);
  assert.deepEqual(pageRange(p, 50, 10), { from: 101, to: 110 });
  p = pagerPrev(p);
  assert.equal(pagerCursor(p), "a");
  p = pagerPrev(pagerPrev(p));
  assert.equal(pagerCursor(p), null);
  assert.equal(pagerNext(p, null), p, "다음 커서가 없으면 그대로");
  assert.deepEqual(pageRange(pagerStart(), 50, 0), { from: 0, to: 0 });
});

test("잘린 칸 찾기", () => {
  const cut = truncatedSet([[0, "memo"], [3, "memo"]]);
  assert.ok(cut.has("0:memo") && cut.has("3:memo") && !cut.has("1:memo"));
});

test("고치기 폼: 같은 뜻의 다른 표기는 바뀐 것이 아니고, 기본 키·잠근 열은 보지 않는다", () => {
  const initial = { id: "E1", state: "open", amount: 1200, qty: 2, on_day: "2026-11-20", flag: true, memo: null };
  const same = { id: "E1", state: "open", amount: "1,200", qty: "2", on_day: "2026/11/20", flag: "true", memo: "" };
  assert.deepEqual(changedColumns(COLS, initial, same), []);
  const edited = { ...same, id: "E9", state: "done", memo: "새 메모", qty: "" };
  assert.deepEqual(changedColumns(COLS, initial, edited), ["state", "qty", "memo"]);
  assert.deepEqual(changedColumns(COLS, initial, edited, { locked: ["memo"] }), ["state", "qty"]);
  // 저장: 기본 키(처음 값)와 바뀐 열만. 비운 칸은 null
  assert.deepEqual(editRow(COLS, initial, edited, ["state", "qty", "memo"]), { id: "E1", state: "done", qty: null, memo: "새 메모" });
  assert.equal(valueToInput(false), "false");
  assert.equal(valueToInput(null), "");
  assert.equal(valueToInput(20.5), "20.5");
});
