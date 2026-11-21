// web/shared/structure.js의 순수 함수(node --test): 이름 검사, 유형 넓히기, 열·데이터셋 연산, 새 테이블 정의 정리.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NAME_RE, widenTargets, splitValues, columnAddOp, columnUpdateOp, datasetAddOp, tableForCreate, renameInKey, usageByDataset,
} from "../../web/shared/structure.js";

const codes = (made) => made.problems.map((p) => `${p.field}:${p.code}`);

test("이름: 소문자로 시작하는 snake_case, 63자까지", () => {
  for (const ok of ["a", "col_1", "x".repeat(63)]) assert.ok(NAME_RE.test(ok), ok);
  for (const bad of ["", "1a", "A", "a-b", "a b", "가", "x".repeat(64), "_a"]) assert.ok(!NAME_RE.test(bad), bad);
});

test("유형 넓히기 후보(서버 _WIDER와 같다)", () => {
  assert.deepEqual(widenTargets("integer"), ["number", "text"]);
  assert.deepEqual(widenTargets("number"), ["text"]);
  assert.deepEqual(widenTargets("enum"), ["text"]);
  assert.deepEqual(widenTargets("text"), []);
  assert.deepEqual(splitValues(" a, b ,a\nc,, "), ["a", "b", "c"]);
});

test("열 추가: 필수면 기본값이 있어야 하고, 기본값은 그 유형의 값으로 적는다", () => {
  assert.deepEqual(codes(columnAddOp("t", { name: "note", type: "text", required: true }, [])), ["default:defaultRequired"]);
  assert.deepEqual(codes(columnAddOp("t", { name: "id", type: "text" }, ["id"])), ["name:exists"]);
  assert.deepEqual(codes(columnAddOp("t", { name: "Bad", type: "text" })), ["name:name"]);
  assert.deepEqual(codes(columnAddOp("t", { name: "n", type: "integer", default: "1.5" })), ["default:integer"]);
  assert.deepEqual(codes(columnAddOp("t", { name: "k", type: "enum", values: "" })), ["values:values"]);
  assert.deepEqual(codes(columnAddOp("t", { name: "k", type: "enum", values: "a,b", default: "c" })), ["default:enum"]);
  assert.deepEqual(columnAddOp("t", { name: "qty2", type: "integer", required: true, default: "1,000", description: " 개수 " }).op,
    { op: "column.add", table: "t", column: "qty2", value: { type: "integer", required: true, default: 1000, description: "개수" } });
  assert.deepEqual(columnAddOp("t", { name: "ok", type: "boolean", default: "Y" }).op.value, { type: "boolean", default: true });
  assert.deepEqual(columnAddOp("t", { name: "d", type: "date", default: "2026/1/5" }).op.value, { type: "date", default: "2026-01-05" });
  assert.deepEqual(columnAddOp("t", { name: "k", type: "enum", values: "a, b" }).op.value, { type: "enum", values: ["a", "b"] });
});

test("열 규칙 고치기: 바뀐 키만, 비우면 null, 유형은 넓히기만", () => {
  const col = { name: "qty", type: "integer", min: 0, description: "개수" };
  assert.equal(columnUpdateOp("t", col, { description: "개수", min: "0", max: "" }).op, null, "바뀐 것이 없다");
  assert.deepEqual(columnUpdateOp("t", col, { description: "", min: "1", max: "10" }).op,
    { op: "column.update", table: "t", column: "qty", set: { description: null, min: 1, max: 10 } });
  const widened = columnUpdateOp("t", col, { description: "개수", min: "0", type: "number" });
  assert.deepEqual(widened.op.set, { type: "number" });
  assert.equal(widened.typeChanged, true);
  assert.deepEqual(codes(columnUpdateOp("t", { name: "x", type: "text" }, { type: "integer" })), ["type:widen"]);
  assert.deepEqual(codes(columnUpdateOp("t", col, { min: "abc" })), ["min:integer"]);
  const e = { name: "s", type: "enum", values: ["a", "b"] };
  assert.deepEqual(columnUpdateOp("t", e, { values: "a, b, c" }).op.set, { values: ["a", "b", "c"] });
  assert.equal(columnUpdateOp("t", e, { values: "a,b" }).op, null);
  assert.deepEqual(codes(columnUpdateOp("t", e, { values: " , " })), ["values:values"]);
  // enum → text: enum이 아닌 열은 values를 가질 수 없으니 함께 지운다(값 칸을 비워 둬도 막지 않는다)
  assert.deepEqual(columnUpdateOp("t", e, { values: "a, b", type: "text" }).op.set, { values: null, type: "text" });
  assert.deepEqual(columnUpdateOp("t", e, { values: "", type: "text" }).op.set, { values: null, type: "text" });
  // 경계가 있는 열을 text로: min·max를 함께 지운다(number로 넓힐 때는 그대로)
  const bounded = { name: "n", type: "integer", min: 0, max: 100 };
  assert.deepEqual(columnUpdateOp("t", bounded, { min: "0", max: "100", type: "text" }).op.set, { type: "text", min: null, max: null });
  assert.deepEqual(columnUpdateOp("t", bounded, { min: "0", max: "100", type: "number" }).op.set, { type: "number" });
  // 배정밀도로 정확히 나타나지 않는 경계는 반올림해 저장하지 않고 거절한다
  assert.deepEqual(codes(columnUpdateOp("t", bounded, { min: "9007199254740993", max: "100" })), ["min:precision"]);
  assert.deepEqual(columnUpdateOp("t", bounded, { min: "9007199254740991", max: "100" }).op.set, { min: 9007199254740991 });
  assert.deepEqual(codes(columnUpdateOp("t", { name: "x", type: "number" }, { max: "0.12345678901234567" })), ["max:precision"]);
  assert.deepEqual(columnUpdateOp("t", { name: "x", type: "number" }, { max: "0.5" }).op.set, { max: 0.5 });
  assert.deepEqual(columnUpdateOp("t", { name: "p", type: "text", pattern: "^A" }, { pattern: "" }).op.set, { pattern: null });
  assert.deepEqual(columnUpdateOp("t", { name: "d", type: "date" }, { min: "2026/01/01" }).op.set, { min: "2026-01-01" });
});

