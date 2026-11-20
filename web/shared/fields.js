// 열 유형 → 입력 부품·형식 검사·표시(설계서 9-4). 직접 입력 폼, 여러 행 표, (이후) 행 조회의 필터 입력과 추론 검토 표가 함께 쓴다.
// 테이블이나 열이 늘어도 화면 코드는 바뀌지 않는다. 유형이 늘면 아래 TYPES에 한 줄을 더한다.
//
// - 순수 함수(tidy, checkValue, parseTsv, matchHeader, splitErrors, rowFromValues)는 DOM을 쓰지 않는다(node --test로 시험한다).
// - 문구는 이 파일에 두지 않는다: 형식 검사는 {code, arg}를 돌려주고, 부품의 글자는 부르는 쪽이 text로 넘긴다.
// - 값 다듬기는 서버(dashkit/ingest/normalize.py의 NORMALIZERS)와 같은 표기만 받는다. 서버가 정규형으로 바꾸므로 보낼 때는 입력 그대로 보낸다.
// - 이 폴더 밖의 모듈을 import하지 않는다(studio 컨테이너는 web/studio/와 web/shared/만 서빙한다).
import { h, uid } from "./dom.js";

/* ---------------- 값 다듬기(서버와 같은 규칙) ---------------- */

const YMD_SEP = /^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$/;
const YMD_PACKED = /^(\d{4})(\d{2})(\d{2})$/;
const THOUSANDS = /^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$/;
const BOOLEANS = { y: "true", yes: "true", 1: "true", true: "true", n: "false", no: "false", 0: "false", false: "false" };
const TS_SPLIT = /^(\S+?)(?:[ T]+)(\d{1,2}:\d{2}.*)$/;

function ymd(value) {
  const m = value.match(YMD_SEP) || value.match(YMD_PACKED);
  if (!m) return null;
  return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
}

const tidyDate = (v) => ymd(v) || v;
const tidyNumber = (v) => (THOUSANDS.test(v) ? v.replace(/,/g, "") : v);
const tidyBoolean = (v) => BOOLEANS[v.toLowerCase()] || v;
function tidyTimestamp(v) {
  const m = v.match(TS_SPLIT);
  if (!m) return tidyDate(v);
  return `${ymd(m[1]) || m[1]}T${m[2]}`;
}

/* ---------------- 형식 검사 ---------------- */

const INT_RE = /^[+-]?\d+$/;
const NUM_RE = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
// Python datetime.fromisoformat(3.12)이 받는 흔한 꼴: 날짜만, 또는 날짜 + T·공백 + 시:분[:초[.소수]] + 시간대(Z, ±HH[:MM])
const TS_RE = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,6})?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$/;
const BIGINT_MAX = 9223372036854775807n;
const BIGINT_MIN = -9223372036854775808n;

function realDate(text) {
  const m = text.match(DATE_RE);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d && y >= 1;
}

function checkInteger(v) {
  if (!INT_RE.test(v)) return { code: "integer" };
  const n = BigInt(v);
  return n > BIGINT_MAX || n < BIGINT_MIN ? { code: "integerRange" } : null;
}
const checkNumber = (v) => (NUM_RE.test(v) ? null : { code: "number" });
const checkBoolean = (v) => (v === "true" || v === "false" ? null : { code: "boolean" });
const checkDate = (v) => (DATE_RE.test(v) ? (realDate(v) ? null : { code: "noSuchDate" }) : { code: "date" });
function checkTimestamp(v) {
  const m = v.match(TS_RE);
  if (!m || !realDate(m[1])) return { code: "timestamp" };
  if (m[2] !== undefined && (Number(m[2]) > 23 || Number(m[3]) > 59 || (m[4] !== undefined && Number(m[4]) > 59))) return { code: "timestamp" };
  return null;
}

/** 유형 등록 표. input: 한 칸 입력 부품의 종류, inputmode: 화면 키보드, tidy: 받아 주는 표기 → 정규형, check: 정규형의 형식 검사, compare: min·max 비교 */
export const TYPES = {
  text: { input: "text", tidy: (v) => v, check: () => null },
  integer: { input: "text", inputmode: "numeric", tidy: tidyNumber, check: checkInteger, compare: (a, b) => cmpNumber(a, b) },
  number: { input: "text", inputmode: "decimal", tidy: tidyNumber, check: checkNumber, compare: (a, b) => cmpNumber(a, b) },
  boolean: { input: "boolean", tidy: tidyBoolean, check: checkBoolean },
  date: { input: "date", tidy: tidyDate, check: checkDate, compare: (a, b) => (a < b ? -1 : a > b ? 1 : 0) },
  timestamp: { input: "text", placeholder: "YYYY-MM-DDTHH:MM", tidy: tidyTimestamp, check: checkTimestamp },
  enum: { input: "select", tidy: (v) => v, check: () => null },
};

/** 서버(Python re.fullmatch)의 pattern → JS 정규식. JS가 읽지 못하는 꼴이면 null(서버의 검사에 맡긴다). */
function patternOf(pattern) {
  for (const flags of ["u", ""]) {
    try { return new RegExp(`^(?:${pattern})$`, flags); } catch (e) { /* 다음 꼴 */ }
  }
  return null;
}

