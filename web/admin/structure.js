// 운용 화면의 구조 변경(설계서 6·7절, contracts/manage.md "구조 변경"): 새 테이블 마법사, 열 추가, 열 규칙 고치기,
// 쓰이는 곳, 테이블 삭제, 배치 합치기, 구조 변경 결과·오류 표시. 데이터 메뉴(menus/tables.js)와 데이터셋 메뉴가 쓴다.
// 바꾸는 일은 admin만 한다(서버도 require_admin). 연산 만들기·검사는 web/shared/structure.js의 순수 함수다.
// 문구는 strings.js의 S.admin.data에만 둔다.
import { h } from "../common.js";
import { fileDrop, filePicker, uid } from "../shared/dom.js";
import * as St from "../shared/structure.js";
import { T, ApiFail, api, apiForm, checkbox } from "./kit.js";
import { box, btn, pill, dataFailText, runJob, currentRevision } from "./datakit.js";

const D = T.data;
const BASE = "/api/manage";
const tablePath = (name) => `${BASE}/tables/${encodeURIComponent(name)}`;

/* ---------------- 공통: 결과·오류, 폼 칸 ---------------- */

/** 구조 변경 작업의 결과(contracts/manage.md "구조 변경"): 바뀐 파일, 행 수, sync, 영향, 새 열이 안 나오는 데이터셋 */
export function configResult(r, title, actions = []) {
  const lines = [];
  if (r.table) lines.push(D.createdTable(r.table));
  if (r.dataset) lines.push(D.createdDataset(r.dataset));
  if (r.tab) lines.push(D.createdTab(r.tab));
  if (r.restored) lines.push(D.restoredHistory(r.restored));
  if (r.changed && r.changed.length) lines.push(D.changedFiles(r.changed));
  else if (r.changed) lines.push(D.nothingChanged);
  for (const a of r.affected || []) if (a.charts && a.charts.length) lines.push(D.affectedCharts(`${a.table}.${a.column}`, a.charts));
  for (const w of r.datasets_without_column || []) if (w.datasets && w.datasets.length) lines.push(D.datasetsWithoutColumn(w.column, w.datasets));
  const syncBad = r.sync && r.sync.ok === false;
  return box(syncBad ? "warn" : "good", title, {
    lines,
    actions,
    body: syncBad ? h("div", {}, h("p", { text: D.syncFailed }), h("ul", { class: "d-lines mono" }, (r.sync.errors || []).map((e) => h("li", { text: e })))) : null,
  });
}

/** 구조 변경 실패: load_failed면 "파일은 그대로(모두 되돌렸다)", in_use면 쓰는 곳 목록, stale_config면 다시 읽기 안내 */
export function configFail(err, { retry } = {}) {
  const body = (err instanceof ApiFail && err.body) || {};
  const detail = err instanceof ApiFail ? err.detail : null;
  const note = detail === "load_failed" ? D.loadFailedNote : detail === "in_use" ? D.inUseNote : detail === "stale_config" ? D.staleConfigNote : null;
  return box("bad", dataFailText(err), {
    body: note ? h("p", { text: note }) : null,
    lines: Array.isArray(body.errors) ? body.errors.map(String) : [],
    actions: retry && detail === "stale_config" ? [btn(D.retry, retry, "small")] : [],
  });
}

/** 레이블 + 칸 + 도움말 + 오류 자리. 돌려주는 것: {el, setErr(text)} */
function fieldRow(label, control, hint) {
  if (!control.id) control.id = uid("st");
  const errId = `${control.id}-err`, hintId = `${control.id}-hint`;
  const err = h("span", { class: "d-err", id: errId });
  control.setAttribute("aria-describedby", `${hint ? hintId : ""} ${errId}`.trim());
  const el = h("div", { class: "field d-field" }, h("label", { for: control.id, text: label }), control,
    hint ? h("span", { class: "hint", id: hintId, text: hint }) : null, err);
  return {
    el,
    setErr(text) {
      err.textContent = text || "";
      if (text) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid");
    },
  };
}

