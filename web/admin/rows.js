// 운용 화면의 행 표(테이블 상세의 미리보기 탭과 데이터셋 상세가 함께 쓴다). 계약: contracts/manage.md "행 조회".
// 정렬(머리글: 오름 → 내림 → 없음), 열별 필터(유형에 맞는 부품: web/shared/fields.js), 50행씩 커서로 다음·이전,
// 데이터가 바뀌면(changed) 처음부터 다시 보기, 잘린 칸(truncated_cells) 표시. 판단은 web/shared/query.js의 순수 함수다.
// 문구는 strings.js의 S.admin.data에만 둔다.
import { h } from "../common.js";
import { iconButton, uid } from "../shared/dom.js";
import * as F from "../shared/fields.js";
import * as Q from "../shared/query.js";
import { T, ApiFail, api } from "./kit.js";
import { dataFailText } from "./datakit.js";

const D = T.data;
const SIZE = 50;
const FIELD_TEXT = { yes: D.yes, no: D.no, choose: D.choose, empty: D.emptyValue };


const msg = (tone, text, ...extra) => h("div", { class: "a-msg d-box", "data-tone": tone, role: tone === "bad" ? "alert" : "status" }, h("span", { text }), ...extra);

/** 행 표. opts
 *   path      POST …/rows/query
 *   columns   열 정의 [{name, type, values?, label?}] (테이블은 GET /tables/{t}의 열. 없으면 첫 응답의 columns)
 *   labels    {열: 표시 이름} (데이터셋의 labels)
 *   alive()   화면이 그대로인지(떠났으면 늦게 온 응답을 버린다)
 *   rowAction(row) → 행 끝에 붙일 요소 | null (이 행 고치기)
 * 돌려주는 것: {el, reload()} — reload는 지금 페이지를 다시 읽는다(직접 고친 뒤라 "바뀌었습니다" 안내는 내지 않는다) */
