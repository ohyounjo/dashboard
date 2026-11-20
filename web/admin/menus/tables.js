// 운용 화면: 테이블 목록(#tables)과 테이블 상세(#table/<이름>), 상세의 "추가" 화면(설계서 9-1, 9-2. 계약 contracts/manage.md).
// 관리 서비스(/api/manage)가 떠 있을 때만 메뉴에 있다(셸이 GET /api/manage/config를 읽어 session.manage에 둔다).
// 입력 부품·형식 검사·붙여넣기 해석은 web/shared/fields.js의 등록 표에서 나온다. 테이블마다 화면 코드를 쓰지 않는다.
// 문구는 strings.js의 S.admin.data에만 둔다. 권한은 서버가 지킨다(되돌리기 버튼은 admin이거나 자기가 더한 배치에만 보인다).
import { h } from "../../common.js";
import { fileDrop, filePicker, iconButton, uid } from "../../shared/dom.js";
import * as F from "../../shared/fields.js";
import { T, view, session, ApiFail, api, apiForm, waitJob, errorText, notice, head, checkbox, pagedTable, detailId } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
const tablePath = (name) => `${BASE}/tables/${encodeURIComponent(name)}`;
const batchPath = (name, file) => `${tablePath(name)}/batches/${encodeURIComponent(file)}`;
const FIELD_TEXT = { yes: D.yes, no: D.no, choose: D.choose, empty: D.emptyValue };

let gen = 0;                 // 화면을 그릴 때마다 늘린다. 비동기 일이 끝났을 때 아직 그 화면인지 본다
let detailTab = "batches";   // 상세에서 마지막으로 연 탭(같은 메뉴 안에서 기억한다)

const isAdmin = () => ((session.manage && session.manage.role) || (session.me && session.me.role)) === "admin";
const limits = () => (session.manage && session.manage.limits) || {};
const inlineMax = () => limits().inline_max_rows || 500;

/* ---------------- 공통 조각 ---------------- */

function failText(err) {
  if (err instanceof ApiFail) {
    if (err.detail && typeof D.errors[err.detail] === "string") return D.errors[err.detail];
    if (err.detail && typeof T.errors[err.detail] === "string") return T.errors[err.detail];
    if (err.body && typeof err.body.message === "string" && err.body.message) return err.body.message;
  }
  return errorText(err);
}

const pill = (text, tone) => h("span", { class: "pill tone", "data-tone": tone, text });

/** 안내 상자: 제목 한 줄 + (줄 목록) + (버튼들). tone: good | warn | bad | primary | neutral */
function box(tone, title, { lines, more, actions, body } = {}) {
  const shown = (lines || []).slice(0, 50);
  return h("div", { class: "a-msg d-box", "data-tone": tone, role: tone === "bad" ? "alert" : "status" },
    h("strong", { text: title }),
    body || null,
    shown.length ? h("ul", { class: "d-lines" }, shown.map((l) => h("li", { text: l }))) : null,
    more ? h("p", { class: "d-more", text: more }) : null,
    actions && actions.length ? h("div", { class: "a-actions" }, actions) : null);
}

function btn(label, onclick, cls = "") {
  return h("button", { type: "button", class: `btn ${cls}`.trim(), onclick }, label);
}

function modeCell(mode, conflict) {
  return h("span", { class: "pills" }, pill(D.modeLabel[mode] || mode, mode === "append" ? "primary" : "neutral"),
    mode === "append" && conflict ? h("small", { class: "a-key", text: D.conflictLabel[conflict] || conflict }) : null);
}

const typeText = (type) => D.typeLabel[type] || type;

/** 열 규칙의 글 목록(구조 탭과 입력 칸의 도움말이 같이 쓴다). */
function ruleTexts(c, { withRequired = true } = {}) {
  const out = [];
  if (withRequired && F.isRequired(c) && !c.primary_key) out.push(D.ruleRequired);
  if (Array.isArray(c.values)) out.push(D.ruleValues(c.values.map(String)));
  if (c.ref) out.push(D.ruleRef(String(c.ref)));
  if (c.min != null || c.max != null) out.push(D.ruleRange(c.min, c.max));
  if (c.pattern != null) out.push(D.rulePattern(String(c.pattern)));
  return out;
}

function checkText(problem) {
  const m = D.check[problem.code];
  return typeof m === "function" ? m(problem.arg) : m || problem.code;
}

/** 진행 중인 작업 한 줄(GET /jobs/{id}의 progress.step). */
function jobText(job) {
  const p = (job && job.progress) || {};
  const label = D.jobStep[p.step] || D.jobRunning;
  return p.rows != null ? `${label} · ${D.rowsRead(p.rows)}` : label;
}