function cmpNumber(a, b) {
  const x = Number(a), y = Number(b);
  return x < y ? -1 : x > y ? 1 : 0;
}

const typeOf = (column) => TYPES[column && column.type] || TYPES.text;

/** 열이 반드시 값을 가져야 하는지(기본 키 열은 언제나 필수다). */
export const isRequired = (column) => !!(column.required || column.primary_key);

/** 입력값 → 서버가 저장할 정규형(앞뒤 공백 제거 + 유형별 표기). 빈 값은 "". */
export function tidy(column, raw) {
  const v = raw == null ? "" : String(raw).trim();
  return v === "" ? "" : typeOf(column).tidy(v);
}

/** 한 칸의 형식 검사. 맞으면 null, 아니면 {code, arg}.
 *  code: required | integer | integerRange | number | boolean | date | noSuchDate | timestamp | enum | min | max | pattern */
export function checkValue(column, raw) {
  const v = tidy(column, raw);
  if (v === "") return isRequired(column) ? { code: "required" } : null;
  const type = typeOf(column);
  const bad = type.check(v);
  if (bad) return bad;
  if (column.type === "enum" && Array.isArray(column.values) && !column.values.map(String).includes(v)) return { code: "enum", arg: column.values.map(String) };
  if (column.pattern != null) {
    const re = patternOf(column.pattern);
    if (re && !re.test(v)) return { code: "pattern", arg: column.pattern };
  }
  if (type.compare) {
    if (column.min != null && type.compare(v, String(column.min)) < 0) return { code: "min", arg: column.min };
    if (column.max != null && type.compare(v, String(column.max)) > 0) return { code: "max", arg: column.max };
  }
  return null;
}

/* ---------------- 붙여넣기 해석 ---------------- */

/** 엑셀·시트에서 복사한 글(탭 구분, 줄바꿈으로 행) → 칸의 2차원 배열.
 *  큰따옴표로 감싼 칸(안에 탭·줄바꿈·"" 포함)을 푼다. 끝의 빈 줄은 버린다. */