export function rowsView({ path, columns: known = null, labels = {}, alive = () => true, rowAction = null }) {
  let cols = known;
  let sort = null;
  let pager = Q.pagerStart();
  let count = false;
  let entries = [];           // 필터 줄 [{id, column, op, input, err}]
  let sentRows = [];          // 보낸 filters[i] → entries의 자리
  let gen = 0;
  const filterRows = h("div", { class: "d-filter-rows" });
  const notice = h("div");
  const tableBox = h("div");
  const pagerBox = h("div", { class: "a-pager d-rows-pager" });
  const colDef = (name) => (cols || []).find((c) => c.name === name) || { name, type: "text" };
  const labelOf = (name) => labels[name] || (colDef(name).label) || name;

  /* ---------- 필터 ---------- */

  function newEntry(column) {
    const c = colDef(column);
    const op = Q.filterOps(c)[0];
    return { id: uid("flt"), column, op, input: valueInput(c), err: h("span", { class: "d-err" }) };
  }

  function valueInput(c) {
    // 필터 칸은 필수·기본값이 없는 칸이다(enum은 고르기, boolean은 예·아니요, 날짜는 달력)
    return F.createInput({ ...c, required: false, primary_key: false, default: null }, { text: FIELD_TEXT, attrs: { "aria-label": D.filterValue(labelOf(c.name)) } });
  }

  function drawFilters() {
    filterRows.replaceChildren(...entries.map((e, at) => {
      const c = colDef(e.column);
      const colSel = h("select", { class: "input", "aria-label": D.filterColumn }, (cols || []).map((x) => h("option", { value: x.name, text: labelOf(x.name) })));
      colSel.value = e.column;
      colSel.addEventListener("change", () => { entries[at] = newEntry(colSel.value); drawFilters(); entries[at].input.focus(); });
      const opSel = h("select", { class: "input", "aria-label": D.filterOp }, Q.filterOps(c).map((op) => h("option", { value: op, text: D.opLabel[op] || op })));
      opSel.value = e.op;
      opSel.addEventListener("change", () => { e.op = opSel.value; e.input.el.hidden = !Q.needsValue(e.op); });
      e.input.el.hidden = !Q.needsValue(e.op);
      return h("div", { class: "d-filter" }, colSel, opSel, e.input.el,
        iconButton("trash", D.removeFilter(labelOf(e.column)), { cls: "small", onclick: () => { entries.splice(at, 1); drawFilters(); restart(); } }),
        e.err);
    }));
    clearBtn.hidden = !entries.length;
  }

  const addBtn = h("button", { type: "button", class: "btn small", onclick: () => {
    if (!cols || !cols.length) return;
    entries.push(newEntry(cols[0].name));
    drawFilters();
    const last = filterRows.lastElementChild;
    if (last) last.querySelector("select").focus();
  } }, D.addFilter);
  const applyBtn = h("button", { type: "submit", class: "btn small primary" }, D.applyFilters);
  const clearBtn = h("button", { type: "button", class: "btn small", hidden: true, onclick: () => { entries = []; drawFilters(); restart(); } }, D.clearFilters);
  const form = h("form", { class: "d-filter-form", "aria-label": D.filtersLabel }, filterRows, h("div", { class: "a-actions" }, addBtn, applyBtn, clearBtn));
  form.addEventListener("submit", (e) => { e.preventDefault(); restart(); });

  /* ---------- 읽기 ---------- */

  function restart() {
    pager = Q.pagerStart();
    count = false;
    load();
  }

  async function load({ quiet = false } = {}) {
    const my = ++gen;
    const current = () => alive() && my === gen;
    const built = Q.buildFilters(entries.map((e) => ({ column: e.column, op: e.op, value: e.input.get() })), cols || []);
    sentRows = built.rows;
    entries.forEach((e) => { e.err.textContent = ""; });
    notice.replaceChildren(h("div", { class: "sk-note", role: "status", text: T.loading }));
    let res;
    try {
      res = await api("POST", path, Q.buildQuery({ filters: built.filters, sort, cursor: Q.pagerCursor(pager), limit: SIZE, count }));
    } catch (err) {
      if (!current()) return;
      const at = err instanceof ApiFail && err.detail === "invalid_filter" ? Q.filterErrorIndex(err.body && err.body.message) : null;
      const entry = at !== null ? entries[sentRows[at]] : null;
      if (entry) {
        entry.err.textContent = dataFailText(err).replace(/^filters\[\d+\]:\s*/, "");      // 어느 조건인지는 자리로 보인다
        notice.replaceChildren(msg("bad", D.fixFilter));
        entry.input.focus();
      } else {
        notice.replaceChildren(msg("bad", dataFailText(err)));
      }
      return;
    }
    if (!current()) return;
    if (!cols) { cols = res.columns || []; addBtn.disabled = !cols.length; }
    draw(res, { quiet });
  }

  function draw(res, { quiet }) {
    const shown = res.columns || [];
    const rows = res.rows || [];
    const cut = Q.truncatedSet(res.truncated_cells);
    notice.replaceChildren(res.changed && !quiet
      ? msg("warn", D.changedWhileReading, h("div", { class: "a-actions" }, h("button", { type: "button", class: "btn small", onclick: restart }, D.restartReading)))
      : "");
    if (!rows.length) {
      tableBox.replaceChildren(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: Q.pagerIndex(pager) ? D.noMoreRows : D.noMatchingRows })));
    } else {
      const head = h("tr", {}, shown.map((c) => {
        const th = h("th", { scope: "col", class: c.type === "integer" || c.type === "number" ? "num" : null });
        const on = sort && sort.column === c.name;
        if (on) th.setAttribute("aria-sort", sort.dir === "asc" ? "ascending" : "descending");
        th.append(h("button", { type: "button", title: D.sortBy(labelOf(c.name)), onclick: () => { sort = Q.nextSort(sort, c.name); restart(); } },
          h("span", { class: "d-stack" }, labelOf(c.name), labelOf(c.name) !== c.name ? h("small", { class: "a-key mono", text: c.name }) : null),
          on ? (sort.dir === "asc" ? " ↑" : " ↓") : ""));
        return th;
      }), rowAction ? h("th", { scope: "col" }, h("span", { class: "sr-only", text: D.actions })) : null);
      const body = rows.map((row, i) => h("tr", {}, shown.map((c) => {
        const v = row[c.name];
        const td = h("td", { class: c.type === "integer" || c.type === "number" ? "num" : null });
        if (v == null) td.append(h("span", { class: "d-null", text: "—" }));
        else {
          const text = F.display(colDef(c.name), v, FIELD_TEXT);
          td.append(h("span", { class: "d-val", title: text.length > 40 ? text : null, text }));
          if (cut.has(`${i}:${c.name}`)) td.append(h("span", { class: "d-cut", title: D.truncatedCell, "aria-label": D.truncatedCell, text: "…" }));
        }
        return td;
      }), rowAction ? h("td", {}, rowAction(row)) : null));
      tableBox.replaceChildren(h("div", { class: "a-table-wrap" }, h("table", { class: "a-table d-rows" }, h("thead", {}, head), h("tbody", {}, body))));
    }
    // 페이지: total이 있으면 "N행 중", 없으면 범위와 다음만. 셀 수 없으면 "건수 세기"(count: true로 다시 읽는다)
    const { from, to } = Q.pageRange(pager, SIZE, rows.length);
    const info = typeof res.total === "number" ? D.rowsOf(from, to, res.total)
      : res.total ? D.rowsOfMany(from, to, res.total) : D.rowsRange(from, to);
    const next = res.next_cursor;
    // replaceChildren은 null을 "null" 글자로 넣는다: 없는 버튼은 목록에서 뺀다
    pagerBox.replaceChildren(...[
      h("span", { text: info }),
      res.total == null && !count ? h("button", { type: "button", class: "btn small", onclick: () => { count = true; load(); } }, D.countRows) : null,
      h("button", { type: "button", class: "btn small", disabled: Q.pagerIndex(pager) === 0, onclick: () => { pager = Q.pagerPrev(pager); load(); } }, T.prev),
      h("button", { type: "button", class: "btn small", disabled: !next, onclick: () => { pager = Q.pagerNext(pager, next); load(); } }, T.next),
    ].filter(Boolean));
  }

  if (cols) addBtn.disabled = !cols.length; else addBtn.disabled = true;
  load();
  return { el: h("div", { class: "d-stack gap d-rows-view" }, form, notice, tableBox, pagerBox), reload: () => load({ quiet: true }) };
}