const textInput = (value = "", attrs = {}) => h("input", { type: "text", class: "input", autocomplete: "off", spellcheck: "false", value, ...attrs });
function typeSelect(types, value) {
  const sel = h("select", { class: "input" }, types.map((t) => h("option", { value: t, text: D.typeLabel[t] || t })));
  sel.value = value;
  return sel;
}
/** 검사 코드 → 글(구조 변경 폼의 것 먼저, 없으면 값 검사의 것) */
function problemText(p) {
  const m = D.structCheck[p.code] || D.check[p.code];
  return typeof m === "function" ? m(p.arg) : m || p.code;
}

/** 카드(제목 + 닫기 + 본문). 구조 변경 폼들이 같은 모양을 쓴다 */
function card(title, onClose, ...children) {
  return h("section", { class: "a-card d-add", "aria-label": title },
    h("div", { class: "a-head d-add-head" }, h("h3", { text: title }), btn(D.close, onClose)), ...children);
}

/* ---------------- 열 추가 ---------------- */

/** 열 추가 폼(admin). PATCH /config {ops: [column.add], revision} → 작업. 결과에 새 열이 안 나오는 sql: 데이터셋을 알린다 */
export function addColumnCard(t, { alive, onClose, onDone }) {
  const name = textInput("", { name: "name", maxlength: 63 });
  const type = typeSelect(St.TYPES, "text");
  const req = checkbox(D.colRequired, false, D.colRequiredHint);
  const def = textInput("", { name: "default" });
  const values = textInput("", { name: "values" });
  const desc = textInput("", { name: "description" });
  const rows = {
    name: fieldRow(D.colName, name, D.nameRule),
    type: fieldRow(D.type, type),
    default: fieldRow(D.defaultValue, def, D.defaultHint),
    values: fieldRow(D.enumValues, values, D.enumValuesHint),
    description: fieldRow(D.description, desc),
  };
  const out = h("div", { class: "d-out" });
  const sync = () => { rows.values.el.hidden = type.value !== "enum"; };
  type.addEventListener("change", sync);
  sync();
  const submit = h("button", { type: "submit", class: "btn primary" }, D.addColumnSubmit);
  const form = h("form", { class: "d-form", novalidate: true },
    h("div", { class: "d-fields" }, rows.name.el, rows.type.el, rows.values.el, rows.default.el, rows.description.el),
    req.el, h("div", { class: "a-actions" }, submit, btn(D.cancel, onClose)));
  let busy = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    Object.values(rows).forEach((r) => r.setErr(null));
    const made = St.columnAddOp(t.name, { name: name.value, type: type.value, required: req.input.checked, default: def.value,
      values: values.value, description: desc.value }, t.columns.map((c) => c.name));
    if (made.problems.length) {
      for (const p of made.problems) (rows[p.field] || rows.name).setErr(problemText(p));
      const first = { name, type, default: def, values, description: desc }[made.problems[0].field] || name;
      first.focus();
      return;
    }
    busy = true;
    submit.disabled = true;
    try {
      const result = await runJob(async () => api("PATCH", `${BASE}/config`, { ops: [made.op], revision: t.revision }), out, alive);
      if (!result || !alive()) return;
      onDone(configResult(result, D.columnAdded(made.op.column)));
    } catch (err) {
      if (alive()) out.replaceChildren(configFail(err));
    } finally {
      busy = false;
      submit.disabled = false;
    }
  });
  const el = card(D.addColumnTitle(t.name), onClose,
    h("p", { class: "a-note", text: t.mode === "append" ? D.addColumnIntroAppend : D.addColumnIntroReplace }), form, out);
  return { el, focus: () => name.focus() };
}

/* ---------------- 열 규칙 고치기 ---------------- */