/** 탭 묶음(role=tablist). 화살표·Home·End로 옮긴다. onSelect(key)를 부른다. 돌려주는 것: {el, select(key)} */
function tabStrip(label, items, current, onSelect) {
  const buttons = new Map();
  const el = h("div", { class: "d-tabs", role: "tablist", "aria-label": label });
  const select = (key, focus = false) => {
    for (const [k, b] of buttons) {
      b.setAttribute("aria-selected", String(k === key));
      b.tabIndex = k === key ? 0 : -1;
    }
    if (focus) buttons.get(key).focus();
    onSelect(key);
  };
  items.forEach((it) => {
    const b = h("button", { type: "button", role: "tab", id: it.tabId, "aria-controls": it.panelId, text: it.label, onclick: () => select(it.key) });
    buttons.set(it.key, b);
    el.append(b);
  });
  el.addEventListener("keydown", (e) => {
    const keys = items.map((it) => it.key);
    const at = keys.findIndex((k) => buttons.get(k) === document.activeElement);
    if (at < 0) return;
    let to = null;
    if (e.key === "ArrowRight") to = (at + 1) % keys.length;
    else if (e.key === "ArrowLeft") to = (at - 1 + keys.length) % keys.length;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = keys.length - 1;
    if (to === null) return;
    e.preventDefault();
    select(keys[to], true);
  });
  for (const [k, b] of buttons) { b.setAttribute("aria-selected", String(k === current)); b.tabIndex = k === current ? 0 : -1; }
  return { el, select };
}

/* ---------------- 목록 ---------------- */

function listView() {
  let cache = null;
  const table = pagedTable({
    defaultSort: "name",
    empty: D.noTables,
    load: async (st) => {
      if (!cache) cache = (await api("GET", `${BASE}/tables`)).items || [];
      const from = (st.page - 1) * st.size;
      return { items: cache.slice(from, from + st.size), total: cache.length };
    },
    rowHref: (t) => `#table/${encodeURIComponent(t.name)}`,
    columns: [
      { key: "name", label: D.name, wrap: true, render: (t) => h("span", { class: "d-stack" },
        h("a", { class: "a-link", href: `#table/${encodeURIComponent(t.name)}`, text: t.name }),
        t.description ? h("small", { class: "a-key", text: t.description }) : null) },
      { key: "mode", label: D.mode, render: (t) => modeCell(t.mode, t.on_conflict) },
      { key: "rows", label: D.rows, num: true, render: (t) => (t.rows == null ? "—" : D.num(t.rows)) },
      { key: "last", label: D.lastBatch, render: (t) => (t.last_batch
        ? h("span", { class: "d-stack" },
          h("span", { class: "num", title: t.last_batch.file, text: t.last_batch.added_at ? T.fmtDateTime(t.last_batch.added_at) : t.last_batch.file }),
          t.last_batch.actor_email ? h("small", { class: "a-key", text: t.last_batch.actor_email }) : null)
        : "—") },
      { key: "batches", label: D.batchCount, num: true, render: (t) => (t.mode !== "append" ? "—"
        : h("span", { class: "pills" }, D.num(t.batches), t.compact_hint ? pill(D.compactHint, "warn") : null)) },
    ],
  });
  const refresh = btn(D.refresh, () => { cache = null; table.state.page = 1; table.draw(); });
  view().replaceChildren(head(D.listTitle, D.listIntro(session.manage ? session.manage.project : ""), refresh), table.el);
  table.draw();
}

/* ---------------- 상세 ---------------- */

async function detailView(name, my) {
  const alive = () => my === gen;
  const crumb = h("a", { class: "a-crumb", href: "#tables" }, "← ", D.backToList);
  view().replaceChildren(crumb, h("div", { class: "sk-note", text: T.loading }));
  let t;
  try {
    t = await api("GET", tablePath(name));
  } catch (err) {
    if (alive()) view().replaceChildren(crumb, box("bad", failText(err)));
    return;
  }
  if (!alive()) return;

  const info = h("dl", { class: "a-infos" },
    infoRow(D.mode, modeCell(t.mode, t.on_conflict)),
    infoRow(D.primaryKey, h("span", { class: "mono", text: (t.primary_key || []).join(", ") || "—" })),
    t.checks && t.checks.length ? infoRow(D.checks, t.checks.join(", ")) : null,
    infoRow(D.datasets, (t.datasets || []).join(", ") || "—"));

  const tabsBox = h("div");
  const addBox = h("div", { hidden: true });
  const panel = h("div", { class: "d-panel", role: "tabpanel", id: uid("panel"), tabindex: "0" });
  const ids = { columns: uid("tab"), batches: uid("tab") };
  const draw = (key) => {
    detailTab = key;
    panel.setAttribute("aria-labelledby", ids[key]);
    if (key === "columns") panel.replaceChildren(columnsPanel(t));
    else panel.replaceChildren(batchesPanel(t, alive));
  };
  const strip = tabStrip(D.detailTabs, [
    { key: "columns", label: D.tabColumns, tabId: ids.columns, panelId: panel.id },
    { key: "batches", label: D.tabBatches, tabId: ids.batches, panelId: panel.id },
  ], detailTab, draw);
  tabsBox.append(strip.el, panel);

  let addBtn = null;
  const closeAdd = () => {
    addBox.hidden = true;
    addBox.replaceChildren();
    tabsBox.hidden = false;
    if (addBtn) addBtn.hidden = false;
    draw(detailTab);          // 넣거나 취소했을 수 있으니 다시 읽는다
    if (addBtn) addBtn.focus();
  };
  if (t.mode === "append") {
    addBtn = btn(D.add, () => {
      tabsBox.hidden = true;
      addBtn.hidden = true;
      addBox.replaceChildren(addScreen(t, alive, closeAdd));
      addBox.hidden = false;
      const first = addBox.querySelector("[role=tab][aria-selected=true]");
      if (first) first.focus();
    }, "primary");
  }
  view().replaceChildren(crumb,
    head(t.name, t.description || (t.mode === "append" ? null : D.replaceNote), addBtn),
    h("div", { class: "a-card" }, info), h("div", { class: "a-gap" }), addBox, tabsBox);
  draw(detailTab);
}

