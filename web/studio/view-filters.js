// 필터 화면: 대시보드의 필터 순서(끌어다 놓기와 키보드), 추가·편집·삭제.
// 저장 방식(탭 구성 화면과 같다): 순서 변경·삭제는 바로 연산 하나, 폼은 "저장"을 눌러야 나간다(바뀐 필드만, 지운 필드는 null).
// 유형별로 쓰는 필드는 contracts/chart-shorthand.md의 "필터" 표를 따른다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { h, uid, field, select, button, iconButton, badge, alertBox, stateBox, clone } from "./dom.js";
import { diffValues, KEY_PATTERN, TIME_GRAINS } from "./chart-form.js";
import { pickDashboard, dashboardSelect, saver } from "./screen-kit.js";

const TYPES = ["select", "time_range", "number_range", "time_grain"];   // 계약 스키마 $defs.filter.type
const DEFAULT_TYPE = "select";
// 유형별로 쓰는 필드(공통: label, type, default, charts)
const USES = { select: ["dataset", "column", "multi", "parent"], number_range: ["dataset", "column"], time_grain: ["dataset"], time_range: [] };
const NUMERIC = ["integer", "number"];
const blank = (v) => v === undefined || v === null || v === "";

export function mount(container, ctx) {
  let dash = pickDashboard(ctx);
  let sel = null;          // { kind: "filter", key } | { kind: "new" }
  let pending = null;      // 필터에서 key를 뺀 초안
  let original = null;
  let keyBox = null;
  let dirty = false;
  let listError = null, formError = null;
  let drag = null;         // 끌고 있는 필터의 key
  let focusAfter = null;
  let alive = true;        // 화면을 떠나면 false(떠난 뒤 끝난 저장이 같이 쓰는 컨테이너를 다시 그리지 않게)
  const save = saver(container, ctx, () => alive);

  const board = () => ((ctx.config.dashboard && ctx.config.dashboard.dashboards) || {})[dash] || null;
  const filters = () => (board() && Array.isArray(board().filters) ? board().filters : []);
  const byKey = (key) => filters().find((f) => f && f.key === key) || null;
  const charts = () => (board() && Array.isArray(board().charts) ? board().charts : []);
  const datasets = () => Object.keys((ctx.config.dashboard && ctx.config.dashboard.datasets) || {});
  const columnsOf = (ds) => {
    const cols = ds && ctx.config.columns ? ctx.config.columns[ds] : null;
    return Array.isArray(cols) ? cols.map((c) => (typeof c === "string" ? { name: c } : c)) : null;
  };
  const typeOf = (f) => (f && TYPES.includes(f.type) ? f.type : DEFAULT_TYPE);
  const uses = (name) => USES[typeOf(pending)].includes(name);

  /* ---------------- 선택과 초안 ---------------- */

  function leave() {
    if (dirty && !window.confirm(S.discardConfirm)) return false;
    dirty = false; formError = null;
    return true;
  }

  function resetSelection() {
    dirty = false; formError = null; keyBox = null;
    const f = sel && sel.kind === "filter" ? byKey(sel.key) : null;
    if (!f) { sel = null; pending = original = null; return; }
    const { key, ...rest } = f;
    pending = clone(rest); original = clone(rest);
  }

  function pick(next) {
    if (save.busy || !leave()) return;
    sel = next;
    listError = null;
    if (next && next.kind === "new") { keyBox = { value: "" }; pending = { label: "", type: DEFAULT_TYPE, dataset: datasets()[0] || "" }; original = null; dirty = true; }
    else resetSelection();
    render();
    const box = next && container.querySelector(".inspector");
    if (box && box.getBoundingClientRect().top > window.innerHeight - 160) box.scrollIntoView({ block: "start" });
    if (next && next.kind === "new") { const el = container.querySelector(".inspector input"); if (el) el.focus(); }
  }

  /* ---------------- 저장 ---------------- */

  async function send(ops, { message, onDone, form = false }) {
    if (save.busy) return;
    listError = null; formError = null;
    const res = await save.send(ops);
    if (!alive || !res) return;   // 화면을 떠났다: 잠금은 풀렸고, 다시 그리지 않는다
    if (res.ok) {
      if (onDone) onDone();
      if (message) ctx.announce(message);
    } else {
      if (res.info.detail === "stale_config") { resetSelection(); form = false; }   // 설정을 새로 읽었다 → 초안을 버린다
      if (form) formError = res.info; else { listError = res.info; focusAfter = null; }
      ctx.announce(res.info.title);
    }
    render();
  }

  function moveFilter(key, to) {
    const from = filters().findIndex((f) => f.key === key);
    if (from < 0 || to < 0 || to >= filters().length || to === from) return;
    if (!leave()) return;
    focusAfter = `filter:${key}`;
    send([{ op: "filter.move", dashboard: dash, key, index: to }], { message: S.filterMoved(byKey(key).label, to + 1, filters().length), onDone: resetSelection });
  }

  function removeFilter(key) {
    const f = byKey(key);
    if (!f) return;
    // 지우는 대상이 지금 고치는 것이 아니면, 고치던 초안을 버릴지 먼저 묻는다(삭제 뒤 초안을 서버 값으로 다시 읽으므로)
    if (!(sel && sel.key === key) && !leave()) return;
    if (!window.confirm(S.filterRemoveConfirm(f.label))) return;
    dirty = false;
    send([{ op: "filter.remove", dashboard: dash, key }], { message: S.filterRemoved(f.label), onDone: () => { if (sel && sel.key === key) sel = null; resetSelection(); } });
  }

  /** 보낼 값: 이 유형이 쓰지 않는 필드와 빈 값을 뺀다 */
  function cleaned() {
    const out = {};
    const type = typeOf(pending);
    for (const [k, v] of Object.entries(pending)) {
      if (blank(v)) continue;
      if (["dataset", "column", "multi", "parent"].includes(k) && !USES[type].includes(k)) continue;
      if (k === "default" && Array.isArray(v)) {
        const list = type === "number_range" ? v : v.filter((x) => !blank(x));
        if (!list.length || (type === "number_range" && list.every(blank))) continue;
        out[k] = clone(list); continue;
      }
      if (k === "charts" && (!Array.isArray(v) || !v.length)) continue;
      out[k] = clone(v);
    }
    return out;
  }

  function saveForm() {
    if (!sel || save.busy) return;
    const value = cleaned();
    const problems = [];
    if (!String(value.label || "").trim()) problems.push(S.filterLabelEmpty);
    if (sel.kind === "new") {
      const key = keyBox.value.trim();
      if (!KEY_PATTERN.test(key)) problems.push(S.filterKeyInvalid);
      else if (byKey(key)) problems.push(S.filterKeyTaken(key));
      if (problems.length) { formError = { title: S.errInvalid, body: null, errors: problems }; ctx.announce(S.errInvalid); render(); return; }
      focusAfter = `filter:${key}`;
      send([{ op: "filter.add", dashboard: dash, filter: { key, ...value } }], { form: true, message: S.filterAdded(value.label), onDone: () => { sel = { kind: "filter", key }; resetSelection(); } });
      return;
    }
    if (problems.length) { formError = { title: S.errInvalid, body: null, errors: problems }; ctx.announce(S.errInvalid); render(); return; }
    const values = diffValues(original, value);
    if (!Object.keys(values).length) { dirty = false; render(); return; }
    focusAfter = `filter:${sel.key}`;
    send([{ op: "filter.update", dashboard: dash, key: sel.key, values }], { form: true, message: S.saved, onDone: resetSelection });
  }

  /* ---------------- 끌어다 놓기 ---------------- */

  const clearMarks = () => container.querySelectorAll(".drop-before, .drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
  function slotOf(e) {
    const rows = Array.from(container.querySelectorAll(".filter-list > .sec:not(.is-pending)"));
    let slot = 0;
    for (const row of rows) { const r = row.getBoundingClientRect(); if (e.clientY > r.top + r.height / 2) slot++; }
    return { rows, slot };
  }
  function onDragOver(e) {
    if (!drag) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const { rows, slot } = slotOf(e);
    clearMarks();
    if (!rows.length) return;
    if (slot < rows.length) rows[slot].classList.add("drop-before"); else rows[rows.length - 1].classList.add("drop-after");
  }
  function onDrop(e) {
    const key = drag;
    drag = null;
    if (!key) return;
    e.preventDefault();
    const { slot } = slotOf(e);
    clearMarks();
    const from = filters().findIndex((f) => f.key === key);
    moveFilter(key, slot > from ? slot - 1 : slot);
  }

  /* ---------------- 그리기 ---------------- */

  function filterRow(f, i, n) {
    const selected = sel && sel.kind === "filter" && sel.key === f.key;
    const type = typeOf(f);
    const aim = [f.dataset, f.column].filter(Boolean).join(".");
    const row = h("li", { class: `sec${selected ? " is-selected" : ""}`, tabindex: "0", "data-focus": `filter:${f.key}`, "data-filter": f.key,
      "aria-label": S.filterAria(f.label, S.filterTypes[type], i + 1, n), "aria-current": selected ? "true" : null },
      h("span", { class: "grip", "aria-hidden": "true" }, icon("grip")),
      h("span", { class: "sec-main" }, badge(S.filterTypes[type], type === DEFAULT_TYPE ? "neutral" : "primary"),
        h("span", { class: "sec-name", text: f.label }), h("span", { class: "mono muted sec-key", text: aim ? `#${f.key} · ${aim}` : `#${f.key}` })),
      h("span", { class: "row-actions" },
        iconButton("up", S.moveUp, { cls: "ghost", disabled: i === 0, onclick: (e) => { e.stopPropagation(); moveFilter(f.key, i - 1); } }),
        iconButton("down", S.moveDown, { cls: "ghost", disabled: i === n - 1, onclick: (e) => { e.stopPropagation(); moveFilter(f.key, i + 1); } }),
        iconButton("trash", S.remove, { cls: "ghost", onclick: (e) => { e.stopPropagation(); removeFilter(f.key); } })));
    row.addEventListener("click", () => pick({ kind: "filter", key: f.key }));
    row.addEventListener("keydown", (e) => {
      if (e.target !== row) return;
      if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); moveFilter(f.key, i + (e.key === "ArrowUp" ? -1 : 1)); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick({ kind: "filter", key: f.key }); }
    });
    row.setAttribute("draggable", "true");
    row.addEventListener("dragstart", (e) => {
      if (save.busy) { e.preventDefault(); return; }
      drag = f.key;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", f.label || f.key);
      row.classList.add("is-dragging");
    });
    row.addEventListener("dragend", () => { drag = null; row.classList.remove("is-dragging"); clearMarks(); });
    return row;
  }

  /** 열의 유형이 숫자면 입력한 글자를 숫자로 바꾼다(그 밖에는 글자 그대로) */
  function typed(textValue) {
    const col = (columnsOf(pending.dataset) || []).find((c) => c.name === pending.column);
    const t = textValue.trim();
    return col && NUMERIC.includes(col.type) && t !== "" && !Number.isNaN(Number(t)) ? Number(t) : textValue;
  }

  function defaultEditor(touch, redraw) {
    const type = typeOf(pending);
    const cur = pending.default;
    if (type === "time_range") {
      const el = h("input", { type: "text", class: "input", autocomplete: "off", value: typeof cur === "string" ? cur : "", "data-path": "default" });
      el.addEventListener("input", () => { if (el.value) pending.default = el.value; else delete pending.default; touch(); });
      return [field(S.filterDefault, el, { hint: S.filterDefaultRangeHint }),
        h("p", { class: "note-line", role: "note" }, icon("info"), h("span", { text: S.filterEmbedNote }))];
    }
    if (type === "time_grain") {
      return [field(S.filterDefault, select(TIME_GRAINS.map((g) => ({ value: g, label: S.grainLabel(g) })), typeof cur === "string" ? cur : "",
        { "data-path": "default", placeholder: S.none, onchange: (v) => { if (v) pending.default = v; else delete pending.default; touch(); } }))];
    }
    if (type === "number_range") {
      const pair = Array.isArray(cur) ? [cur[0] ?? null, cur[1] ?? null] : [null, null];
      const input = (n, label) => {
        const el = h("input", { type: "number", class: "input", step: "any", value: pair[n] ?? "", "data-path": `default.${n}`, "aria-label": label, placeholder: label });
        el.addEventListener("input", () => {
          pair[n] = el.value === "" || Number.isNaN(Number(el.value)) ? null : Number(el.value);
          if (pair[0] === null && pair[1] === null) delete pending.default; else pending.default = [pair[0], pair[1]];
          touch();
        });
        return el;
      };
      return [h("div", { class: "field" }, h("span", { class: "field-label", text: S.filterDefault }), h("div", { class: "pair" }, input(0, S.filterMin), input(1, S.filterMax)),
        h("span", { class: "hint", text: S.filterDefaultNumberHint }))];
    }
    if (pending.multi === false) {
      const el = h("input", { type: "text", class: "input", autocomplete: "off", value: blank(cur) ? "" : String(Array.isArray(cur) ? cur[0] ?? "" : cur), "data-path": "default" });
      el.addEventListener("input", () => { if (el.value === "") delete pending.default; else pending.default = typed(el.value); touch(); });
      return [field(S.filterDefault, el, { hint: S.filterDefaultOneHint })];
    }
    // 여러 값: 값 목록(순서대로)
    const list = Array.isArray(cur) ? cur : blank(cur) ? [] : [cur];
    const write = (next) => { if (next.length) pending.default = next; else delete pending.default; };
    const rows = list.map((v, i) => {
      const el = h("input", { type: "text", class: "input", autocomplete: "off", value: blank(v) ? "" : String(v), "data-path": `default.${i}`, "aria-label": S.itemLabel(i + 1) });
      el.addEventListener("input", () => { const next = list.slice(); next[i] = typed(el.value); list[i] = next[i]; write(next); touch(); });
      return h("li", { class: "arr-item is-scalar" }, h("span", { class: "arr-no num", text: String(i + 1), "aria-hidden": "true" }), h("div", { class: "arr-body" }, el),
        h("div", { class: "arr-actions" }, iconButton("trash", S.itemRemove(S.filterDefault, i + 1), { onclick: () => { const next = list.slice(); next.splice(i, 1); write(next); touch(); redraw("default.__add"); } })));
    });
    return [h("fieldset", { class: "group", "data-field": "default" }, h("legend", {}, h("span", { class: "legend-name", text: S.filterDefault })),
      h("p", { class: "hint", text: S.filterDefaultManyHint }),
      rows.length ? h("ol", { class: "arr" }, rows) : h("p", { class: "hint", text: S.arrayEmpty }),
      button(S.itemAdd, { iconName: "plus", cls: "small", "data-path": "default.__add", onclick: () => { write([...list, ""]); touch(); redraw(`default.${list.length}`); } }))];
  }

  function formBody(box, touch) {
    const redraw = (focusPath) => {
      const active = focusPath || (document.activeElement && box.contains(document.activeElement) ? document.activeElement.getAttribute("data-path") : null);
      box.replaceChildren(...build());
      if (active) { const el = box.querySelector(`[data-path="${CSS.escape(active)}"]`); if (el) el.focus(); }
    };
    function build() {
      const out = [];
      const isNew = sel.kind === "new";
      if (isNew) {
        const key = h("input", { type: "text", class: "input mono", autocomplete: "off", value: keyBox.value, "data-path": "key" });
        key.addEventListener("input", () => { keyBox.value = key.value; });
        out.push(field(S.filterKey, key, { hint: S.filterKeyHint, required: true, requiredText: S.required }));
      } else {
        out.push(h("p", { class: "hint" }, S.filterKey, " ", h("span", { class: "mono", text: sel.key }), " · ", S.filterKeyFixed));
      }
      const label = h("input", { type: "text", class: "input", autocomplete: "off", value: pending.label ?? "", "data-path": "label" });
      label.addEventListener("input", () => { pending.label = label.value; touch(); });
      out.push(field(S.filterLabel, label, { hint: S.filterLabelHint, required: true, requiredText: S.required }));

      out.push(field(S.filterType, select(TYPES.map((t) => ({ value: t, label: `${S.filterTypes[t]} · ${t}` })), typeOf(pending), { "data-path": "type", onchange: (v) => {
        // 유형을 생략해 둔 필터(= select)는 그대로 생략해 둔다
        if (v === DEFAULT_TYPE && original && !("type" in original)) delete pending.type; else pending.type = v;
        for (const k of ["dataset", "column", "multi", "parent"]) if (!USES[v].includes(k)) delete pending[k];
        delete pending.default;   // 기본값의 꼴이 유형마다 다르다
        if (USES[v].includes("dataset") && blank(pending.dataset)) pending.dataset = datasets()[0] || "";
        touch(); redraw("type");
      } }), { hint: S.filterTypeHint[typeOf(pending)] }));

      if (uses("dataset")) {
        const ds = datasets().slice();
        if (!blank(pending.dataset) && !ds.includes(pending.dataset)) ds.push({ value: pending.dataset, label: S.notInList(pending.dataset) });
        out.push(field(S.filterDataset, select(ds, pending.dataset, { "data-path": "dataset", placeholder: S.chooseDataset, onchange: (v) => { pending.dataset = v; touch(); redraw("dataset"); } }),
          { hint: typeOf(pending) === "time_grain" ? S.filterDatasetGrainHint : S.filterDatasetHint, required: true, requiredText: S.required }));
      }
      if (uses("column")) {
        const cols = columnsOf(pending.dataset);
        let el, hint = S.filterColumnHint;
        if (cols && cols.length) {
          const opts = cols.map((c) => ({ value: c.name, label: c.type ? `${c.name} (${c.type})` : c.name }));
          if (!blank(pending.column) && !cols.some((c) => c.name === pending.column)) opts.push({ value: pending.column, label: S.notInList(pending.column) });
          el = select(opts, pending.column, { "data-path": "column", placeholder: S.chooseColumn, onchange: (v) => { if (v) pending.column = v; else delete pending.column; touch(); } });
        } else {
          el = h("input", { type: "text", class: "input mono", autocomplete: "off", value: pending.column ?? "", "data-path": "column" });
          el.addEventListener("input", () => { if (el.value) pending.column = el.value; else delete pending.column; touch(); });
          hint = S.noColumnList;
        }
        out.push(field(S.filterColumn, el, { hint, required: true, requiredText: S.required }));
      }
      if (uses("multi")) {
        const multi = h("input", { type: "checkbox", id: uid("multi"), checked: pending.multi !== false, "data-path": "multi" });
        multi.addEventListener("change", () => {
          // 기본값이 "여러 개"다: 켜면 키를 지우고(처음부터 true라고 적혀 있었으면 그대로), 끄면 false
          if (!multi.checked) { pending.multi = false; if (Array.isArray(pending.default)) { if (pending.default.length) pending.default = pending.default[0]; else delete pending.default; } }
          else if (original && original.multi === true) pending.multi = true; else delete pending.multi;
          touch(); redraw("multi");
        });
        out.push(h("div", { class: "field check" }, multi, h("label", { for: multi.id, text: S.filterMulti }), h("span", { class: "hint", text: S.filterMultiHint })));
      }
      out.push(...defaultEditor(touch, redraw));
      if (uses("parent")) {
        const others = filters().filter((f) => typeOf(f) === DEFAULT_TYPE && f.key !== (sel.kind === "filter" ? sel.key : null));
        const opts = others.map((f) => ({ value: f.key, label: `${f.label} · ${f.key}` }));
        if (!blank(pending.parent) && !others.some((f) => f.key === pending.parent)) opts.push({ value: pending.parent, label: S.notInList(pending.parent) });
        out.push(field(S.filterParent, select(opts, pending.parent, { "data-path": "parent", placeholder: S.none, onchange: (v) => { if (v) pending.parent = v; else delete pending.parent; touch(); } }),
          { hint: S.filterParentHint }));
      }
      // 적용할 차트: 아무것도 고르지 않으면 전체
      const picked = Array.isArray(pending.charts) ? pending.charts : [];
      const boxes = charts().map((c) => {
        const cb = h("input", { type: "checkbox", id: uid("fc"), checked: picked.includes(c.key), "data-chart": c.key });
        cb.addEventListener("change", () => {
          const now = new Set(Array.isArray(pending.charts) ? pending.charts : []);
          if (cb.checked) now.add(c.key); else now.delete(c.key);
          const known = charts().map((x) => x.key);
          const ordered = [...known.filter((k) => now.has(k)), ...[...now].filter((k) => !known.includes(k))];   // 대시보드의 차트 순서대로
          if (ordered.length) pending.charts = ordered; else delete pending.charts;
          touch(); note.textContent = S.filterChartsCount(ordered.length, charts().length);
        });
        return h("div", { class: "field check" }, cb, h("label", { for: cb.id }, h("span", { text: c.title || c.key }), " ", h("span", { class: "mono muted", text: c.key })));
      });
      const note = h("p", { class: "hint", role: "status", text: S.filterChartsCount(picked.length, charts().length) });
      out.push(h("fieldset", { class: "group", "data-field": "charts" }, h("legend", {}, h("span", { class: "legend-name", text: S.filterCharts })),
        h("p", { class: "hint", text: S.filterChartsHint }), h("div", { class: "check-list" }, boxes), note));
      return out;
    }
    redraw();
  }

  function inspector() {
    const box = h("section", { class: "panel inspector", "aria-label": S.inspector });
    if (!sel) {
      box.append(stateBox({ iconName: "filter", title: S.filterInspectorEmptyTitle, body: S.filterInspectorEmptyBody }));
      return box;
    }
    const isNew = sel.kind === "new";
    const touch = () => { if (!dirty) { dirty = true; paintActions(); } };
    const saveBtn = button(isNew ? S.filterAdd : S.save, { iconName: "check", cls: "primary", "data-action": "save", onclick: saveForm });
    const cancel = button(isNew ? S.cancel : S.revert, { "data-action": "cancel", onclick: () => {
      dirty = false; formError = null;
      if (isNew) { sel = null; pending = null; } else resetSelection();
      render();
    } });
    const note = h("span", { class: "dirty-note", role: "status" });
    function paintActions() {
      saveBtn.disabled = save.busy || (!isNew && !dirty);
      cancel.disabled = save.busy || (!isNew && !dirty);
      note.textContent = save.busy ? S.saving : dirty && !isNew ? S.unsaved : "";
    }
    paintActions();
    const formBox = h("div", { class: "schema-form" });
    formBody(formBox, touch);
    const form = h("form", { class: "form", novalidate: true },
      h("h3", { text: isNew ? S.newFilterHeading : S.filterHeading(byKey(sel.key).label) }), formBox,
      h("div", { class: "form-error" }, formError ? alertBox({ title: formError.title, body: formError.body, errors: formError.errors }) : null),
      h("div", { class: "form-actions" }, saveBtn, cancel, note));
    form.addEventListener("submit", (e) => { e.preventDefault(); saveForm(); });
    box.append(form);
    return box;
  }

  function render() {
    if (!alive) return;   // 화면을 떠난 뒤 끝난 요청은 그리지 않는다
    const head = h("div", { class: "view-head" }, h("h2", { text: S.screenFilters }), h("p", { class: "intro", text: S.filtersLead }));
    if (!board()) {
      container.replaceChildren(head, stateBox({ tone: "warn", iconName: "warning", title: S.noDashboardsTitle, body: S.layNoDashboards }));
      return;
    }
    const rows = filters().map((f, i) => filterRow(f, i, filters().length));
    if (sel && sel.kind === "new") {
      rows.push(h("li", { class: "sec is-pending" }, h("span", { class: "grip", "aria-hidden": "true" }, icon("plus")),
        h("span", { class: "sec-main" }, badge(S.filterTypes[typeOf(pending)], "warn"), h("span", { class: "sec-name", text: S.filterPending }))));
    }
    const list = h("ol", { class: "sec-list filter-list", "aria-label": S.filterList(board().title || dash) }, rows);
    list.addEventListener("dragover", onDragOver);
    list.addEventListener("drop", onDrop);

    container.replaceChildren(head,
      h("div", { class: "panel head-form" }, dashboardSelect(ctx, dash, (key) => { if (!leave()) return false; dash = pickDashboard(ctx, key); sel = null; pending = original = null; listError = null; render(); return true; })),
      h("div", { class: "tabs-grid" },
        h("div", { class: "tabs-col" },
          listError ? alertBox({ title: listError.title, body: listError.body, errors: listError.errors }) : null,
          rows.length ? h("div", { class: "tab-card" }, list) : stateBox({ iconName: "filter", title: S.noFiltersTitle, body: S.noFiltersBody }),
          h("div", { class: "list-actions" },
            button(S.filterAdd, { iconName: "plus", "data-action": "add-filter", onclick: () => pick({ kind: "new" }) }),
            h("span", { class: "hint", text: S.filterKeyboardHint }))),
        inspector()));

    if (focusAfter) {
      const el = container.querySelector(`[data-focus="${CSS.escape(focusAfter)}"]`);
      focusAfter = null;
      if (el) el.focus();
    }
    if (formError) {
      const el = container.querySelector(".form-error");
      if (el) el.scrollIntoView({ block: "nearest" });
    }
    if (listError) {
      // 옮기기·삭제의 오류는 목록 위에 나온다 → 옵션 쪽을 보고 있었더라도 보이게 한다
      const el = container.querySelector(".tabs-col > .alert");
      if (el) el.scrollIntoView({ block: "nearest" });
    }
  }

  render();
  return {
    /** 화면을 떠나도 되는지(저장하지 않은 폼). 저장 요청이 나가 있는 동안에는 묻지 않는다(그 초안은 이미 보냈다). */
    canLeave: () => save.busy || leave(),
    /** 셸이 설정을 다시 읽었다(적용 뒤: 열 목록이 DB 기준으로 바뀐다). 고치던 폼이 없을 때만 다시 그린다. */
    refresh: () => { if (!alive || save.busy || dirty) return; resetSelection(); render(); },
    destroy: () => { alive = false; },
  };
}