/** 열 규칙 고치기(admin): 설명, enum 값, min·max·pattern, 유형 넓히기. 유형을 바꾸면 쓰이는 곳(차트)을 먼저 보이고 확인을 받는다 */
export function columnRuleCard(t, col, { alive, onClose, onDone }) {
  const desc = textInput(col.description || "", { name: "description" });
  const ctl = { description: desc };
  const rows = { description: fieldRow(D.description, desc) };
  if (col.type === "enum") {
    ctl.values = textInput((col.values || []).join(", "), { name: "values" });
    rows.values = fieldRow(D.enumValues, ctl.values, D.enumValuesHint);
  }
  if (["integer", "number", "date"].includes(col.type)) {
    ctl.min = textInput(col.min ?? "", { name: "min" });
    ctl.max = textInput(col.max ?? "", { name: "max" });
    rows.min = fieldRow(D.ruleMin, ctl.min);
    rows.max = fieldRow(D.ruleMax, ctl.max);
  }
  if (col.type === "text") {
    ctl.pattern = textInput(col.pattern || "", { name: "pattern" });
    rows.pattern = fieldRow(D.rulePatternLabel, ctl.pattern, D.patternHint);
  }
  const wider = St.widenTargets(col.type);
  if (wider.length && !col.primary_key) {
    ctl.type = typeSelect([col.type, ...wider], col.type);
    rows.type = fieldRow(D.type, ctl.type, D.widenHint);
  }
  const impact = h("div");
  const confirm = checkbox(D.typeChangeConfirm, false);
  confirm.el.hidden = true;
  if (ctl.type) {
    ctl.type.addEventListener("change", async () => {
      const changing = ctl.type.value !== col.type;
      confirm.el.hidden = !changing;
      confirm.input.checked = false;
      if (!changing) { impact.replaceChildren(); return; }
      impact.replaceChildren(h("div", { class: "sk-note", role: "status", text: T.loading }));
      try {
        const used = await api("GET", `${tablePath(t.name)}/used_by`);
        if (alive()) impact.replaceChildren(typeImpact(used));
      } catch (err) { if (alive()) impact.replaceChildren(box("bad", dataFailText(err))); }
    });
  }
  const out = h("div", { class: "d-out" });
  const submit = h("button", { type: "submit", class: "btn primary" }, D.saveRules);
  const form = h("form", { class: "d-form", novalidate: true }, h("div", { class: "d-fields" }, Object.values(rows).map((r) => r.el)),
    impact, confirm.el, h("div", { class: "a-actions" }, submit, btn(D.cancel, onClose)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    Object.values(rows).forEach((r) => r.setErr(null));
    const form0 = Object.fromEntries(Object.entries(ctl).map(([k, c]) => [k, c.value]));
    const made = St.columnUpdateOp(t.name, col, form0);
    if (made.problems.length) {
      for (const p of made.problems) (rows[p.field] || rows.description).setErr(problemText(p));
      (ctl[made.problems[0].field] || desc).focus();
      return;
    }
    if (!made.op) { out.replaceChildren(box("neutral", D.noChanges)); return; }
    if (made.typeChanged && !confirm.input.checked) { out.replaceChildren(box("warn", D.typeChangeNeedsConfirm)); confirm.input.focus(); return; }
    submit.disabled = true;
    try {
      const result = await runJob(async () => api("PATCH", `${BASE}/config`, { ops: [made.op], revision: t.revision }), out, alive);
      if (!result || !alive()) return;
      onDone(configResult(result, D.columnUpdated(col.name)));
    } catch (err) {
      if (alive()) out.replaceChildren(configFail(err));
    } finally {
      submit.disabled = false;
    }
  });
  const el = card(D.ruleTitle(`${t.name}.${col.name}`), onClose, h("p", { class: "a-note", text: D.ruleIntro }), form, out);
  return { el, focus: () => desc.focus() };
}

/** 유형을 바꿀 때 영향받는 그래프(쓰이는 곳의 차트·섹션). 계보는 열 단위가 아니라 테이블 단위라 넓게 보인다 */
function typeImpact(used) {
  const groups = St.usageByDataset(used);
  const charts = groups.flatMap((g) => g.charts.map((c) => `${c.title || c.key} (${g.dataset})`));
  const sections = groups.flatMap((g) => g.sections.map((s) => D.sectionAt(s.tab, s.index, g.dataset)));
  if (!charts.length && !sections.length) return box("neutral", D.typeImpactNone);
  return box("warn", D.typeImpactTitle, { lines: [...charts, ...sections] });
}