function infoRow(label, value) {
  return h("div", { class: "a-info" }, h("dt", { text: label }), h("dd", {}, value));
}

function columnsPanel(t) {
  const rows = t.columns.map((c) => h("tr", {},
    h("td", {}, h("span", { class: "d-stack" }, h("span", { class: "mono", text: c.name }), c.label ? h("small", { class: "a-key", text: c.label }) : null)),
    h("td", {}, typeText(c.type)),
    h("td", { class: "wrap" }, h("span", { class: "pills" }, c.primary_key ? pill(D.primaryKey, "primary") : null,
      F.isRequired(c) && !c.primary_key ? pill(D.ruleRequired, "neutral") : null),
    " ", ruleTexts(c, { withRequired: false }).join(" · ")),
    h("td", { class: "wrap" }, c.description || "—"),
    h("td", {}, c.default == null ? "—" : h("span", { class: "mono", text: F.display(c, String(c.default), FIELD_TEXT) })),
    h("td", { class: "wrap" }, c.aliases && c.aliases.length ? c.aliases.join(", ") : "—")));
  return h("div", { class: "a-table-wrap" }, h("table", { class: "a-table" },
    h("thead", {}, h("tr", {}, [D.column, D.type, D.rules, D.description, D.defaultValue, D.aliases].map((l) => h("th", { scope: "col", text: l })))),
    h("tbody", {}, rows)));
}

function batchesPanel(t, alive) {
  const note = notice();
  let cache = null;
  const reload = () => { cache = null; table.state.page = 1; table.draw(); };
  const canChange = (b) => isAdmin() || b.mine;

  async function act(b, kind, trigger) {
    const ask = kind === "remove" ? D.confirmRemove(b.file) : D.confirmRestore(b.file);
    if (!window.confirm(ask)) return;
    trigger.disabled = true;
    note.set(kind === "remove" ? D.removing : D.restoring, "primary");
    try {
      let r = kind === "remove" ? await api("DELETE", batchPath(t.name, b.file), {}) : await api("POST", `${batchPath(t.name, b.file)}/restore`, {});
      if (r && r.job) {
        r = await waitJob(r.job.id, { alive, onTick: (job) => note.set(jobText(job), "primary") });
        if (r === null) return;
      }
      if (!alive()) return;
      note.set(kind === "restore" ? D.restored(b.file) : r && r.fast ? D.removedFast(b.file, r.rows) : D.removedRebuild(b.file), "good");
      reload();
    } catch (err) {
      if (!alive()) return;
      trigger.disabled = false;
      note.set(failText(err), "bad");
    }
  }

  const table = pagedTable({
    defaultSort: "file",
    empty: D.noBatches,
    load: async (st) => {
      if (!cache) cache = (await api("GET", `${tablePath(t.name)}/batches?removed=1`)).items || [];
      const from = (st.page - 1) * st.size;
      return { items: cache.slice(from, from + st.size), total: cache.length };
    },
    columns: [
      { key: "file", label: D.file, render: (b) => h("span", { class: "d-stack" }, h("span", { class: "mono", text: b.file }),
        h("span", { class: "pills" }, b.first && t.mode === "append" ? pill(D.firstBatch, "neutral") : null, b.removed ? pill(D.removedBatch, "bad") : null,
          !b.removed && !b.recorded ? pill(D.notApplied, "warn") : null)) },
      { key: "added_at", label: D.addedAt, render: (b) => h("span", { class: "num", text: b.added_at ? T.fmtDateTime(b.added_at) : "—" }) },
      { key: "rows", label: D.rows, num: true, render: (b) => (b.rows == null ? "—" : h("span", { class: "d-stack end" }, D.num(b.rows),
        h("small", { class: "a-key", title: D.rowPartsTitle, text: D.rowParts(b) }))) },
      { key: "who", label: D.addedBy, wrap: true, render: (b) => h("span", { class: "d-stack" }, b.actor_email || "—",
        b.method || b.source_name ? h("small", { class: "a-key", text: [D.methodLabel[b.method] || b.method, b.source_name].filter(Boolean).join(" · ") }) : null) },
      { key: "act", label: D.actions, render: (b) => {
        const parts = [h("a", { class: "btn small", href: batchPath(t.name, b.file), download: "" }, D.download)];
        if (canChange(b) && b.removed) {
          const r = btn(D.restore, () => act(b, "restore", r), "small");
          parts.push(r);
        } else if (canChange(b) && !b.first) {
          const r = btn(D.remove, () => act(b, "remove", r), "small danger");
          parts.push(r);
        }
        return h("span", { class: "a-actions d-nowrap" }, parts);
      } },
    ],
  });
  table.draw();
  return h("div", {}, h("p", { class: "a-note", text: t.mode === "append" ? D.batchesIntro : D.batchesIntroReplace }), h("div", { class: "a-gap" }), note.el, table.el);
}

/* ---------------- 추가(9-2) ---------------- */

