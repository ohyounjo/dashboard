// 운용 화면: 데이터셋 목록(#datasets)과 데이터셋 상세(#dataset/<키>): 그래프가 실제로 보는 뷰(dash.<키>)의 행(설계서 9-1).
// 관리 서비스(/api/manage)가 떠 있을 때만 메뉴에 있다. 행 표는 테이블 미리보기와 같은 부품(../rows.js)이다.
// sql: 데이터셋은 전체 행 수가 없어 "다음"만 보이고, "건수 세기"로 count: true를 보내 다시 읽는다.
import { h } from "../../common.js";
import { rowsView } from "../rows.js";
import { uid } from "../../shared/dom.js";
import * as St from "../../shared/structure.js";
import { dataFailText, box, btn, runJob, currentRevision } from "../datakit.js";
import { configResult, configFail } from "../structure.js";
import { T, view, session, api, head, pagedTable, detailId, message } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
let gen = 0;

const kindPill = (kind) => h("span", { class: "pill tone", "data-tone": kind === "table" ? "primary" : "neutral", text: D.kindLabel[kind] || kind });
const tableLink = (name) => (name ? h("a", { class: "a-link", href: `#table/${encodeURIComponent(name)}`, text: name }) : "—");

const isAdmin = () => ((session.manage && session.manage.role) || (session.me && session.me.role)) === "admin";

/** 데이터셋 만들기(admin): 키 + (테이블 고르기 | SQL 문) → PATCH /config {ops: [dataset.add], revision} → 작업.
 *  load가 실패하면(load_failed: SQL 오류 등) 서버가 모든 파일을 되돌렸으므로 "파일은 그대로"라고 알린다 */