/* ---------------- 쓰이는 곳 ---------------- */

/** "쓰이는 곳" 탭: 그 테이블을 읽는 데이터셋 → 그래프(차트·위젯 섹션) → 탭(GET /tables/{t}/used_by). operator도 본다 */
export function usagePanel(t, alive) {
  const wrap = h("div", { class: "d-stack gap" }, h("div", { class: "sk-note", role: "status", text: T.loading }));
  api("GET", `${tablePath(t.name)}/used_by`).then((used) => {
    if (!alive()) return;
    const groups = St.usageByDataset(used);
    const parts = [h("p", { class: "a-note", text: D.usageIntro })];
    if (!groups.length) parts.push(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: D.usageNone })));
    for (const g of groups) {
      parts.push(h("section", { class: "a-card d-usage" },
        h("h3", {}, D.usageDataset, " ", h("a", { class: "a-link", href: `#dataset/${encodeURIComponent(g.dataset)}`, text: g.dataset })),
        g.charts.length || g.sections.length
          ? h("ul", { class: "d-lines" },
            g.charts.map((c) => h("li", {}, h("strong", { text: c.title || c.key }), " ", h("small", { class: "a-key", text: D.chartIn(c.dashboard, c.tabs) }))),
            g.sections.map((s) => h("li", { text: D.sectionAt(s.tab, s.index, s.type) })))
          : h("p", { class: "a-note", text: D.usageNoGraphs })));
    }
    if (used.tabs && used.tabs.length) parts.push(h("p", { class: "a-note", text: D.usageTabs(used.tabs) }));
    if (used.unknown_datasets && used.unknown_datasets.length) parts.push(box("neutral", D.usageUnknown(used.unknown_datasets)));
    wrap.replaceChildren(...parts);
  }).catch((err) => { if (alive()) wrap.replaceChildren(box("bad", dataFailText(err))); });
  return wrap;
}

/* ---------------- 테이블 삭제, 합치기 ---------------- */

/** 테이블 삭제(admin): 이름을 다시 입력받고 PATCH /config table.remove. 쓰는 곳이 있으면 409 in_use(+errors) */
export function dropTableCard(t, { alive, onClose, onDone }) {
  const confirmName = textInput("", { name: "confirm" });
  const row = fieldRow(D.dropConfirmLabel(t.name), confirmName);
  const out = h("div", { class: "d-out" });
  const submit = h("button", { type: "submit", class: "btn danger" }, D.dropTable);
  const form = h("form", { class: "d-form", novalidate: true }, row.el, h("div", { class: "a-actions" }, submit, btn(D.cancel, onClose)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (confirmName.value.trim() !== t.name) { row.setErr(D.dropMismatch); confirmName.focus(); return; }
    row.setErr(null);
    submit.disabled = true;
    try {
      const result = await runJob(async () => api("PATCH", `${BASE}/config`, { ops: [{ op: "table.remove", table: t.name }], revision: t.revision }), out, alive);
      if (!result || !alive()) return;
      onDone(configResult(result, D.tableDropped(t.name)));
    } catch (err) {
      if (alive()) out.replaceChildren(configFail(err));
    } finally {
      submit.disabled = false;
    }
  });
  const el = card(D.dropTitle(t.name), onClose, h("p", { class: "a-note", text: D.dropIntro }), form, out);
  el.classList.add("danger-zone");
  return { el, focus: () => confirmName.focus() };
}

