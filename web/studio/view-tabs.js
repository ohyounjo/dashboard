// 탭 구성 화면: 탭·섹션 순서(끌어다 놓기와 키보드), 팔레트에서 섹션 추가, 섹션·탭 옵션 폼.
// 저장 방식: 순서 변경·삭제는 그 자리에서 바로 PATCH(연산 하나), 폼은 "저장" 버튼으로 한 번에 보낸다.
// 새 섹션·새 탭은 초안 폼을 채운 뒤 "추가"를 눌러야 저장된다(필수 옵션이 비어 있는 섹션은 검증을 통과하지 못하므로).
import S from "./strings.js";
import { icon } from "./icons.js";
import { h, uid, field, button, iconButton, badge, alertBox, stateBox, clone, same } from "./dom.js";
import { renderForm, defaultsFor, clean } from "./schema-form.js";

const SUPERSET = "superset";            // 계약 스키마의 고정 섹션 유형(위젯이 아니다)
const KEY_PATTERN = /^[a-z][a-z0-9_]{0,62}$/;

export function mount(container, ctx) {
  let sel = null;         // {kind: "tab", key} | {kind: "section", tab, index} | {kind: "newSection", tab, index, type} | {kind: "newTab", type}
  let pending = null;       // 폼 초안(섹션 옵션 또는 탭 값)
  let tabDraft = null;    // 새 탭의 {key, label, icon}
  let original = null;    // 초안의 원래 값(바뀐 것만 보내기 위해)
  let dirty = false;
  let busy = false;
  let listError = null;   // 순서 변경·삭제 오류(목록 위)
  let formError = null;   // 폼 저장 오류(폼 옆)
  let drag = null;        // 끌고 있는 것: {kind: "tab", key} | {kind: "section", tab, index} | {kind: "palette", type}
  let focusAfter = null;  // 다시 그린 뒤 포커스를 줄 요소의 data-focus 값

  const tabs = () => (ctx.config.dashboard && Array.isArray(ctx.config.dashboard.tabs) ? ctx.config.dashboard.tabs : []);
  const tabByKey = (key) => tabs().find((t) => t.key === key);
  const widgets = () => ctx.registry.widgets || [];
  const widgetOf = (type) => widgets().find((w) => w.type === type);
  const typeLabel = (type) => (type === SUPERSET ? S.supersetSection : (widgetOf(type) || {}).label || type);
  const dashboards = () => Object.keys((ctx.config.dashboard && ctx.config.dashboard.dashboards) || {});

  const formCtx = () => ({
    defs: ctx.registry.defs || null,
    datasets: Object.keys((ctx.config.dashboard && ctx.config.dashboard.datasets) || {}),
    columns: (ds) => {
      const cols = ds && ctx.config.columns ? ctx.config.columns[ds] : null;
      return Array.isArray(cols) ? cols.map((c) => (typeof c === "string" ? { name: c } : c)) : null;
    },
    icons: ctx.registry.icons || [],
    tones: ctx.registry.tones || [],
    dataset: () => (pending ? pending.dataset : null),
  });

  /** 섹션 유형의 옵션 스키마(공통 키 title 포함). 모르는 유형이면 null. */
  function sectionSchema(type) {
    const title = { title: { type: "string", description: S.sectionTitleHint } };
    if (type === SUPERSET) {
      const all = (ctx.config.dashboard && ctx.config.dashboard.dashboards) || {};
      const labels = {};
      for (const [k, d] of Object.entries(all)) labels[k] = d && d.title ? `${k} · ${d.title}` : k;
      return { type: "object", required: ["dashboard"], properties: {
        ...title,
        dashboard: { enum: dashboards(), "x-labels": labels, description: S.supersetDashboardHint },
        height: { type: "integer", minimum: 200, maximum: 5000, description: S.supersetHeightHint },
      } };
    }
    const w = widgetOf(type);
    if (!w || !w.options || !w.options.properties) return null;
    return { type: "object", required: w.options.required || [], properties: { ...title, ...w.options.properties } };
  }

  /* ---------------- 선택과 초안 ---------------- */

  function leave() {
    if (dirty && !window.confirm(S.discardConfirm)) return false;
    dirty = false; formError = null;
    return true;
  }

  function select(next) {
    if (busy || !leave()) return;
    sel = next; tabDraft = null;
    if (!next) { pending = original = null; }
    else if (next.kind === "tab") {
      const t = tabByKey(next.key);
      pending = { label: t.label, icon: t.icon }; original = clone(pending);
    } else if (next.kind === "section") {
      const { type, ...options } = tabByKey(next.tab).sections[next.index];
      pending = clone(options); original = clone(options);
    } else if (next.kind === "newSection") {
      pending = newSectionDraft(next.type); original = null; dirty = true;
    } else if (next.kind === "newTab") {
      tabDraft = { key: "", label: "", icon: (ctx.registry.icons || [])[0] || "" };
      pending = newSectionDraft(next.type); original = null; dirty = true;
    }
    render();
    // 좁은 화면에서는 옵션 폼이 목록 아래에 있다 → 고르면 폼이 보이게 한다
    const box = next && container.querySelector(".inspector");
    if (box && box.getBoundingClientRect().top > window.innerHeight - 160) box.scrollIntoView({ block: "start" });
  }

  function newSectionDraft(type) {
    const schema = sectionSchema(type);
    return schema ? defaultsFor(schema, formCtx()) : {};
  }

  /* ---------------- 저장 ---------------- */

  async function patch(ops, { message, onDone, form = false }) {
    if (busy) return;
    busy = true; listError = null; formError = null;
    container.classList.add("is-busy");
    try {
      await ctx.patch(ops);
      busy = false;
      if (onDone) onDone();
      if (message) ctx.announce(message);
    } catch (e) {
      busy = false;
      const info = ctx.describeError(e);
      if (info.detail === "stale_config") { dirty = false; if (sel && sel.kind !== "tab" && sel.kind !== "section") sel = null; resetDraftFromSelection(); form = false; }   // 설정을 새로 읽었다 → 초안을 버린다
      if (form) formError = info; else listError = info;
      ctx.announce(info.title);
    }
    container.classList.remove("is-busy");
    render();
  }

  function moveTab(key, to) {
    const from = tabs().findIndex((t) => t.key === key);
    if (from < 0 || to < 0 || to >= tabs().length || to === from) return;
    if (!leave()) return;
    const label = tabByKey(key).label;
    focusAfter = `tab:${key}`;
    patch([{ op: "tab.move", key, index: to }], { message: S.tabMoved(label, to + 1, tabs().length), onDone: () => { if (sel && sel.kind !== "tab" && sel.kind !== "section") sel = null; resetDraftFromSelection(); } });
  }

  /** 섹션을 옮긴다. toIndex는 옮긴 뒤의 위치(api.js A2). */
  function moveSection(tab, index, toTab, toIndex) {
    const src = tabByKey(tab), dst = tabByKey(toTab);
    if (!src || !dst) return;
    if (tab === toTab && toIndex === index) return;
    if (tab !== toTab && src.sections.length <= 1) {
      listError = { title: S.lastSectionTitle, body: S.lastSectionMove(src.label), errors: [] };
      ctx.announce(S.lastSectionTitle); render(); return;
    }
    if (!leave()) return;
    const op = { op: "section.move", tab, index, to_index: toIndex };
    if (toTab !== tab) op.to_tab = toTab;
    focusAfter = `sec:${toTab}:${toIndex}`;
    patch([op], {
      message: toTab === tab ? S.sectionMoved(toIndex + 1, dst.sections.length) : S.sectionMovedToTab(dst.label, toIndex + 1),
      onDone: () => { sel = { kind: "section", tab: toTab, index: toIndex }; resetDraftFromSelection(); },
    });
  }

  function resetDraftFromSelection() {
    dirty = false; formError = null; tabDraft = null;
    if (!sel) { pending = original = null; return; }
    if (sel.kind === "tab") {
      const t = tabByKey(sel.key);
      if (!t) { sel = null; pending = original = null; return; }
      pending = { label: t.label, icon: t.icon }; original = clone(pending);
    } else if (sel.kind === "section") {
      const t = tabByKey(sel.tab);
      const s = t && t.sections[sel.index];
      if (!s) { sel = null; pending = original = null; return; }
      const { type, ...options } = s;
      pending = clone(options); original = clone(options);
    }
  }

  /** 위(-1)·아래(+1)로 한 칸. 탭의 끝에서는 이웃 탭으로 넘어간다. */
  function stepSection(tab, index, dir) {
    const ti = tabs().findIndex((t) => t.key === tab);
    const t = tabs()[ti];
    const to = index + dir;
    if (to >= 0 && to < t.sections.length) return moveSection(tab, index, tab, to);
    const neighbor = tabs()[ti + dir];
    if (!neighbor) return;
    moveSection(tab, index, neighbor.key, dir < 0 ? neighbor.sections.length : 0);
  }

  function removeSection(tab, index) {
    const t = tabByKey(tab);
    if (t.sections.length <= 1) {
      listError = { title: S.lastSectionTitle, body: S.lastSectionRemove(t.label), errors: [] };
      ctx.announce(S.lastSectionTitle); render(); return;
    }
    if (!window.confirm(S.sectionRemoveConfirm(index + 1, t.label))) return;
    dirty = false;
    focusAfter = `tab:${tab}`;
    patch([{ op: "section.remove", tab, index }], { message: S.sectionRemoved, onDone: () => { sel = null; pending = original = null; } });
  }

  function removeTab(key) {
    const t = tabByKey(key);
    if (tabs().length <= 1) {
      listError = { title: S.lastTabTitle, body: S.lastTabBody, errors: [] };
      ctx.announce(S.lastTabTitle); render(); return;
    }
    if (!window.confirm(S.tabRemoveConfirm(t.label, t.sections.length))) return;
    dirty = false;
    patch([{ op: "tab.remove", key }], { message: S.tabRemoved(t.label), onDone: () => { sel = null; pending = original = null; } });
  }

  /** 팔레트 항목을 탭의 index 자리에 놓는다 → 초안 폼을 연다 */
  function startSection(type, tab, index) {
    select({ kind: "newSection", type, tab, index });
    ctx.announce(S.newSectionStarted(typeLabel(type), tabByKey(tab).label));
    focusInspector();
  }

  function focusInspector() {
    const el = container.querySelector(".inspector input, .inspector select, .inspector textarea");
    if (el) el.focus();
  }

  function saveForm() {
    if (!sel || busy) return;
    const fc = formCtx();
    if (sel.kind === "tab") {
      const values = {};
      for (const k of ["label", "icon"]) if (pending[k] !== original[k]) values[k] = pending[k];
      if (!Object.keys(values).length) return;
      focusAfter = `tab:${sel.key}`;
      return patch([{ op: "tab.update", key: sel.key, values }], { form: true, message: S.saved, onDone: resetDraftFromSelection });
    }
    const type = sel.kind === "section" ? tabByKey(sel.tab).sections[sel.index].type : sel.type;
    const schema = sectionSchema(type);
    const value = schema ? clean(pending, schema, fc) : clean(pending, {}, fc);
    if (sel.kind === "section") {
      const values = {};
      for (const k of new Set([...Object.keys(original), ...Object.keys(value)])) {
        if (!(k in value)) values[k] = null;                       // 지운 옵션
        else if (!same(value[k], original[k])) values[k] = value[k];
      }
      if (!Object.keys(values).length) { dirty = false; render(); return; }
      focusAfter = `sec:${sel.tab}:${sel.index}`;
      return patch([{ op: "section.update", tab: sel.tab, index: sel.index, values }], { form: true, message: S.saved, onDone: resetDraftFromSelection });
    }
    if (sel.kind === "newSection") {
      const { tab, index } = sel;
      focusAfter = `sec:${tab}:${index}`;
      return patch([{ op: "section.add", tab, index, section: { type, ...value } }], {
        form: true, message: S.sectionAdded(typeLabel(type), tabByKey(tab).label),
        onDone: () => { sel = { kind: "section", tab, index }; resetDraftFromSelection(); },
      });
    }
    if (sel.kind === "newTab") {
      const tab = { key: tabDraft.key.trim(), label: tabDraft.label.trim(), icon: tabDraft.icon };
      const problems = [];
      if (!KEY_PATTERN.test(tab.key)) problems.push(S.tabKeyInvalid);
      else if (tabByKey(tab.key)) problems.push(S.tabKeyTaken(tab.key));
      if (!tab.label) problems.push(S.tabLabelEmpty);
      if (problems.length) { formError = { title: S.errInvalid, body: null, errors: problems }; ctx.announce(S.errInvalid); render(); return; }
      focusAfter = `tab:${tab.key}`;
      // 탭은 섹션이 하나는 있어야 저장된다 → 탭 추가와 첫 섹션 추가를 한 요청으로 보낸다
      return patch([{ op: "tab.add", tab }, { op: "section.add", tab: tab.key, section: { type, ...value } }], {
        form: true, message: S.tabAdded(tab.label),
        onDone: () => { sel = { kind: "tab", key: tab.key }; resetDraftFromSelection(); },
      });
    }
  }

  /* ---------------- 끌어다 놓기 ---------------- */

  const clearMarks = () => container.querySelectorAll(".drop-before, .drop-after, .drop-into").forEach((el) => el.classList.remove("drop-before", "drop-after", "drop-into"));

  /** 세로로 늘어선 rows 중 포인터 y가 몇 번째 틈에 있는지(0 ~ rows.length) */
  function slotAt(rows, y) {
    let slot = 0;
    for (const row of rows) { const r = row.getBoundingClientRect(); if (y > r.top + r.height / 2) slot++; }
    return slot;
  }
  function mark(rows, slot, fallback) {
    clearMarks();
    if (!rows.length) { if (fallback) fallback.classList.add("drop-into"); return; }
    if (slot < rows.length) rows[slot].classList.add("drop-before"); else rows[rows.length - 1].classList.add("drop-after");
  }

  function target(e) {
    if (!drag) return null;
    const list = container.querySelector(".tab-list");
    if (drag.kind === "tab") {
      const cards = Array.from(list.querySelectorAll(":scope > .tab-card"));
      return { rows: cards, slot: slotAt(cards, e.clientY) };
    }
    const card = e.target instanceof Element ? e.target.closest(".tab-card") : null;
    if (!card) return null;
    const rows = Array.from(card.querySelectorAll(".sec:not(.is-pending)"));
    return { rows, slot: slotAt(rows, e.clientY), tab: card.getAttribute("data-tab"), card };
  }

  function onDragOver(e) {
    const t = target(e);
    if (!t) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = drag.kind === "palette" ? "copy" : "move";
    mark(t.rows, t.slot, t.card);
  }

  function onDrop(e) {
    const t = target(e);
    const d = drag;
    drag = null; clearMarks();
    if (!t || !d) return;
    e.preventDefault();
    if (d.kind === "tab") {
      const from = tabs().findIndex((x) => x.key === d.key);
      moveTab(d.key, t.slot > from ? t.slot - 1 : t.slot);
    } else if (d.kind === "section") {
      const to = d.tab === t.tab && t.slot > d.index ? t.slot - 1 : t.slot;
      moveSection(d.tab, d.index, t.tab, to);
    } else if (d.kind === "palette") {
      startSection(d.type, t.tab, t.slot);
    }
  }

  function draggable(el, payload, label) {
    el.setAttribute("draggable", "true");
    el.addEventListener("dragstart", (e) => {
      if (busy) { e.preventDefault(); return; }
      e.stopPropagation();
      drag = payload;
      e.dataTransfer.effectAllowed = payload.kind === "palette" ? "copy" : "move";
      e.dataTransfer.setData("text/plain", label);
      el.classList.add("is-dragging");
    });
    el.addEventListener("dragend", () => { drag = null; el.classList.remove("is-dragging"); clearMarks(); });
  }

  /* ---------------- 그리기 ---------------- */

  function paletteItem(type) {
    const label = typeLabel(type);
    const add = () => {
      const tab = sel && (sel.tab || sel.key) && tabByKey(sel.tab || sel.key) ? (sel.tab || sel.key) : tabs()[0] && tabs()[0].key;
      if (tab) startSection(type, tab, tabByKey(tab).sections.length);
    };
    // button 요소는 브라우저에 따라 끌 수 없어서(Firefox) role="button"인 div로 만든다
    const el = h("div", { class: "chip", role: "button", tabindex: "0", "data-type": type, "aria-label": S.paletteAdd(label), title: S.paletteHint, onclick: add },
      icon("grip"), h("span", { text: label }));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); add(); } });
    draggable(el, { kind: "palette", type }, label);
    return el;
  }

  function sectionRow(tab, s, i, n) {
    const selected = sel && sel.kind === "section" && sel.tab === tab.key && sel.index === i;
    const name = s.title || s.dashboard || s.dataset || "";
    const row = h("li", { class: `sec${selected ? " is-selected" : ""}`, tabindex: "0", "data-focus": `sec:${tab.key}:${i}`,
      "aria-label": S.sectionAria(typeLabel(s.type), name, i + 1, n, tab.label), "aria-current": selected ? "true" : null },
      h("span", { class: "grip", "aria-hidden": "true" }, icon("grip")),
      h("span", { class: "sec-main" }, badge(typeLabel(s.type), s.type === SUPERSET ? "primary" : "neutral"), h("span", { class: "sec-name", text: name })),
      h("span", { class: "row-actions" },
        iconButton("up", S.moveUp, { cls: "ghost", onclick: (e) => { e.stopPropagation(); stepSection(tab.key, i, -1); } }),
        iconButton("down", S.moveDown, { cls: "ghost", onclick: (e) => { e.stopPropagation(); stepSection(tab.key, i, 1); } }),
        iconButton("trash", S.remove, { cls: "ghost", onclick: (e) => { e.stopPropagation(); removeSection(tab.key, i); } })));
    row.addEventListener("click", () => select({ kind: "section", tab: tab.key, index: i }));
    row.addEventListener("keydown", (e) => {
      if (e.target !== row) return;
      if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); stepSection(tab.key, i, e.key === "ArrowUp" ? -1 : 1); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select({ kind: "section", tab: tab.key, index: i }); }
    });
    draggable(row, { kind: "section", tab: tab.key, index: i }, name || s.type);
    return row;
  }

  function tabCard(tab, i, n) {
    const selected = sel && sel.kind === "tab" && sel.key === tab.key;
    const head = h("div", { class: `tab-head${selected ? " is-selected" : ""}`, tabindex: "0", "data-focus": `tab:${tab.key}`,
      role: "group", "aria-label": S.tabAria(tab.label, i + 1, n), "aria-current": selected ? "true" : null },
      h("span", { class: "grip", "aria-hidden": "true" }, icon("grip")),
      h("span", { class: "tab-ico" }, icon(tab.icon)),
      h("span", { class: "tab-name" }, h("b", { text: tab.label }), h("span", { class: "mono muted", text: `#${tab.key}` })),
      h("span", { class: "row-actions" },
        iconButton("up", S.moveUp, { cls: "ghost", disabled: i === 0, onclick: (e) => { e.stopPropagation(); moveTab(tab.key, i - 1); } }),
        iconButton("down", S.moveDown, { cls: "ghost", disabled: i === n - 1, onclick: (e) => { e.stopPropagation(); moveTab(tab.key, i + 1); } }),
        iconButton("trash", S.remove, { cls: "ghost", onclick: (e) => { e.stopPropagation(); removeTab(tab.key); } })));
    head.addEventListener("click", () => select({ kind: "tab", key: tab.key }));
    head.addEventListener("keydown", (e) => {
      if (e.target !== head) return;
      if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); moveTab(tab.key, i + (e.key === "ArrowUp" ? -1 : 1)); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select({ kind: "tab", key: tab.key }); }
    });
    draggable(head, { kind: "tab", key: tab.key }, tab.label);

    const sections = Array.isArray(tab.sections) ? tab.sections : [];
    const rows = sections.map((s, j) => sectionRow(tab, s, j, sections.length));
    if (sel && sel.kind === "newSection" && sel.tab === tab.key) {
      rows.splice(Math.min(sel.index, rows.length), 0,
        h("li", { class: "sec is-pending" }, h("span", { class: "grip", "aria-hidden": "true" }, icon("plus")),
          h("span", { class: "sec-main" }, badge(typeLabel(sel.type), "warn"), h("span", { class: "sec-name", text: S.draftRow }))));
    }
    return h("li", { class: "tab-card", "data-tab": tab.key }, head, h("ol", { class: "sec-list", "aria-label": S.sectionsOf(tab.label) }, rows));
  }

  function iconPicker(value, onPick) {
    const name = uid("icon");
    return h("div", { class: "icon-grid", role: "radiogroup", "aria-label": S.tabIcon },
      (ctx.registry.icons || []).map((ic) => {
        const id = uid("ic");
        const input = h("input", { type: "radio", name, id, value: ic, class: "sr-only", checked: ic === value });
        input.addEventListener("change", () => onPick(ic));
        return h("span", { class: "icon-opt" }, input, h("label", { for: id, title: ic }, icon(ic), h("span", { class: "sr-only", text: ic })));
      }));
  }

  function inspector() {
    const box = h("section", { class: "panel inspector", "aria-label": S.inspector });
    if (!sel) {
      box.append(stateBox({ iconName: "sliders", title: S.inspectorEmptyTitle, body: S.inspectorEmptyBody }));
      return box;
    }
    const touch = () => { if (!dirty) { dirty = true; paintActions(); } };
    const actions = h("div", { class: "form-actions" });
    const errorBox = h("div", { class: "form-error" });
    if (formError) errorBox.append(alertBox({ title: formError.title, body: formError.body, errors: formError.errors }));

    let saveLabel = S.save, heading = "", body = [];
    if (sel.kind === "tab" || sel.kind === "newTab") {
      const t = sel.kind === "tab" ? tabByKey(sel.key) : null;
      const tv = sel.kind === "tab" ? pending : tabDraft;
      heading = t ? S.tabHeading(t.label) : S.newTabHeading;
      if (sel.kind === "newTab") {
        const key = h("input", { type: "text", class: "input mono", autocomplete: "off", value: tv.key, "data-new-tab": "key" });
        key.addEventListener("input", () => { tv.key = key.value; });
        body.push(field(S.tabKey, key, { hint: S.tabKeyHint, required: true, requiredText: S.required }));
      } else {
        body.push(h("p", { class: "hint" }, S.tabKey, " ", h("span", { class: "mono", text: `#${t.key}` }), " · ", S.tabKeyFixed));
      }
      const label = h("input", { type: "text", class: "input", autocomplete: "off", value: tv.label, "data-new-tab": "label" });
      label.addEventListener("input", () => { tv.label = label.value; touch(); });
      body.push(field(S.tabLabel, label, { required: true, requiredText: S.required }));
      body.push(h("div", { class: "field" }, h("span", { class: "field-label", text: S.tabIcon }), iconPicker(tv.icon, (ic) => { tv.icon = ic; touch(); })));
    }
    if (sel.kind === "newTab") {
      saveLabel = S.addTab;
      const types = [{ value: SUPERSET, label: S.supersetSection }, ...widgets().map((w) => ({ value: w.type, label: w.label || w.type }))];
      const pick = h("select", { class: "input", "data-new-tab": "type" }, types.map((t) => h("option", { value: t.value, text: t.label })));
      pick.value = sel.type;
      pick.addEventListener("change", () => { sel = { kind: "newTab", type: pick.value }; pending = newSectionDraft(pick.value); render(); });
      body.push(h("h4", { text: S.firstSection }), h("p", { class: "hint", text: S.firstSectionHint }), field(S.sectionType, pick));
    }
    if (sel.kind !== "tab") {
      const type = sel.kind === "section" ? tabByKey(sel.tab).sections[sel.index].type : sel.type;
      const schema = sectionSchema(type);
      if (sel.kind === "section") heading = S.sectionHeading(typeLabel(type), sel.index + 1, tabByKey(sel.tab).label);
      else if (sel.kind === "newSection") { heading = S.newSectionHeading(typeLabel(type), tabByKey(sel.tab).label); saveLabel = S.addSection; }
      const formBox = h("div", { class: "schema-form" });
      if (!schema) formBox.append(alertBox({ tone: "warn", title: S.unknownTypeTitle(type), body: S.unknownTypeBody }));
      else if (type === SUPERSET && !dashboards().length) formBox.append(alertBox({ tone: "warn", title: S.noDashboardsTitle, body: S.noDashboardsBody }));
      else renderForm(formBox, { schema, value: pending, ctx: formCtx(), onChange: touch });
      body.push(formBox);
    }

    const save = button(saveLabel, { iconName: "check", cls: "primary", "data-action": "save", onclick: saveForm });
    const isNew = sel.kind === "newSection" || sel.kind === "newTab";
    const cancel = button(isNew ? S.cancel : S.revert, { "data-action": "cancel", onclick: () => {
      dirty = false; formError = null;
      if (isNew) { sel = null; pending = null; tabDraft = null; } else resetDraftFromSelection();
      render();
    } });
    const dirtyNote = h("span", { class: "dirty-note", role: "status" });
    function paintActions() {
      save.disabled = busy || (!isNew && !dirty);
      cancel.disabled = busy || (!isNew && !dirty);
      dirtyNote.textContent = busy ? S.saving : dirty && !isNew ? S.unsaved : "";
    }
    paintActions();
    actions.append(save, cancel, dirtyNote);
    const form = h("form", { class: "form", novalidate: true }, h("h3", { text: heading }), body, errorBox, actions);
    form.addEventListener("submit", (e) => { e.preventDefault(); saveForm(); });
    box.append(form);
    return box;
  }

  function render() {
    const list = h("ol", { class: "tab-list", "aria-label": S.tabList }, tabs().map((t, i) => tabCard(t, i, tabs().length)));
    list.addEventListener("dragover", onDragOver);
    list.addEventListener("drop", onDrop);

    const palette = h("section", { class: "palette", "aria-label": S.palette },
      h("h3", { text: S.palette }), h("p", { class: "hint", text: S.paletteHint }),
      h("div", { class: "chips" }, paletteItem(SUPERSET), widgets().map((w) => paletteItem(w.type))));

    container.replaceChildren(
      h("div", { class: "view-head" }, h("h2", { text: S.screenTabs }), h("p", { class: "intro", text: S.tabsLead })),
      h("div", { class: "tabs-grid" },
        h("div", { class: "tabs-col" },
          palette,
          listError ? alertBox({ title: listError.title, body: listError.body, errors: listError.errors }) : null,
          tabs().length ? list : stateBox({ iconName: "layers", title: S.noTabsTitle, body: S.noTabsBody }),
          h("div", { class: "list-actions" },
            button(S.addTab, { iconName: "plus", "data-action": "add-tab", onclick: () => { select({ kind: "newTab", type: SUPERSET }); focusInspector(); } }),
            h("span", { class: "hint", text: S.keyboardHint }))),
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
  }

  render();
  return {
    /** 화면을 떠나도 되는지(저장하지 않은 폼) */
    canLeave: () => leave(),
  };
}
