// 행 조회 화면의 순수 함수(contracts/manage.md "행 조회"): 정렬 순환, 필터 → 요청, 커서 쌓기, 고치기 폼의 바뀐 열.
// DOM을 쓰지 않는다(node --test로 시험한다). 문구는 이 파일에 두지 않는다. 이 폴더 밖의 모듈을 import하지 않는다.
import { tidy } from "./fields.js";

/** 열 머리글을 누를 때의 정렬: 없음 → 오름 → 내림 → 없음. 다른 열을 누르면 그 열의 오름. sort: {column, dir} | null */
export function nextSort(sort, column) {
  if (!sort || sort.column !== column) return { column, dir: "asc" };
  if (sort.dir === "asc") return { column, dir: "desc" };
  return null;
}

/** 열 유형 → 필터 부품이 고를 수 있는 연산(첫 것이 기본). values가 있는 열(enum)은 고르기(in).
 *  서버: lt~ge는 글·숫자·날짜·시각, contains·starts_with는 글만. */
export function filterOps(column) {
  if (Array.isArray(column.values) && column.values.length) return ["in", "is_null"];
  switch (column.type) {
    case "integer": case "number": case "date": case "timestamp": return ["eq", "ge", "le", "is_null"];
    case "boolean": return ["eq", "is_null"];
    default: return ["contains", "eq", "is_null"];
  }
}

/** 값이 필요 없는 연산 */
export const needsValue = (op) => op !== "is_null" && op !== "not_null";

/** 필터 입력들 → 요청의 filters. entries: [{column, op, value}] (화면의 줄 순서), columns: 열 정의({name, type, values?}).
 *  값이 비었으면(값이 필요한 연산인데) 그 줄은 보내지 않는다. 값은 서버와 같은 표기로 다듬는다(1,234 → 1234, 2026/11/20 → 2026-11-20).
 *  돌려주는 것: {filters, rows: [보낸 filters[i]가 화면의 몇째 줄인지]} (422 invalid_filter의 filters[i]를 그 줄에 보인다) */
export function buildFilters(entries, columns) {
  const byName = new Map((columns || []).map((c) => [c.name, c]));
  const filters = [];
  const rows = [];
  (entries || []).forEach((e, at) => {
    const col = byName.get(e && e.column);
    if (!col || !e.op) return;
    if (!needsValue(e.op)) { filters.push({ column: col.name, op: e.op }); rows.push(at); return; }
    const raw = e.value == null ? "" : String(e.value);
    const value = tidy(col, raw);
    if (value === "") return;
    filters.push({ column: col.name, op: e.op, value: e.op === "in" ? [value] : value });
    rows.push(at);
  });
  return { filters, rows };
}

/** 422 invalid_filter의 글("filters[2]: …") → 2. 없으면 null. */
export function filterErrorIndex(message) {
  const m = String(message || "").match(/^filters\[(\d+)\]/);
  return m ? Number(m[1]) : null;
}

/** 요청 본문. sort: {column, dir} | null */
export function buildQuery({ filters = [], sort = null, cursor = null, limit = 50, count = false } = {}) {
  const body = { filters, sort: sort ? [{ column: sort.column, dir: sort.dir }] : [], limit, cursor };
  if (count) body.count = true;
  return body;
}

/** CSV 내보내기 URL. sort: {column, dir} | [{column, dir}] | null */
export function buildExportUrl(path, { filters = [], sort = null, columns = null } = {}) {
  const params = new URLSearchParams({ format: "csv" });
  if (Array.isArray(filters) && filters.length) params.set("filters", JSON.stringify(filters));
  const sortList = Array.isArray(sort) ? sort : (sort ? [{ column: sort.column, dir: sort.dir }] : []);
  if (sortList.length) params.set("sort", JSON.stringify(sortList));
  if (Array.isArray(columns) && columns.length) params.set("columns", JSON.stringify(columns));
  const sep = String(path).includes("?") ? "&" : "?";
  return `${path}${sep}${params.toString()}`;
}

/** 페이지 커서 쌓기. 이전으로 가려면 받았던 커서를 쌓아 둔다(서버 커서는 앞으로만 간다).
 *  상태: {stack: [첫 페이지 null, 둘째 페이지의 커서, …]}; 지금 페이지의 커서는 stack의 마지막 */
export const pagerStart = () => ({ stack: [null] });
export const pagerCursor = (p) => p.stack[p.stack.length - 1];
export const pagerIndex = (p) => p.stack.length - 1;
export const pagerNext = (p, nextCursor) => (nextCursor ? { stack: [...p.stack, nextCursor] } : p);
export const pagerPrev = (p) => (p.stack.length > 1 ? { stack: p.stack.slice(0, -1) } : p);

/** truncated_cells([[행 번호, 열]]) → 찾기 쉬운 Set("행:열") */
export function truncatedSet(cells) {
  return new Set((cells || []).map(([i, c]) => `${i}:${c}`));
}

/** 응답 값 → 입력 칸에 넣을 글. boolean은 true/false, null은 "". */
export function valueToInput(v) {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

/** 고치기 폼: 처음 값(한 행 읽기의 row)과 지금 입력을 견주어 바뀐 열 이름(기본 키·잠근 열 제외).
 *  같은 뜻의 다른 표기(1,000과 1000, 2026/11/20과 2026-11-20)는 바뀐 것으로 보지 않는다. */
export function changedColumns(columns, initial, current, { locked = [] } = {}) {
  const skip = new Set(locked);
  return (columns || []).filter((c) => !c.primary_key && !c.sensitive && !skip.has(c.name)
    && tidy(c, valueToInput((initial || {})[c.name])) !== tidy(c, (current || {})[c.name] ?? "")).map((c) => c.name);
}

/** 고치기 저장의 한 행: 기본 키와 바뀐 열만(보내지 않은 열은 그대로 남는다, 설계서 5-2). 빈 칸은 null. */
export function editRow(columns, initial, current, changed) {
  const row = {};
  for (const c of columns || []) {
    if (c.primary_key) row[c.name] = (initial || {})[c.name];
    else if (changed.includes(c.name)) {
      const v = (current || {})[c.name];
      row[c.name] = v == null || String(v).trim() === "" ? null : String(v);
    }
  }
  return row;
}

/** 페이지 범위 글에 쓸 숫자: {from, to} (1부터) */
export function pageRange(pager, size, shown) {
  const from = pagerIndex(pager) * size + 1;
  return { from: shown ? from : 0, to: from + shown - 1 };
}