/** 배치 합치기(admin, append 테이블): 되돌릴 수 없다는 확인 → POST /tables/{t}/compact → 작업 */
export async function compactTable(t, out, alive) {
  if (!window.confirm(D.compactConfirm(t.name))) return null;
  try {
    const r = await runJob(() => api("POST", `${tablePath(t.name)}/compact`, {}), out, alive);
    if (!r || !alive()) return null;
    const done = r.file ? box("good", D.compacted(r.merged, r.file), { lines: r.rows != null ? [D.compactedRows(r.rows)] : [] }) : box("neutral", D.compactNothing);
    out.replaceChildren(done);
    return r;
  } catch (err) {
    if (alive()) out.replaceChildren(configFail(err));
    return null;
  }
}

/* ---------------- 새 테이블 마법사 ---------------- */

/** 새 테이블(admin): CSV 하나 놓기 → POST /tables/infer → 추론 표 고치기 → POST /tables → 작업 → 결과(만든 것·sync) */
export function newTableWizard({ alive, onClose, onCreated }) {
  // 마법사를 연 때의 설정 판. 그 사이 다른 사람이 설정을 바꿨으면 서버가 409 stale_config로 알린다
  const revAtOpen = currentRevision().catch(() => null);
  const out = h("div", { class: "d-out" });
  const stepBox = h("div");
  let busy = false;

  async function onFiles(files) {
    if (busy) return;
    const file = files[0];
    if (!/\.csv$/i.test(file.name)) { out.replaceChildren(box("bad", D.csvOnly)); return; }
    busy = true;
    out.replaceChildren(h("div", { class: "sk-note", role: "status", text: D.inferring }));
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const inferred = await apiForm(`${BASE}/tables/infer`, form);
      if (!alive()) return;
      out.replaceChildren();
      stepBox.replaceChildren(reviewStep(inferred));
      const first = stepBox.querySelector("input[name=table]");
      if (first) first.focus();
    } catch (err) {
      if (alive()) out.replaceChildren(box("bad", dataFailText(err), { lines: (err.body && err.body.errors) || [] }));
    } finally {
      busy = false;
    }
  }

  function reviewStep(inf) {
    const spec = JSON.parse(JSON.stringify(inf.table));      // 사람이 고치는 사본
    const name = textInput(spec.name, { name: "table", maxlength: 63 });
    const nameRow = fieldRow(D.newTableName, name, D.nameRule);
    name.addEventListener("input", () => { spec.name = name.value; });
    const mode = h("select", { class: "input", name: "mode" }, ["append", "replace"].map((m) => h("option", { value: m, text: D.modeLabel[m] })));
    mode.value = inf.mode || "append";
    const conflict = h("select", { class: "input", name: "on_conflict" }, ["error", "skip", "update"].map((c) => h("option", { value: c, text: D.conflictLabel[c] })));
    conflict.value = inf.on_conflict || "error";
    const modeRow = fieldRow(D.mode, mode, D.modeHint);
    const conflictRow = fieldRow(D.conflictPolicy, conflict);
    mode.addEventListener("change", () => { conflictRow.el.hidden = mode.value !== "append"; });
    const dataset = checkbox(D.makeDataset, inf.dataset !== false, D.makeDatasetHint);
    const suggest = checkbox(D.makeDraft, inf.suggest === true, D.makeDraftHint);
    const colErr = h("div");

    const body = h("tbody");
    const drawColumns = () => {
      body.replaceChildren(...spec.columns.map((c, i) => {
        const cn = textInput(c.name, { "aria-label": D.colNameOf(c.source || c.name), maxlength: 63 });
        cn.addEventListener("input", () => { spec.primary_key = St.renameInKey(spec.primary_key, c.name, cn.value); c.name = cn.value; });
        const ty = typeSelect(St.TYPES, c.type);
        ty.setAttribute("aria-label", D.colTypeOf(c.source || c.name));
        const vals = textInput((c.values || []).join(", "), { "aria-label": D.enumValuesOf(c.source || c.name) });
        vals.hidden = c.type !== "enum";
        ty.addEventListener("change", () => { c.type = ty.value; vals.hidden = c.type !== "enum"; });
        vals.addEventListener("input", () => { c.values = St.splitValues(vals.value); });
        const rq = h("input", { type: "checkbox", "aria-label": D.colRequiredOf(c.source || c.name) });
        rq.checked = !!c.required;
        rq.addEventListener("change", () => { c.required = rq.checked; });
        const pk = h("input", { type: "checkbox", "aria-label": D.colKeyOf(c.source || c.name) });
        pk.checked = spec.primary_key.includes(c.name);
        pk.addEventListener("change", () => {
          spec.primary_key = pk.checked ? [...spec.primary_key.filter((k) => k !== c.name), c.name] : spec.primary_key.filter((k) => k !== c.name);
        });
        const notes = [c.basis, c.ref ? D.refNote(c.ref) : null].filter(Boolean).join(" · ");
        return h("tr", {},
          h("td", { class: "mono", text: c.source || "" }),
          h("td", {}, cn),
          h("td", {}, h("span", { class: "d-stack" }, ty, vals)),
          h("td", { class: "pick" }, rq),
          h("td", { class: "pick" }, pk),
          h("td", { class: "wrap" }, h("small", { class: "a-key", text: notes })));
      }));
    };
    drawColumns();
    const columnsTable = h("div", { class: "a-table-wrap" }, h("table", { class: "a-table d-infer" },
      h("thead", {}, h("tr", {}, [D.sourceHeader, D.colName, D.type, D.colRequiredShort, D.primaryKey, D.inferBasis].map((l) => h("th", { scope: "col", text: l })))),
      body));

    const submit = h("button", { type: "submit", class: "btn primary" }, D.createTable);
    const form = h("form", { class: "d-form", novalidate: true },
      h("p", { class: "a-note", text: D.inferredFrom(inf.source_name, spec.rows) }),
      h("div", { class: "d-fields" }, nameRow.el, modeRow.el, conflictRow.el),
      columnsTable, colErr, dataset.el, suggest.el,
      h("div", { class: "a-actions" }, submit, btn(D.cancel, onClose)));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      nameRow.setErr(null);
      colErr.replaceChildren();
      const made = St.tableForCreate(spec);
      if (made.problems.length) {
        const nameP = made.problems.find((p) => p.field === "name");
        if (nameP) { nameRow.setErr(problemText(nameP)); name.focus(); }
        const rest = made.problems.filter((p) => p.field !== "name");
        if (rest.length) colErr.replaceChildren(box("bad", D.fixColumns, { lines: rest.map((p) => (p.index != null ? `${spec.columns[p.index].source || spec.columns[p.index].name}: ` : "") + problemText(p)) }));
        return;
      }
      busy = true;
      submit.disabled = true;
      const send = async () => api("POST", `${BASE}/tables`, {
        upload_id: inf.upload_id, table: made.table, mode: mode.value, on_conflict: mode.value === "append" ? conflict.value : null,
        dataset: dataset.input.checked, suggest: suggest.input.checked, revision: await revAtOpen,
      });
      try {
        const result = await runJob(send, out, alive);
        if (!result || !alive()) return;
        stepBox.replaceChildren();
        const created = result.table || made.table.name;
        out.replaceChildren(configResult(result, D.tableCreated(created),
          [h("a", { class: "btn small primary", href: `#table/${encodeURIComponent(created)}` }, D.openNewTable)]));
        onCreated(result);
      } catch (err) {
        if (alive()) out.replaceChildren(configFail(err, { retry: () => form.requestSubmit() }));
      } finally {
        busy = false;
        submit.disabled = false;
      }
    });
    return form;
  }

  const drop = h("div", { class: "d-drop" }, h("p", { text: D.newTableDrop }), filePicker(D.pickFile, { onFiles }));
  fileDrop(drop, onFiles);
  stepBox.replaceChildren(drop);
  const el = card(D.newTableTitle, onClose, h("p", { class: "a-note", text: D.newTableIntro }), stepBox, out);
  el.dataset.screen = "new-table";        // e2e 캡처가 찾는다
  return { el, focus: () => { const b = drop.querySelector("button"); if (b) b.focus(); } };
}

