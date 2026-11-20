// web/shared/fields.js의 순수 함수(node --test). make test-unit이 node 컨테이너에서 돌린다.
// 값 다듬기·형식 검사는 서버(dashkit/ingest/normalize.py, dashkit/loader/validate.py)와 같은 표기를 받아야 한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { TYPES, tidy, checkValue, parseTsv, matchHeader, looksLikeHeader, splitErrors, rowFromValues, display, isRequired, initialValue }
  from "../../web/shared/fields.js";

const col = (type, extra = {}) => ({ name: "c", type, required: false, primary_key: false, ...extra });
const code = (column, raw) => (checkValue(column, raw) || {}).code || null;

test("등록 표에 계약의 유형이 모두 있다", () => {
  assert.deepEqual(Object.keys(TYPES).sort(), ["boolean", "date", "enum", "integer", "number", "text", "timestamp"]);
});

test("값 다듬기는 서버와 같은 표기만 바꾼다", () => {
  assert.equal(tidy(col("text"), "  a b  "), "a b");
  assert.equal(tidy(col("date"), "2026/11/20"), "2026-11-20");
  assert.equal(tidy(col("date"), "2026.1.5"), "2026-01-05");
  assert.equal(tidy(col("date"), "20261120"), "2026-11-20");
  assert.equal(tidy(col("date"), "11/20/2026"), "11/20/2026", "순서가 모호한 날짜는 그대로 둔다");
  assert.equal(tidy(col("integer"), "1,234,567"), "1234567");
  assert.equal(tidy(col("number"), "-1,234.5"), "-1234.5");
  assert.equal(tidy(col("integer"), "12,34"), "12,34", "천 단위가 아닌 쉼표는 그대로");
  assert.equal(tidy(col("boolean"), "Y"), "true");
  assert.equal(tidy(col("boolean"), "no"), "false");
  assert.equal(tidy(col("boolean"), "0"), "false");
  assert.equal(tidy(col("timestamp"), "2026/11/20 09:30"), "2026-11-20T09:30");
  assert.equal(tidy(col("timestamp"), "2026-11-20   09:30:00+09:00"), "2026-11-20T09:30:00+09:00");
  assert.equal(tidy(col("integer"), null), "");
});

test("형식 검사: 유형", () => {
  assert.equal(code(col("integer"), "12"), null);
  assert.equal(code(col("integer"), "1,200"), null);
  assert.equal(code(col("integer"), "1.5"), "integer");
  assert.equal(code(col("integer"), "9223372036854775808"), "integerRange");
  assert.equal(code(col("number"), "1e3"), null);
  assert.equal(code(col("number"), "abc"), "number");
  assert.equal(code(col("boolean"), "yes"), null);
  assert.equal(code(col("boolean"), "maybe"), "boolean");
  assert.equal(code(col("date"), "2026-02-29"), "noSuchDate");
  assert.equal(code(col("date"), "2028-02-29"), null);
  assert.equal(code(col("date"), "11/20/2026"), "date");
  assert.equal(code(col("timestamp"), "2026-11-20"), null);
  assert.equal(code(col("timestamp"), "2026-11-20T25:00"), "timestamp");
  assert.equal(code(col("timestamp"), "2026-11-20 09:30:15.123Z"), null);
  assert.equal(code(col("timestamp"), "어제"), "timestamp");
});