function addScreen(t, alive, onClose) {
  const panels = {};
  const ids = { form: uid("tab"), grid: uid("tab"), file: uid("tab") };
  const panel = h("div", { class: "d-panel", role: "tabpanel", id: uid("panel"), tabindex: "-1" });
  let strip = null;
  const show = (key) => {
    if (!panels[key]) {
      const ctx = { alive, toFileTab: () => strip.select("file", true) };
      panels[key] = key === "form" ? formPanel(t, ctx) : key === "grid" ? gridPanel(t, ctx) : filePanel(t, ctx);
    }
    panel.setAttribute("aria-labelledby", ids[key]);
    panel.replaceChildren(panels[key]);
  };
  strip = tabStrip(D.addTabs, [
    { key: "form", label: D.tabForm, tabId: ids.form, panelId: panel.id },
    { key: "grid", label: D.tabGrid, tabId: ids.grid, panelId: panel.id },
    { key: "file", label: D.tabFile, tabId: ids.file, panelId: panel.id },
  ], "form", show);
  show("form");
  return h("section", { class: "a-card d-add", "aria-label": D.addTitle(t.name) },
    h("div", { class: "a-head d-add-head" }, h("h3", { text: D.addTitle(t.name) }), btn(D.close, onClose)),
    h("p", { class: "a-note", text: D.addIntro(D.conflictLabel[t.on_conflict] || t.on_conflict || "") }),
    strip.el, panel);
}

/** 미리보기 → 넣기 → 결과·방금 추가 취소(세 탭이 같이 쓴다).
 *  src: {kind: "rows", rows, method} | {kind: "file", file}. onCells(오류 줄들)은 칸 오류를 그 칸에 보이게 한다. */
function makeFlow(t, { alive, out, onCells = () => {}, onDone = () => {}, toFileTab }) {
  const path = `${tablePath(t.name)}/batches`;
  let state = { src: null, opts: {}, preview: null };
  // 요청 세대 번호. 미리보기·넣기를 새로 시작할 때마다 오르고, 늦게 온 앞 요청의 응답은 버린다
  let gen = 0;
  const busy = (text) => out.replaceChildren(h("div", { class: "sk-note", role: "status", text }));
  const settle = (el) => { out.replaceChildren(el); el.tabIndex = -1; el.focus({ preventScroll: false }); };

  async function send(src, { dry, columns, force }, prev = null, my = gen) {
    // 이 요청이 아직 마지막 요청일 때만 화면을 바꾸고 작업을 지켜본다(늦게 온 앞 요청의 진행 표시가 결과를 덮지 않게)
    const current = () => alive() && my === gen;
    const show = (text) => { if (current()) busy(text); };
    const extra = { ...(columns ? { columns } : {}), ...(force ? { force: true } : {}) };
    if (src.kind === "rows") return api("POST", path, { rows: src.rows, method: src.method, dry_run: dry, ...extra });
    let r = null;
    if (!dry && prev && prev.upload_id) {
      try {
        r = await api("POST", path, { upload_id: prev.upload_id, ...extra });
      } catch (err) {
        if (!(err instanceof ApiFail && err.detail === "upload_expired")) throw err;
        r = null;          // 잡아 둔 파일이 없어졌다 → 같은 파일을 다시 올려 넣는다(미리보기는 이미 봤다)
      }
    }
    if (!r) {
      show(D.uploading);
      const form = new FormData();
      form.append("file", src.file, src.file.name);
      if (dry) form.append("dry_run", "1");
      if (force) form.append("force", "1");
      if (columns) form.append("columns", JSON.stringify(columns));
      r = await apiForm(path, form);
    }
    if (!r || !r.job) return r;
    show(jobText(r.job));
    return waitJob(r.job.id, { alive: current, onTick: (job) => show(jobText(job)) });
  }

  async function preview(src, opts = {}) {
    const my = ++gen;
    state = { src, opts, preview: null };
    onCells([]);
    busy(src.kind === "file" ? D.uploading : D.previewing);
    let r;
    try {
      r = await send(src, { dry: true, ...opts }, null, my);
    } catch (err) {
      if (alive() && my === gen) failed(err, "preview");
      return;
    }
    if (!alive() || !r || my !== gen) return;
    state.preview = r;
    const b = (r.batches || [])[0] || {};
    onCells(b.errors || []);
    // 넣기 버튼은 이 미리보기에 쓴 입력·옵션·업로드에 묶는다(그 뒤 입력이 바뀌어도 본 것만 넣는다)
    const shown = { src, opts, preview: r };
    const commitBtn = r.ok ? btn(D.insert, () => commit(shown.src, shown.opts, shown.preview), "primary") : null;
    settle(resultBox(r, { preview: true, actions: [commitBtn, btn(D.cancel, () => { onCells([]); out.replaceChildren(); })] }));
  }

  async function commit(src = state.src, opts = state.opts, prev = null) {
    const my = ++gen;
    state = { src, opts, preview: prev };
    busy(D.inserting);
    let r;
    try {
      r = await send(src, { dry: false, ...opts }, prev, my);
    } catch (err) {
      if (alive() && my === gen) failed(err, "commit");
      return null;
    }
    if (!alive() || !r || my !== gen) return null;
    onCells([]);
    const b = (r.batches || [])[0] || {};
    const undo = b.file ? btn(D.undoJust, () => undoBatch(b.file, undo)) : null;
    settle(resultBox(r, { preview: false, actions: [undo] }));
    onDone(r);
    return r;
  }

  async function undoBatch(file, trigger) {
    if (!window.confirm(D.confirmUndo(file))) return;
    trigger.disabled = true;
    busy(D.undoing);
    try {
      let r = await api("DELETE", batchPath(t.name, file), {});
      if (r && r.job) {
        r = await waitJob(r.job.id, { alive, onTick: (job) => busy(jobText(job)) });
        if (r === null) return;
      }
      if (!alive()) return;
      settle(box("good", r && r.fast ? D.undoneFast(r.rows) : D.undoneRebuild));
    } catch (err) {
      if (alive()) settle(box("bad", failText(err)));
    }
  }

  function failed(err, step) {
    const body = (err instanceof ApiFail && err.body) || {};
    if (err instanceof ApiFail && err.detail === "unknown_columns" && Array.isArray(body.columns)) {
      settle(headerMatchBox(body.columns));
      return;
    }
    const lines = Array.isArray(body.errors) ? body.errors.map(String) : [];
    onCells(lines);
    const actions = [];
    if (err instanceof ApiFail && err.detail === "duplicate_file") {
      actions.push(btn(D.forceInsert, () => {
        const opts = { ...state.opts, force: true };
        if (step === "preview") preview(state.src, opts); else commit(state.src, opts, state.preview);
      }, "primary"));
    }
    if (err instanceof ApiFail && err.detail === "too_many_rows" && toFileTab) actions.push(btn(D.toFileTab, toFileTab));
    const count = typeof body.error_count === "number" && body.error_count > lines.length ? D.moreErrors(body.error_count) : null;
    settle(box("bad", failText(err), {
      body: err instanceof ApiFail && err.detail === "duplicate_file" ? h("p", { text: D.duplicateBody }) : null,
      lines, more: count, actions,
    }));
  }

  /** 422 unknown_columns: 맞지 않은 머리글마다 있는 열을 고르거나 버린다 → columns로 다시 미리보기 */
  function headerMatchBox(unknown) {
    const picks = unknown.map((u) => {
      const id = uid("map");
      const candidates = (u.candidates || []).map(String);
      const rest = t.columns.map((c) => c.name).filter((n) => !candidates.includes(n));
      const sel = h("select", { class: "input", id },
        h("option", { value: "", text: D.choose }),
        candidates.length ? h("optgroup", { label: D.candidates }, candidates.map((n) => h("option", { value: `c:${n}`, text: n }))) : null,
        h("optgroup", { label: D.otherColumns }, rest.map((n) => h("option", { value: `c:${n}`, text: n }))),
        h("option", { value: "drop", text: D.dropHeader }));
      if (candidates.length === 1) sel.value = `c:${candidates[0]}`;
      return { header: String(u.header), sel, el: h("div", { class: "field" }, h("label", { for: id, text: D.headerLabel(String(u.header) || D.blankHeader) }), sel) };
    });
    const warn = h("div");
    const again = btn(D.retryWithMatch, () => {
      const map = { ...(state.opts.columns || {}) };
      for (const p of picks) {
        if (!p.sel.value) { warn.replaceChildren(box("warn", D.pickEveryHeader)); p.sel.focus(); return; }
        map[p.header] = p.sel.value === "drop" ? null : p.sel.value.slice(2);
      }
      preview(state.src, { ...state.opts, columns: map });
    }, "primary");
    return box("warn", D.unknownColumnsTitle, {
      body: h("div", { class: "d-stack gap" }, h("p", { text: D.unknownColumnsBody }), h("div", { class: "d-fields" }, picks.map((p) => p.el)), warn),
      actions: [again, btn(D.cancel, () => out.replaceChildren())],
    });
  }

  // 입력을 비우거나 바꾸면 진행 중인 요청을 무효로 한다(늦게 온 응답이 비운 화면을 다시 채우지 않게)
  return { preview, commit, reset: () => { gen += 1; state = { src: null, opts: {}, preview: null }; onCells([]); out.replaceChildren(); } };
}

