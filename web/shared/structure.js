// 구조 변경 화면의 순수 함수(contracts/manage.md "구조 변경", studio.md "변경 연산"): 이름 검사, 유형 넓히기 후보,
// 열·데이터셋 연산 만들기, 새 테이블 정의 정리. DOM을 쓰지 않는다(node --test로 시험한다).
// 문구는 이 파일에 두지 않는다: 검사는 코드 목록을 돌려주고 화면이 글로 바꾼다. 이 폴더 밖의 모듈을 import하지 않는다.
import { checkValue, tidy } from "./fields.js";

/** 테이블·열·데이터셋 이름: 소문자로 시작하는 snake_case, 63자까지(서버 NAME_RE와 같다) */
export const NAME_RE = /^[a-z][a-z0-9_]{0,62}$/;
export const TYPES = ["text", "integer", "number", "boolean", "date", "timestamp", "enum"];

/** 이 화면이 만드는 구조 변경 연산(PATCH /config가 받는 것 가운데) */
export const OPS = { columnAdd: "column.add", columnUpdate: "column.update", datasetAdd: "dataset.add" };

// 유형은 넓히기만 받는다(서버 dashkit/project/ops.py의 _WIDER)
const WIDER = { integer: ["number", "text"], number: ["text"], boolean: ["text"], date: ["text"], timestamp: ["text"], enum: ["text"], text: [] };

/** 그 유형에서 바꿀 수 있는 유형들(넓히기). */
export const widenTargets = (type) => WIDER[type] || [];

/** 쉼표·줄바꿈으로 나눈 enum 값 목록(앞뒤 공백 제거, 빈 것·겹친 것 빼기) */
export function splitValues(text) {
  const out = [];
  for (const v of String(text || "").split(/[,\n]/).map((x) => x.trim())) if (v && !out.includes(v)) out.push(v);
  return out;
}

/** 열 추가 폼 → column.add 연산. 돌려주는 것: {op, problems: [{field, code}]} (problems가 있으면 op는 null).
 *  form: {name, type, required, default, description, values(글)}; existing: 이미 있는 열 이름 */
export function columnAddOp(table, form, existing = []) {
  const problems = [];
  const name = String(form.name || "").trim();
  if (!NAME_RE.test(name)) problems.push({ field: "name", code: "name" });
  else if (existing.includes(name)) problems.push({ field: "name", code: "exists" });
  if (!TYPES.includes(form.type)) problems.push({ field: "type", code: "type" });
  const value = { type: form.type };
  if (form.type === "enum") {
    const values = splitValues(form.values);
    if (!values.length) problems.push({ field: "values", code: "values" });
    value.values = values;
  }
  const def = String(form.default ?? "").trim();
  if (form.required) {
    value.required = true;
    if (!def) problems.push({ field: "default", code: "defaultRequired" });       // 기존 행이 검증에서 걸린다
  }
  if (def) {
    const col = { name, type: form.type, values: value.values };
    const bad = checkValue(col, def);
    if (bad) problems.push({ field: "default", code: bad.code, arg: bad.arg });
    else value.default = typedDefault(form.type, tidy(col, def));
  }
  const desc = String(form.description || "").trim();
  if (desc) value.description = desc;
  return problems.length ? { op: null, problems } : { op: { op: OPS.columnAdd, table, column: name, value }, problems };
}

/** default는 schema.yaml에 그 유형의 값으로 적는다(숫자는 숫자, 참거짓은 참거짓) */
function typedDefault(type, v) {
  if (type === "integer") return Number.isSafeInteger(Number(v)) ? Number(v) : v;
  if (type === "number") return Number.isFinite(Number(v)) && String(Number(v)) === v ? Number(v) : v;
  if (type === "boolean") return v === "true";
  return v;
}

/** 열 규칙 고치기 폼 → column.update 연산(바뀐 키만, 비우면 null = 지움). before: GET /tables/{t}의 열.
 *  form: {description, values(글), min, max, pattern, type}. 돌려주는 것: {op | null, problems, typeChanged} */