test("형식 검사: 필수·목록·범위·pattern", () => {
  assert.equal(code(col("text", { required: true }), "  "), "required");
  assert.equal(code(col("text", { primary_key: true }), ""), "required");
  assert.equal(code(col("text"), ""), null);
  assert.ok(isRequired(col("text", { primary_key: true })));
  const e = col("enum", { values: ["a", "b"] });
  assert.equal(code(e, "a"), null);
  assert.deepEqual(checkValue(e, "x"), { code: "enum", arg: ["a", "b"] });
  assert.equal(code(col("integer", { min: 1, max: 10 }), "0"), "min");
  assert.equal(code(col("integer", { min: 1, max: 10 }), "11"), "max");
  assert.equal(code(col("number", { min: 0.5 }), "0.25"), "min");
  assert.equal(code(col("date", { min: "2026-01-01" }), "2025/12/31"), "min");
  assert.equal(code(col("text", { pattern: "[A-Z]{2}-\\d+" }), "AB-12"), null);
  assert.equal(code(col("text", { pattern: "[A-Z]{2}-\\d+" }), "AB-12x"), "pattern", "전체가 맞아야 한다(fullmatch)");
  assert.equal(code(col("text", { pattern: "(?P<x>a)" }), "zzz"), null, "JS가 읽지 못하는 pattern은 서버에 맡긴다");
});

test("붙여넣기 해석: 탭·줄바꿈·따옴표 칸", () => {
  assert.deepEqual(parseTsv("a\tb\r\n1\t2\r\n"), [["a", "b"], ["1", "2"]]);
  assert.deepEqual(parseTsv("a\t\tc\n"), [["a", "", "c"]]);
  assert.deepEqual(parseTsv("\"x\ty\"\t\"line1\nline2\"\n\"say \"\"hi\"\"\"\tz"), [["x\ty", "line1\nline2"], ["say \"hi\"", "z"]]);
  assert.deepEqual(parseTsv("5\" pipe\tok"), [["5\" pipe", "ok"]], "닫히지 않은 따옴표는 글자다");
  assert.deepEqual(parseTsv("a\n\n\n"), [["a"]], "끝의 빈 줄은 버린다");
  assert.deepEqual(parseTsv(""), []);
});

test("머리글 맞추기: 이름 → aliases → label → 느슨한 비교", () => {
  const cols = [
    { name: "item_code", aliases: ["code no"], label: "품목 코드" },
    { name: "qty", label: "수량" },
    { name: "note_text" },
  ];
  assert.deepEqual(matchHeader(["item_code", "수량", "Note Text", "기타"], cols), ["item_code", "qty", "note_text", null]);
  assert.deepEqual(matchHeader(["code no", "QTY"], cols), ["item_code", "qty"]);
  assert.deepEqual(matchHeader(["qty", "수량"], cols), ["qty", null], "같은 열에 두 번 맞으면 뒤의 것은 맞추지 않는다");
  assert.ok(looksLikeHeader(["item_code", "qty", ""], cols));
  assert.ok(!looksLikeHeader(["A-1", "3", "memo"], cols));
  assert.ok(!looksLikeHeader(["", ""], cols));
});

test("서버 오류 줄을 칸 오류와 나머지로 나눈다", () => {
  const { cells, other } = splitErrors(["rows[0].qty: 정수가 아님 'x'", "rows[3]: 열 집합이 rows[0]과 다름", "t: 이미 있는 키 A-1"]);
  assert.deepEqual(cells, [{ row: 0, column: "qty", message: "정수가 아님 'x'" }, { row: 3, column: null, message: "열 집합이 rows[0]과 다름" }]);
  assert.deepEqual(other, ["t: 이미 있는 키 A-1"]);
});

test("행 만들기: 빈 칸은 null, 값은 입력 그대로", () => {
  assert.deepEqual(rowFromValues(["a", "b", "c"], [" 1,000 ", "", undefined]), { a: " 1,000 ", b: null, c: null });
});

test("표시와 처음 값", () => {
  const b = col("boolean");
  assert.equal(display(b, "Y", { yes: "예", no: "아니요" }), "예");
  assert.equal(display(b, "", { empty: "-" }), "-");
  assert.equal(display(col("text"), 3), "3");
  assert.equal(initialValue(col("integer", { default: 0 })), "0");
  assert.equal(initialValue(col("boolean", { default: false })), "false");
  assert.equal(initialValue(col("text")), "");
});