function createCard(existing, alive, { onClose, onDone }) {
  const out = h("div", { class: "d-out" });
  const field = (label, control, hint) => {
    control.id = uid("ds");
    const err = h("span", { class: "d-err", id: `${control.id}-err` });
    control.setAttribute("aria-describedby", err.id);
    return { el: h("div", { class: "field d-field" }, h("label", { for: control.id, text: label }), control, hint ? h("span", { class: "hint", text: hint }) : null, err),
      setErr: (t) => { err.textContent = t || ""; if (t) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid"); } };
  };
  const key = h("input", { type: "text", class: "input", name: "key", maxlength: 63, autocomplete: "off", spellcheck: "false" });
  const kind = h("select", { class: "input", name: "kind" }, ["table", "sql"].map((k) => h("option", { value: k, text: D.kindLabel[k] })));
  const table = h("select", { class: "input", name: "table" }, h("option", { value: "", text: D.choose }));
  const sql = h("textarea", { class: "input mono", name: "sql", rows: 6, spellcheck: "false" });
  const desc = h("input", { type: "text", class: "input", name: "description", autocomplete: "off" });
  const rows = { key: field(D.datasetKey, key, D.nameRule), kind: field(D.datasetKind, kind), table: field(D.readsTable, table),
    sql: field(D.datasetSql, sql, D.datasetSqlHint), description: field(D.description, desc) };
  const sync = () => { rows.table.el.hidden = kind.value !== "table"; rows.sql.el.hidden = kind.value !== "sql"; };
  kind.addEventListener("change", sync);
  sync();
  api("GET", `${BASE}/tables`).then((r) => {
    for (const t of r.items || []) table.append(h("option", { value: t.name, text: t.name }));
  }).catch((err) => { if (alive()) out.replaceChildren(box("bad", dataFailText(err))); });
  const revAtOpen = currentRevision().catch(() => null);      // 폼을 연 때의 설정 판(그 사이 바뀌면 409 stale_config)
  const submit = h("button", { type: "submit", class: "btn primary" }, D.createDatasetSubmit);
  const form = h("form", { class: "d-form", novalidate: true },
    h("div", { class: "d-fields" }, rows.key.el, rows.kind.el, rows.table.el, rows.description.el), rows.sql.el,
    h("div", { class: "a-actions" }, submit, btn(D.cancel, onClose)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    Object.values(rows).forEach((r) => r.setErr(null));
    const made = St.datasetAddOp({ key: key.value, kind: kind.value, table: table.value, sql: sql.value, description: desc.value }, existing);
    if (made.problems.length) {
      for (const p of made.problems) {
        const m = D.structCheck[p.code];
        (rows[p.field] || rows.key).setErr(typeof m === "function" ? m(p.arg) : m || p.code);
      }
      ({ key, table, sql }[made.problems[0].field] || key).focus();
      return;
    }
    submit.disabled = true;
    try {
      const result = await runJob(async () => api("PATCH", `${BASE}/config`, { ops: [made.op], revision: await revAtOpen }), out, alive);
      if (!result || !alive()) return;
      onDone(configResult(result, D.datasetCreated(made.op.key)));
    } catch (err) {
      if (alive()) out.replaceChildren(configFail(err));
    } finally {
      submit.disabled = false;
    }
  });
  const el = h("section", { class: "a-card d-add", "aria-label": D.createDatasetTitle, "data-screen": "new-dataset" },
    h("div", { class: "a-head d-add-head" }, h("h3", { text: D.createDatasetTitle }), btn(D.close, onClose)),
    h("p", { class: "a-note", text: D.createDatasetIntro }), form, out);
  return { el, focus: () => key.focus() };
}

function listView(my) {
  const alive = () => my === gen;
  let cache = null;
  const table = pagedTable({
    defaultSort: "key",
    empty: D.noDatasets,
    load: async (st) => {
      if (!cache) cache = (await api("GET", `${BASE}/datasets`)).items || [];
      const from = (st.page - 1) * st.size;
      return { items: cache.slice(from, from + st.size), total: cache.length };
    },
    rowHref: (d) => `#dataset/${encodeURIComponent(d.key)}`,
    columns: [
      { key: "key", label: D.datasetKey, wrap: true, render: (d) => h("span", { class: "d-stack" },
        h("a", { class: "a-link", href: `#dataset/${encodeURIComponent(d.key)}`, text: d.key }),
        d.description ? h("small", { class: "a-key", text: d.description }) : null) },
      { key: "kind", label: D.datasetKind, render: (d) => kindPill(d.kind) },
      { key: "table", label: D.readsTable, render: (d) => tableLink(d.table) },
      { key: "used_by", label: D.usedBy, num: true, render: (d) => D.num(d.used_by || 0) },
    ],
  });
  const refresh = h("button", { type: "button", class: "btn", onclick: () => { cache = null; table.state.page = 1; table.draw(); } }, D.refresh);
  const createBox = h("div");
  let createBtn = null;
  if (isAdmin()) {
    createBtn = btn(D.createDataset, async () => {
      let keys = [];
      try { keys = (cache || (await api("GET", `${BASE}/datasets`)).items || []).map((d) => d.key); } catch (e) { keys = []; }
      if (!alive()) return;
      createBtn.hidden = true;
      const close = () => { createBox.replaceChildren(); createBtn.hidden = false; createBtn.focus(); };
      const made = createCard(keys, alive, {
        onClose: close,
        onDone: (done) => { createBox.replaceChildren(done); createBtn.hidden = false; cache = null; table.draw(); },
      });
      createBox.replaceChildren(made.el);
      made.focus();
    }, "primary");
    createBtn.dataset.action = "new-dataset";
  }
  view().replaceChildren(head(D.datasetsTitle, D.datasetsIntro, createBtn, refresh), createBox, table.el);
  table.draw();
}

async function detailView(key, my) {
  const alive = () => my === gen;
  const crumb = h("a", { class: "a-crumb", href: "#datasets" }, "← ", D.backToDatasets);
  view().replaceChildren(crumb, h("div", { class: "sk-note", text: T.loading }));
  let ds;
  try {
    const items = (await api("GET", `${BASE}/datasets`)).items || [];
    ds = items.find((d) => d.key === key);
  } catch (err) {
    if (alive()) view().replaceChildren(crumb, message(dataFailText(err)));
    return;
  }
  if (!alive()) return;
  if (!ds) { view().replaceChildren(crumb, message(D.errors.unknown_dataset)); return; }
  const info = h("dl", { class: "a-infos" },
    h("div", { class: "a-info" }, h("dt", { text: D.datasetKind }), h("dd", {}, kindPill(ds.kind))),
    h("div", { class: "a-info" }, h("dt", { text: D.readsTable }), h("dd", {}, tableLink(ds.table))),
    h("div", { class: "a-info" }, h("dt", { text: D.usedBy }), h("dd", { text: D.num(ds.used_by || 0) })));
  const rows = rowsView({ path: `${BASE}/datasets/${encodeURIComponent(ds.key)}/rows/query`, labels: ds.labels || {}, alive });
  view().replaceChildren(crumb, head(ds.key, ds.description || (ds.kind === "sql" ? D.sqlDatasetNote : D.tableDatasetNote)),
    h("div", { class: "a-card" }, info), h("div", { class: "a-gap" }), rows.el);
}

export default {
  key: "datasets",
  label: T.menu.datasets,
  match: /^(datasets|dataset\/.+)$/,
  when: () => !!session.manage,
  render(hash) {
    gen += 1;
    const key = detailId(hash, "dataset");
    if (key !== null) detailView(key, gen);
    else listView(gen);
  },
};