/** 미리보기·결과 상자(contracts/ingest.md "결과"). */
function resultBox(r, { preview, actions }) {
  const b = (r.batches || [])[0] || {};
  const rows = b.rows || {};
  const ok = r.ok !== false;
  const title = preview ? (ok ? D.previewOk : D.previewBad) : D.doneTitle(b.file || "");
  const counts = h("dl", { class: "a-infos d-counts" },
    ["total", "inserted", "updated", "skipped", "unchanged", "invalid"].filter((k) => rows[k] != null && (k === "total" || rows[k] > 0 || k === "inserted"))
      .map((k) => infoRow(D.count[k], h("span", { class: "num", text: D.num(rows[k]) }))));
  const notes = [...D.normalizedLines(b.normalized || {}), ...D.columnLines(b.columns || {})];
  for (const f of r.applied_first || []) notes.push(D.appliedFirst(f.file, f.rows));
  if (r.recovered && r.recovered.length) notes.push(D.recovered(r.recovered.length));
  const changes = (b.changes || []).slice(0, 20);
  const errs = (b.errors || []).map(String);
  return box(ok ? (preview ? "primary" : "good") : "bad", title, {
    body: h("div", { class: "d-stack gap" },
      counts,
      notes.length ? h("ul", { class: "d-lines" }, notes.map((n) => h("li", { text: n }))) : null,
      changes.length ? changesTable(changes) : null,
      errs.length ? h("div", {}, h("strong", { text: D.errorsTitle(b.error_count || errs.length) }), h("ul", { class: "d-lines mono" }, errs.slice(0, 50).map((e) => h("li", { text: e })))) : null),
    actions: (actions || []).filter(Boolean),
  });
}