export function columnUpdateOp(table, before, form) {
  const set = {};
  const problems = [];
  const norm = (v) => (v == null ? "" : String(v).trim());
  const desc = norm(form.description);
  if (desc !== norm(before.description)) set.description = desc || null;
  if (before.type === "enum" && form.values !== undefined) {
    const values = splitValues(form.values);
    if (!values.length) problems.push({ field: "values", code: "values" });
    else if (values.join("\n") !== (before.values || []).map(String).join("\n")) set.values = values;
  }
  for (const k of ["min", "max"]) {
    const v = norm(form[k]);
    if (v === norm(before[k])) continue;
    if (!v) { set[k] = null; continue; }
    const kind = before.type === "date" ? "date" : before.type === "integer" ? "integer" : "number";
    const bad = checkValue({ name: k, type: kind }, v);
    if (bad) problems.push({ field: k, code: bad.code, arg: bad.arg });
    else set[k] = kind === "date" ? tidy({ type: "date" }, v) : Number(tidy({ type: kind }, v));
  }
  const pat = norm(form.pattern);
  if (pat !== norm(before.pattern)) set.pattern = pat || null;
  let typeChanged = false;
  if (form.type && form.type !== before.type) {
    if (!widenTargets(before.type).includes(form.type)) problems.push({ field: "type", code: "widen" });
    else { set.type = form.type; typeChanged = true; }
  }
  if (problems.length) return { op: null, problems, typeChanged };
  if (!Object.keys(set).length) return { op: null, problems: [], typeChanged };
  return { op: { op: OPS.columnUpdate, table, column: before.name, set }, problems, typeChanged };
}

/** 데이터셋 만들기 폼 → dataset.add. form: {key, kind: "table"|"sql", table, sql, description}; existing: 있는 키 */
export function datasetAddOp(form, existing = []) {
  const problems = [];
  const key = String(form.key || "").trim();
  if (!NAME_RE.test(key)) problems.push({ field: "key", code: "name" });
  else if (existing.includes(key)) problems.push({ field: "key", code: "exists" });
  const value = {};
  if (form.kind === "sql") {
    const sql = String(form.sql || "").trim();
    if (!sql) problems.push({ field: "sql", code: "sql" });
    value.sql = sql;
  } else {
    if (!form.table) problems.push({ field: "table", code: "table" });
    value.table = form.table;
  }
  const desc = String(form.description || "").trim();
  if (desc) value.description = desc;
  return problems.length ? { op: null, problems } : { op: { op: OPS.datasetAdd, key, value }, problems };
}

/** 추론 결과(POST /tables/infer의 table)를 사람이 고친 것 → POST /tables의 table. 표시용 키(stats, confidence, notes,
 *  basis)는 빼고 서버가 받는 키만 남긴다. 돌려주는 것: {table, problems: [{field, index?, code}]} */
export function tableForCreate(edited) {
  const problems = [];
  const name = String(edited.name || "").trim();
  if (!NAME_RE.test(name)) problems.push({ field: "name", code: "name" });
  const seen = new Set();
  const columns = (edited.columns || []).map((c, index) => {
    const col = { name: String(c.name || "").trim(), type: c.type };
    if (!NAME_RE.test(col.name)) problems.push({ field: "column", index, code: "name" });
    else if (seen.has(col.name)) problems.push({ field: "column", index, code: "duplicate" });
    seen.add(col.name);
    if (!TYPES.includes(c.type)) problems.push({ field: "type", index, code: "type" });
    if (c.source) col.source = c.source;
    if (c.required) col.required = true;
    if (c.type === "enum") {
      col.values = Array.isArray(c.values) ? c.values : splitValues(c.values);
      if (!col.values.length) problems.push({ field: "values", index, code: "values" });
    }
    for (const k of ["ref", "description", "min", "max", "pattern"]) if (c[k] != null && c[k] !== "") col[k] = c[k];
    return col;
  });
  const pk = (edited.primary_key || []).filter((k) => columns.some((c) => c.name === k));
  if (!pk.length) problems.push({ field: "primary_key", code: "primaryKey" });
  return { table: { name, primary_key: pk, columns }, problems };
}

/** 열 이름을 바꿨을 때 기본 키 목록도 따라 바꾼다 */
export function renameInKey(primaryKey, from, to) {
  return (primaryKey || []).map((k) => (k === from ? to : k));
}

/** 쓰이는 곳(GET /tables/{t}/used_by) → 데이터셋마다 [{dataset, charts: [{key, title, tabs}], sections: [{tab, index, type}]}] */
export function usageByDataset(used) {
  const u = used || {};
  return (u.datasets || []).map((d) => ({
    dataset: d,
    charts: (u.charts || []).filter((c) => c.dataset === d).map((c) => ({ key: c.key, title: c.title, dashboard: c.dashboard, tabs: c.tabs || [] })),
    sections: (u.sections || []).filter((s) => s.dataset === d).map((s) => ({ tab: s.tab, index: s.index, type: s.type })),
  }));
}