export function parseTsv(text) {
  const src = String(text == null ? "" : text).replace(/\r\n?/g, "\n");
  const rows = [];
  let row = [];
  let cell = "";
  let i = 0;
  let atStart = true;           // 칸의 첫 글자인지(따옴표로 시작하는 칸만 따옴표 칸이다)
  while (i < src.length) {
    const ch = src[i];
    if (atStart && ch === "\"") {
      const end = closingQuote(src, i + 1);
      if (end >= 0) {
        cell = src.slice(i + 1, end).replace(/""/g, "\"");
        i = end + 1;
        atStart = false;
        continue;
      }
    }
    if (ch === "\t") { row.push(cell); cell = ""; atStart = true; i += 1; continue; }
    if (ch === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; atStart = true; i += 1; continue; }
    cell += ch;
    atStart = false;
    i += 1;
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  while (rows.length && rows[rows.length - 1].every((c) => c === "")) rows.pop();
  return rows;
}

/** 따옴표 칸의 닫는 따옴표 위치(그 뒤가 탭·줄바꿈·끝이어야 한다). 없으면 -1(따옴표를 글자로 본다). */
function closingQuote(src, from) {
  let j = from;
  while (j < src.length) {
    if (src[j] === "\"") {
      if (src[j + 1] === "\"") { j += 2; continue; }
      const next = src[j + 1];
      return next === undefined || next === "\t" || next === "\n" ? j : -1;
    }
    j += 1;
  }
  return -1;
}

const loose = (s) => String(s).toLowerCase().replace(/[\s_-]+/g, "");

/** 머리글 칸들 → 열 이름(맞지 않으면 null). 서버의 머리글 맞추기와 같은 차례: ① 열 이름 ② aliases ③ label ④ 대소문자·공백·_·- 무시.
 *  한 단계에서 두 열에 맞으면 맞추지 않는다. 같은 열에 두 번 맞으면 뒤의 것은 null이다. */
export function matchHeader(cells, columns) {
  const steps = [
    (c, x) => c.name === x,
    (c, x) => Array.isArray(c.aliases) && c.aliases.map(String).includes(x),
    (c, x) => c.label != null && String(c.label) === x,
    (c, x) => [c.name, ...(c.aliases || []), ...(c.label != null ? [c.label] : [])].some((n) => loose(n) === loose(x)),
  ];
  const used = new Set();
  return cells.map((raw) => {
    const x = String(raw == null ? "" : raw).trim();
    if (!x) return null;
    for (const step of steps) {
      const found = columns.filter((c) => step(c, x));
      if (found.length === 1) {
        if (used.has(found[0].name)) return null;
        used.add(found[0].name);
        return found[0].name;
      }
      if (found.length > 1) return null;
    }
    return null;
  });
}

/** 붙여 넣은 첫 줄이 머리글인지: 값이 있는 칸의 절반 이상이 열에 맞으면 머리글로 본다. */
export function looksLikeHeader(cells, columns) {
  const filled = cells.filter((c) => String(c).trim() !== "");
  if (!filled.length) return false;
  const hits = matchHeader(cells, columns).filter((m) => m !== null).length;
  return hits >= 1 && hits * 2 >= filled.length;
}

/* ---------------- 서버 오류 줄 ---------------- */

const ROW_ERR = /^rows\[(\d+)\](?:\.([^:]+))?:\s*(.*)$/s;

/** 오류 줄들을 칸 오류(`rows[<번호>].<열>: …`)와 나머지로 나눈다. cells: [{row, column, message}] (column은 없을 수 있다) */
export function splitErrors(lines) {
  const cells = [];
  const other = [];
  for (const line of lines || []) {
    const m = String(line).match(ROW_ERR);
    if (m) cells.push({ row: Number(m[1]), column: m[2] === undefined || m[2] === "-" ? null : m[2], message: m[3] });
    else other.push(String(line));
  }
  return { cells, other };
}

/** 칸 값들 → JSON 한 행. 빈 칸은 null("일부러 비움")이다. names: 보낼 열 이름, values: 같은 순서의 값 */
export function rowFromValues(names, values) {
  const row = {};
  names.forEach((n, i) => {
    const v = values[i] == null ? "" : String(values[i]);
    row[n] = v.trim() === "" ? null : v;
  });
  return row;
}

/** 값의 표시(boolean은 text.yes/no, 빈 값은 text.empty). */
export function display(column, value, text = {}) {
  if (value == null || value === "") return text.empty || "";
  if (column && column.type === "boolean") {
    const v = tidy(column, value);
    if (v === "true") return text.yes || "true";
    if (v === "false") return text.no || "false";
  }
  return String(value);
}

/* ---------------- 입력 부품(DOM) ---------------- */

/** 열 하나의 입력 부품. 돌려주는 것: {el, get(), set(v), focus(), invalid()}. get()은 문자열(빈 칸은 "").
 *  invalid()는 브라우저 입력 칸이 값을 만들지 못한 상태(날짜를 반만 넣음 등)면 {code}, 아니면 null이다(그때 get()은 ""라 빈 칸과 구별되지 않는다).
 *  opts.cell이면 표의 한 칸이다: 붙여넣기 값을 그대로 담도록 언제나 글 입력이고, enum·boolean은 datalist로 고를 수 있다.
 *  text: {yes, no, choose} — 부품 안의 글자(문구는 부르는 쪽이 준다). attrs: 입력 요소에 더할 속성(id, aria-label 등) */
export function createInput(column, { cell = false, text = {}, attrs = {} } = {}) {
  const type = typeOf(column);
  const required = isRequired(column);
  const kind = cell ? "text" : type.input;
  if (kind === "select" || (kind === "boolean" && !required)) {
    const options = kind === "select"
      ? (column.values || []).map((v) => ({ value: String(v), label: String(v) }))
      : [{ value: "true", label: text.yes || "true" }, { value: "false", label: text.no || "false" }];
    const hasDefault = column.default != null;
    const el = h("select", { class: "input", ...attrs },
      !required || !hasDefault ? h("option", { value: "", text: text.choose || "" }) : null,
      options.map((o) => h("option", { value: o.value, text: o.label })));
    return {
      el,
      get: () => el.value,
      set(v) { const t = tidy(column, v); el.value = t; if (el.value !== t) el.value = ""; },
      focus: () => el.focus(),
      invalid: () => null,
    };
  }
  if (kind === "boolean") {
    const el = h("input", { type: "checkbox", ...attrs });
    return {
      el,
      get: () => (el.checked ? "true" : "false"),
      set(v) { el.checked = tidy(column, v ?? "") === "true"; },
      focus: () => el.focus(),
      invalid: () => null,
    };
  }
  const el = h("input", {
    type: kind === "date" ? "date" : "text", class: "input", autocomplete: "off", spellcheck: "false",
    inputmode: type.inputmode || null, placeholder: !cell && type.placeholder ? type.placeholder : null, ...attrs,
  });
  if (kind === "date") {
    if (column.min != null) el.setAttribute("min", String(column.min));
    if (column.max != null) el.setAttribute("max", String(column.max));
  }
  return {
    el,
    get: () => el.value,
    set(v) {
      const s = v == null ? "" : String(v);
      el.value = kind === "date" ? tidy(column, s) : s;
    },
    focus: () => el.focus(),
    invalid: () => (kind === "date" && el.validity && el.validity.badInput ? { code: "date" } : null),
  };
}

/** 표의 칸이 고를 값 목록(enum·boolean). 없으면 null. 같은 열의 칸들이 list 속성으로 함께 쓴다. */
export function datalistFor(column, text = {}) {
  const values = column.type === "enum" ? (column.values || []).map(String)
    : column.type === "boolean" ? ["true", "false"] : null;
  if (!values) return null;
  return h("datalist", { id: uid("dl") }, values.map((v) => h("option", { value: v, label: column.type === "boolean" ? display(column, v, text) : null })));
}

/** 처음 채울 값: default가 있으면 그 값(문자열), 없으면 "". */
export const initialValue = (column) => (column.default == null ? "" : String(column.default));