function changesTable(changes) {
  const trs = [];
  for (const ch of changes) {
    const cols = Object.keys({ ...(ch.before || {}), ...(ch.after || {}) });
    cols.forEach((c, i) => trs.push(h("tr", {},
      i === 0 ? h("td", { rowspan: cols.length, class: "mono", text: String(ch.key) }) : null,
      h("td", { class: "mono", text: c }),
      h("td", { class: "wrap", text: valueText((ch.before || {})[c]) }),
      h("td", { class: "wrap", text: valueText((ch.after || {})[c]) }))));
  }
  return h("div", {}, h("strong", { text: D.changesTitle(changes.length) }),
    h("div", { class: "a-table-wrap d-gap-top" }, h("table", { class: "a-table" },
      h("thead", {}, h("tr", {}, [D.key, D.column, D.before, D.after].map((l) => h("th", { scope: "col", text: l })))),
      h("tbody", {}, trs))));
}

const valueText = (v) => (v == null || v === "" ? D.emptyValue : String(v));

/* ---------- 직접 입력 ---------- */

function formPanel(t, { alive, toFileTab }) {
  const out = h("div", { class: "d-out" });
  const fields = t.columns.map((c) => {
    const id = uid("in");
    const hintId = `${id}-hint`, errId = `${id}-err`;
    const input = F.createInput(c, { text: FIELD_TEXT, attrs: { id, "aria-describedby": `${hintId} ${errId}`, name: c.name } });
    if (F.isRequired(c)) input.el.setAttribute("aria-required", "true");
    input.set(F.initialValue(c));
    const err = h("span", { class: "d-err", id: errId });
    const hints = [c.description, D.formatHint[c.type], ...ruleTexts(c, { withRequired: false })].filter(Boolean);
    const setErr = (text) => {
      err.textContent = text || "";
      if (text) input.el.setAttribute("aria-invalid", "true"); else input.el.removeAttribute("aria-invalid");
    };
    // 손대지 않은 빈 칸은 칸을 떠날 때 "필수" 오류를 내지 않는다(저장할 때 낸다). 지나가기만 해도 오류가 끼어들면
    // 그 아래 버튼이 밀려 누르려던 것을 놓친다
    let touched = false;
    const check = (soft = false) => {
      const raw = input.get();
      if (soft && !touched && String(raw).trim() === "" && !input.invalid()) { setErr(null); return true; }
      const p = input.invalid() || F.checkValue(c, raw);
      setErr(p ? checkText(p) : null);
      return !p;
    };
    input.el.addEventListener("input", () => { touched = true; });
    input.el.addEventListener(input.el.type === "checkbox" || input.el.tagName === "SELECT" ? "change" : "blur", () => check(true));
    const reset = () => { input.set(F.initialValue(c)); setErr(null); touched = false; };
    const label = h("label", { for: id }, c.label || c.name, c.label ? h("small", { class: "a-key mono", text: ` ${c.name}` }) : null,
      F.isRequired(c) ? h("span", { class: "d-req", text: ` ${D.requiredMark}` }) : null);
    const control = input.el.type === "checkbox" ? h("span", { class: "check" }, input.el, h("span", { text: D.yes })) : input.el;
    const el = h("div", { class: "field d-field" }, label, control, h("span", { class: "hint", id: hintId, text: hints.join(" · ") }), err);
    return { c, input, check, setErr, reset, el };
  });

  const keep = checkbox(D.keepValues, false);
  const form = h("form", { class: "d-form", novalidate: true }, h("div", { class: "d-fields" }, fields.map((f) => f.el)));
  const flow = makeFlow(t, {
    alive, out, toFileTab,
    onCells: (lines) => {
      const { cells } = F.splitErrors(lines);
      fields.forEach((f) => f.setErr(null));
      for (const e of cells) {
        const f = fields.find((x) => x.c.name === e.column);
        if (f && e.row === 0) f.setErr(e.message);
      }
    },
  });

  const row = () => F.rowFromValues(fields.map((f) => f.c.name), fields.map((f) => f.input.get()));
  const valid = () => {
    const bad = fields.filter((f) => !f.check());
    if (bad.length) { bad[0].input.focus(); return false; }
    return true;
  };
  let saving = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (saving || !valid()) return;
    saving = true;
    submit.disabled = true;
    const r = await flow.commit({ kind: "rows", rows: [row()], method: "form" }, {});
    saving = false;
    submit.disabled = false;
    if (!r || !alive()) return;
    if (!keep.input.checked) fields.forEach((f) => f.reset());
    fields[0].input.focus();
  });
  // 고르기 칸·체크 칸에서도 Enter로 저장한다(글 칸은 브라우저가 form을 보낸다)
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    const el = e.target;
    if (el.tagName === "SELECT" || (el.tagName === "INPUT" && el.type === "checkbox")) { e.preventDefault(); form.requestSubmit(); }
  });
  const submit = h("button", { type: "submit", class: "btn primary" }, D.saveContinue);
  const previewBtn = btn(D.preview, () => { if (valid()) flow.preview({ kind: "rows", rows: [row()], method: "form" }); });
  const clear = btn(D.clearForm, () => { fields.forEach((f) => f.reset()); flow.reset(); fields[0].input.focus(); });
  form.append(h("div", { class: "a-actions d-form-actions" }, submit, previewBtn, clear, keep.el));
  return h("div", { class: "d-stack gap" }, h("p", { class: "a-note", text: D.formIntro }), form, out);
}

/* ---------- 여러 행 ---------- */