test("데이터셋 만들기: 키와 (테이블 | SQL) 하나", () => {
  assert.deepEqual(datasetAddOp({ key: "v1", kind: "table", table: "t" }).op, { op: "dataset.add", key: "v1", value: { table: "t" } });
  assert.deepEqual(datasetAddOp({ key: "v2", kind: "sql", sql: " SELECT 1 ", description: "d" }).op,
    { op: "dataset.add", key: "v2", value: { sql: "SELECT 1", description: "d" } });
  assert.deepEqual(codes(datasetAddOp({ key: "v1", kind: "table", table: "" }, ["v1"])), ["key:exists", "table:table"]);
  assert.deepEqual(codes(datasetAddOp({ key: "", kind: "sql", sql: "" })), ["key:name", "sql:sql"]);
});

test("새 테이블: 표시용 키는 빼고 서버가 받는 키만, 기본 키는 있는 열만", () => {
  const inferred = {
    name: "items", rows: 3, label: "Items", notes: ["…"], primary_key: ["code"], primary_key_basis: "…",
    columns: [
      { name: "code", source: "Code", type: "text", required: true, basis: "…", confidence: "high", stats: { rows: 3 } },
      { name: "kind", source: "Kind", type: "enum", values: ["a", "b"], required: false, ref: null, notes: [] },
      { name: "parent_id", source: "parent_id", type: "text", ref: "parents.id", ref_basis: "…" },
    ],
  };
  const { table, problems } = tableForCreate(inferred);
  assert.deepEqual(problems, []);
  assert.deepEqual(table, { name: "items", primary_key: ["code"], columns: [
    { name: "code", type: "text", source: "Code", required: true },
    { name: "kind", type: "enum", source: "Kind", values: ["a", "b"] },
    { name: "parent_id", type: "text", source: "parent_id", ref: "parents.id" },
  ] });
  const renamed = { ...inferred, primary_key: renameInKey(inferred.primary_key, "code", "item_code"),
    columns: inferred.columns.map((c) => (c.name === "code" ? { ...c, name: "item_code" } : c)) };
  assert.deepEqual(tableForCreate(renamed).table.primary_key, ["item_code"]);
  const broken = { name: "Bad", primary_key: [], columns: [{ name: "a", type: "text" }, { name: "a", type: "nope" }, { name: "e", type: "enum", values: [] }] };
  assert.deepEqual(codes(tableForCreate(broken)), ["name:name", "column:duplicate", "type:type", "values:values", "primary_key:primaryKey"]);
});

test("쓰이는 곳을 데이터셋마다 묶는다", () => {
  const used = {
    table: "t", datasets: ["d1", "d2"],
    charts: [{ key: "c1", title: "C1", dashboard: "db", dataset: "d1", tabs: ["x"] }],
    sections: [{ tab: "y", index: 0, type: "kanban", dataset: "d2" }], tabs: ["x", "y"], unknown_datasets: [],
  };
  assert.deepEqual(usageByDataset(used), [
    { dataset: "d1", charts: [{ key: "c1", title: "C1", dashboard: "db", tabs: ["x"] }], sections: [] },
    { dataset: "d2", charts: [], sections: [{ tab: "y", index: 0, type: "kanban" }] },
  ]);
  assert.deepEqual(usageByDataset(null), []);
});