function gridPanel(t, { alive, toFileTab }) {
  const cols = t.columns;
  const index = new Map(cols.map((c, i) => [c.name, i]));
  const pk = cols.filter((c) => c.primary_key).map((c) => c.name);
  const blank = () => cols.map(() => "");
  let rows = [blank(), blank(), blank(), blank(), blank()];
  let active = new Set(cols.map((c) => c.name));
  let cellErrors = new Map();           // "<행>:<열 이름>" → 글
  let sentRows = [];                    // 보낸 행 번호 → 표의 행 번호
  const lists = cols.map((c) => F.datalistFor(c, FIELD_TEXT));
  const note = notice();
  const errorsBox = h("div");
  const wrap = h("div", { class: "a-table-wrap d-grid-wrap" });
  const out = h("div", { class: "d-out" });
  const errKey = (r, name) => `${r}:${name}`;
  const filled = (row) => row.some((v) => String(v || "").trim() !== "");
  const filledCount = () => rows.filter(filled).length;

  function cellInput(r, ci) {
    const c = cols[ci];
    const input = F.createInput(c, { cell: true, text: FIELD_TEXT, attrs: {
      "aria-label": D.cellLabel(r + 1, c.label || c.name), list: lists[ci] ? lists[ci].id : null,
      "data-r": String(r), "data-c": String(ci), disabled: !active.has(c.name),
    } });
    input.set(rows[r][ci]);
    const msg = cellErrors.get(errKey(r, c.name));
    if (msg) { input.el.setAttribute("aria-invalid", "true"); input.el.title = msg; }
    input.el.addEventListener("input", () => { rows[r][ci] = input.get(); });
    input.el.addEventListener("blur", () => {
      if (!input.el.isConnected || !rows[r]) return;      // 표를 다시 그리며 떼어 낸 칸(붙여넣기·행 지우기)
      const p = filled(rows[r]) ? F.checkValue(c, rows[r][ci]) : null;
      setCellError(r, c.name, p ? checkText(p) : null, input.el);
    });
    return input.el;
  }

  function setCellError(r, name, text, el) {
    const key = errKey(r, name);
    if (text) cellErrors.set(key, text); else cellErrors.delete(key);
    if (el) {
      if (text) { el.setAttribute("aria-invalid", "true"); el.title = text; } else { el.removeAttribute("aria-invalid"); el.removeAttribute("title"); }
    }
    drawErrors();
  }

  function drawErrors() {
    if (!cellErrors.size) { errorsBox.replaceChildren(); return; }
    const items = [...cellErrors.entries()].sort((a, b) => Number(a[0].split(":")[0]) - Number(b[0].split(":")[0])).slice(0, 50);
    errorsBox.replaceChildren(box("bad", D.cellErrorsTitle(cellErrors.size), {
      body: h("ul", { class: "d-lines" }, items.map(([key, text]) => {
        const [r, name] = [Number(key.split(":")[0]), key.slice(key.indexOf(":") + 1)];
        return h("li", {}, h("button", { type: "button", class: "d-linkish", onclick: () => focusCell(r, index.get(name)) }, D.cellAt(r + 1, name)), `: ${text}`);
      })),
    }));
  }

  function focusCell(r, ci) {
    const el = wrap.querySelector(`input[data-r="${r}"][data-c="${ci}"]`);
    if (el) el.focus();
  }

  function draw() {
    const head = h("tr", {}, h("th", { scope: "col", class: "num", text: "#" }),
      cols.map((c) => h("th", { scope: "col", class: active.has(c.name) ? null : "d-off", title: active.has(c.name) ? null : D.notSentColumn },
        h("span", { class: "d-stack" }, c.label || c.name, c.label ? h("small", { class: "a-key mono", text: c.name }) : null,
          F.isRequired(c) ? h("small", { class: "d-req", text: D.requiredMark }) : null))),
      h("th", { scope: "col" }, h("span", { class: "sr-only", text: D.actions })));
    const body = rows.map((row, r) => h("tr", {},
      h("td", { class: "num a-key", text: String(r + 1) }),
      cols.map((c, ci) => h("td", { class: "d-cell" }, cellInput(r, ci))),
      h("td", {}, iconButton("trash", D.removeRow(r + 1), { cls: "small", onclick: () => removeRow(r) }))));
    wrap.replaceChildren(h("table", { class: "a-table d-grid" }, h("thead", {}, head), h("tbody", {}, body)), ...lists.filter(Boolean));
    drawErrors();
    allCols.hidden = active.size === cols.length;
    over();
  }

  function over() {
    const n = filledCount();
    const tooMany = n > inlineMax();
    previewBtn.disabled = tooMany;
    overBox.replaceChildren(tooMany ? box("warn", D.tooManyRows(n, inlineMax()), { actions: [btn(D.toFileTab, toFileTab)] }) : "");
  }

  function removeRow(r) {
    rows.splice(r, 1);
    if (!rows.length) rows.push(blank());
    cellErrors = new Map();
    flow.reset();
    draw();
    focusCell(Math.min(r, rows.length - 1), 0);
  }

  function applyPaste(grid, r0, c0) {
    if (!grid.length) return;
    let data = grid;
    let colAt;                 // 붙여 넣은 j번째 칸 → 표의 열 번호(-1이면 버린다)
    let firstCol = c0;
    const notes = [];
    if (F.looksLikeHeader(grid[0], cols)) {
      const names = F.matchHeader(grid[0], cols);
      const map = names.map((n) => (n == null ? -1 : index.get(n)));
      colAt = (j) => (j < map.length ? map[j] : -1);
      firstCol = Math.max(0, Math.min(...map.filter((m) => m >= 0)));
      const dropped = grid[0].filter((x, i) => names[i] == null && String(x).trim() !== "");
      active = new Set([...names.filter(Boolean), ...pk]);
      data = grid.slice(1);
      notes.push(D.headerMatched(names.filter(Boolean).length));
      if (dropped.length) notes.push(D.headerDropped(dropped));
      if (active.size < cols.length) notes.push(D.partialColumns);
    } else {
      colAt = (j) => (c0 + j < cols.length ? c0 + j : -1);
      if (Math.max(...grid.map((g) => g.length)) + c0 > cols.length) notes.push(D.extraCellsIgnored);
    }
    data.forEach((cells, i) => {
      const r = r0 + i;
      while (rows.length <= r) rows.push(blank());
      cells.forEach((v, j) => {
        const ci = colAt(j);
        if (ci >= 0) rows[r][ci] = v;
      });
    });
    cellErrors = new Map();
    flow.reset();
    draw();
    note.set([D.pasted(data.length), ...notes].join(" "), "primary");
    focusCell(r0, firstCol);
  }

  wrap.addEventListener("paste", (e) => {
    const target = e.target.closest && e.target.closest("input[data-r]");
    if (!target) return;
    const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
    if (!/[\t\n]/.test(text.replace(/\r?\n$/, ""))) return;      // 한 칸짜리는 브라우저가 그대로 붙인다
    e.preventDefault();
    applyPaste(F.parseTsv(text), Number(target.dataset.r), Number(target.dataset.c));
  });

  const flow = makeFlow(t, {
    alive, out, toFileTab,
    onCells: (lines) => {
      const { cells } = F.splitErrors(lines);
      cellErrors = new Map();
      for (const e of cells) {
        const r = sentRows[e.row];
        if (r === undefined) continue;
        cellErrors.set(errKey(r, e.column && index.has(e.column) ? e.column : cols[0].name), e.message);
      }
      draw();
    },
  });

  const previewBtn = btn(D.preview, () => {
    const names = cols.filter((c) => active.has(c.name)).map((c) => c.name);
    const sent = [];
    sentRows = [];
    cellErrors = new Map();
    rows.forEach((row, r) => {
      if (!filled(row)) return;
      for (const name of names) {
        const p = F.checkValue(cols[index.get(name)], row[index.get(name)]);
        if (p) cellErrors.set(errKey(r, name), checkText(p));
      }
      sentRows.push(r);
      sent.push(F.rowFromValues(names, names.map((n) => row[index.get(n)])));
    });
    draw();
    if (!sent.length) { note.set(D.noRows, "warn"); return; }
    if (cellErrors.size) { note.set(D.fixCells, "bad"); const [first] = cellErrors.keys(); focusCell(Number(first.split(":")[0]), index.get(first.slice(first.indexOf(":") + 1))); return; }
    note.set(null);
    flow.preview({ kind: "rows", rows: sent, method: "paste" });
  }, "primary");
  const addRow = btn(D.addRow, () => { rows.push(blank()); draw(); focusCell(rows.length - 1, 0); });
  const clearAll = btn(D.clearRows, () => {
    if (filledCount() && !window.confirm(D.confirmClearRows)) return;
    rows = [blank(), blank(), blank(), blank(), blank()];
    active = new Set(cols.map((c) => c.name));
    cellErrors = new Map();
    flow.reset();
    note.set(null);
    draw();
  });
  const allCols = btn(D.sendAllColumns, () => { active = new Set(cols.map((c) => c.name)); note.set(null); draw(); });
  const overBox = h("div");
  draw();
  return h("div", { class: "d-stack gap" }, h("p", { class: "a-note", text: D.gridIntro(inlineMax()) }), note.el, overBox, wrap, errorsBox,
    h("div", { class: "a-actions" }, previewBtn, addRow, clearAll, allCols), out);
}

/* ---------- 파일 ---------- */

function filePanel(t, { alive }) {
  const out = h("div", { class: "d-out" });
  const note = notice();
  const chosen = h("p", { class: "a-note", role: "status" });
  const flow = makeFlow(t, { alive, out });
  const onFiles = (files) => {
    if (files.length > 1) note.set(D.oneFileOnly, "warn"); else note.set(null);
    const file = files[0];
    if (!/\.csv$/i.test(file.name)) { note.set(D.csvOnly, "bad"); return; }
    chosen.textContent = D.fileChosen(file.name, D.size(file.size));
    flow.preview({ kind: "file", file });
  };
  const drop = h("div", { class: "d-drop" }, h("p", { text: D.dropHere }), filePicker(D.pickFile, { onFiles }),
    h("small", { class: "a-key", text: D.fileLimits(limits().max_upload_mb || 200, limits().max_rows || 2000000) }));
  fileDrop(drop, onFiles);
  return h("div", { class: "d-stack gap" }, h("p", { class: "a-note", text: D.fileIntro }), note.el, drop, chosen, out);
}

/* ---------------- 메뉴 ---------------- */

export default {
  key: "tables",
  label: T.menu.tables,
  match: /^(tables|table\/.+)$/,
  when: () => !!session.manage,
  render(hash) {
    gen += 1;
    const name = detailId(hash, "table");
    if (name !== null) detailView(name, gen);
    else listView();
  },
};
