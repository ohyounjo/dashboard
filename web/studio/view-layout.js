// 대시보드 배치 화면: 12칸 격자의 카드(제목·유형·데이터셋만, 실제 차트는 그리지 않는다)를 옮기고 너비를 바꾸고,
// 팔레트에서 차트·제목·글·탭 묶음을 추가한다. 실제 모습은 적용한 뒤 오른쪽 미리보기에서 본다.
// 저장 방식(탭 구성 화면과 같다):
//  - 옮기기·너비 조절·삭제는 끝나는 즉시 연산 하나를 보낸다(layout.set은 layout 전체. 건드리지 않은 칸은 원래 꼴 그대로).
//    검증에 실패하면 오류를 보여 주고 화면은 서버 상태 그대로다(이 화면은 배치의 초안을 따로 들고 있지 않다).
//  - 차트·제목·글의 내용은 폼에서 고치고 "저장"을 눌러야 나간다. 실패하면 초안을 그대로 두고 오류를 폼 옆에 보여 준다.
// 너비 조절: 끄는 칸의 너비만 바꾼다. 같은 행의 다른 칸이 지정한 너비는 건드리지 않고, 너비가 없는 칸에 1칸씩은 남도록
//  끌 수 있는 범위를 막는다(행 합계가 12칸을 넘지 않는다. 이웃의 설정 글자가 바뀌지 않는다).
import S from "./strings.js";
import { icon, vizIconName } from "./icons.js";
import { h, field, button, iconButton, badge, alertBox, stateBox, clone, same, select } from "./dom.js";
import { GRID, isBlock, isHeader, isText, isHeaderRow, chartOf, widthOf, sameC, rowsOf, itemAt, findChart, rowWidths, fits, maxWidth,
  setSize, moveItem, removeItem, insertItem, moveEntry, removeTab, placedCharts } from "./layout-model.js";
import { renderChartForm, cleanChart, diffValues, newChart, KEY_PATTERN } from "./chart-form.js";
import { pickDashboard, dashboardSelect, saver } from "./screen-kit.js";

const cText = (c) => (c === null ? "top" : `${c[0]}.${c[1]}`);
const cParse = (s) => (s === "top" || s == null ? null : s.split(".").map(Number));

export function mount(container, ctx) {
  let dash = pickDashboard(ctx);
  let sel = null;          // { kind: "item", addr, key? } | { kind: "new", what: { type: "chart", viz } | { type: "raw" } | { type: "header" } | { type: "markdown" }, place }
  let pending = null;      // 폼 초안: 차트(키 제외) 또는 { text }
  let original = null;
  let keyBox = null;       // 새 차트의 키 입력값
  let chartForm = null;
  let dirty = false;
  let headPending = null;  // 대시보드 제목·교차 필터 초안
  let listError = null, formError = null, headError = null;
  let drag = null;         // { kind: "item", addr } | { kind: "block", index } | { kind: "palette", what }
  let resizing = false;
  let focusAfter = null;
  let alive = true;        // 화면을 떠나면 false(떠난 뒤 끝난 저장이 같이 쓰는 컨테이너를 다시 그리지 않게)
  const save = saver(container, ctx, () => alive);

  const boards = () => (ctx.config.dashboard && ctx.config.dashboard.dashboards) || {};
  const board = () => boards()[dash] || null;
  const layout = () => (board() && Array.isArray(board().layout) ? board().layout : []);
  const charts = () => (board() && Array.isArray(board().charts) ? board().charts : []);
  const chartByKey = (key) => charts().find((c) => c && c.key === key) || null;
  const types = () => ctx.registry.charts || [];
  const typeOf = (viz) => types().find((t) => t.viz === viz) || null;
  const datasets = () => Object.keys((ctx.config.dashboard && ctx.config.dashboard.datasets) || {});
  const columnsOf = (ds) => {
    const cols = ds && ctx.config.columns ? ctx.config.columns[ds] : null;
    return Array.isArray(cols) ? cols.map((c) => (typeof c === "string" ? { name: c } : c)) : null;
  };
  // 지표 목록: config의 metrics(계약 5b). 없으면 dashboard.datasets.<키>.metrics의 키로 대신한다
  const metricsOf = (ds) => {
    const fromApi = ctx.config.metrics && ctx.config.metrics[ds];
    if (Array.isArray(fromApi)) return fromApi.map(String);
    const d = ((ctx.config.dashboard && ctx.config.dashboard.datasets) || {})[ds];
    return d && d.metrics && typeof d.metrics === "object" ? Object.keys(d.metrics) : [];
  };
  const typeName = (chart) => (chart.viz ? (typeOf(chart.viz) || {}).label || chart.viz : chart.viz_type || "");
  const whatLabel = (what) => (what.type === "chart" ? (typeOf(what.viz) || {}).label || what.viz : what.type === "raw" ? S.paletteRaw : what.type === "header" ? S.itemHeader : S.itemText);
  const focusKey = (lay, addr) => { const k = chartOf(itemAt(lay, addr)); return k ? `chart:${k}` : `cell:${cText(addr.c)}:${addr.r}:${addr.i}`; };

  function whereText(lay, addr) {
    const box = addr.c === null ? S.layTop : S.layInTab(addr.c[0] + 1, (lay[addr.c[0]].tabs[addr.c[1]] || {}).label || "");
    return S.layWhere(box, addr.r + 1, addr.i + 1);
  }

  /* ---------------- 선택과 초안 ---------------- */

  function leave() {
    if (dirty && !window.confirm(S.discardConfirm)) return false;
    dirty = false; formError = null;
    return true;
  }

  /** 대시보드 제목·교차 필터 초안이 저장된 값과 다른지 */
  function headDirty() {
    const b = board();
    return !!(headPending && b) && (headPending.title !== (b.title || "") || headPending.cross_filters !== (b.cross_filters === true));
  }

  /** 다른 대시보드나 다른 화면으로 떠날 때: 칸 폼과 대시보드 제목·교차 필터 초안을 함께 본다 */
  function leaveBoard() {
    if ((dirty || headDirty()) && !window.confirm(S.discardConfirm)) return false;
    dirty = false; formError = null; headPending = null; headError = null;   // 버린 초안은 다음 render()가 다시 읽는다
    return true;
  }

  function loadHead() {
    const b = board();
    headPending = b ? { title: b.title || "", cross_filters: b.cross_filters === true } : null;
    headError = null;
  }

  /** 서버 상태가 바뀐 뒤: 선택한 칸이 아직 있으면 초안을 다시 읽고, 없으면 선택을 푼다 */
  function resetSelection() {
    dirty = false; formError = null; keyBox = null;
    if (sel && sel.kind === "item") {
      const addr = sel.key ? findChart(layout(), sel.key) : sel.addr;
      const item = addr ? itemAt(layout(), addr) : undefined;
      if (item === undefined || (sel.key && !chartByKey(sel.key))) sel = null;
      else {
        sel = { kind: "item", addr, key: sel.key };
        if (sel.key) { const { key, ...rest } = chartByKey(sel.key); pending = clone(rest); }
        else pending = { text: isHeader(item) ? item.header : isText(item) ? item.markdown : "" };
        original = clone(pending);
        return;
      }
    } else sel = null;
    pending = original = null;
  }

  function selectItem(addr) {
    if (save.busy || !leave()) return;
    listError = null;
    const key = chartOf(itemAt(layout(), addr));
    sel = { kind: "item", addr, key: key && chartByKey(key) ? key : undefined };
    resetSelection();
    focusAfter = focusKey(layout(), addr);
    render();
    const box = container.querySelector(".inspector");
    if (box && box.getBoundingClientRect().top > window.innerHeight - 160) box.scrollIntoView({ block: "start" });
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
      if (res.info.detail === "stale_config") { resetSelection(); loadHead(); form = false; }   // 설정을 새로 읽었다 → 초안을 버린다
      if (form) formError = res.info; else { listError = res.info; focusAfter = null; }
      ctx.announce(res.info.title);
    }
    render();
  }

  function reject(reason) {
    listError = { title: S.layRejectTitle, body: S.layReject[reason] || S.layReject.same, errors: [] };
    ctx.announce(listError.body);
    render();
  }

  /** layout 사본을 고쳐 layout.set 하나로 보낸다. fn이 { error }를 돌려주면 보내지 않고 이유를 보여 준다. */
  function mutate(fn, { message, focus, form = false } = {}) {
    if (save.busy || !board()) return;
    const next = clone(layout());
    const res = fn(next) || {};
    if (res.error === "same") return;
    if (res.error) { reject(res.error); return; }
    if (same(next, layout())) { if (form) { dirty = false; render(); } return; }
    if (!form && !leave()) return;   // 폼 저장(form)은 초안을 보내는 것이라 묻지 않는다
    const moved = res.addr;
    if (moved) focusAfter = focusKey(next, moved); else if (focus) focusAfter = focus;
    const wasSelected = sel && sel.kind === "item" && res.from && sameC(sel.addr.c, res.from.c) && sel.addr.r === res.from.r && sel.addr.i === res.from.i;
    send([{ op: "layout.set", dashboard: dash, layout: next }], {
      form,
      message: typeof message === "function" ? message(next, res) : message,
      onDone: () => {
        if (res.drop) sel = null;
        else if (moved && wasSelected) sel = { ...sel, addr: moved };
        else if (sel && sel.kind === "item" && !sel.key) sel = null;   // 제목·글 칸의 자리가 바뀌었을 수 있다 → 선택을 푼다
        resetSelection();
      },
    });
  }

  function move(from, to) {
    mutate((next) => ({ ...moveItem(next, from, to), from }), { message: (next, res) => S.layMoved(whereText(next, res.addr)) });
  }

  /** 행 안에서 왼쪽(-1)·오른쪽(+1)으로 한 칸 */
  function stepItem(addr, dir) {
    const row = rowsOf(layout(), addr.c)[addr.r];
    const to = addr.i + dir;
    if (to < 0 || to >= row.length) return;
    move(addr, { c: addr.c, r: addr.r, i: dir < 0 ? to : to + 1 });
  }

  /** 위(-1)·아래(+1) 행으로. 이웃 행에 들어갈 수 없으면(제목, 남은 칸 없음) 그 행을 넘어 새 행이 된다. 탭 묶음을 만나면 그 안으로, 탭의 끝에서는 밖으로. */
  function vertItem(addr, dir) {
    const lay = layout();
    const rows = rowsOf(lay, addr.c);
    const row = rows[addr.r];
    const item = row[addr.i];
    const n = addr.r + dir;
    if (n < 0 || n >= rows.length) {
      if (row.length > 1) return move(addr, { c: addr.c, at: dir < 0 ? 0 : rows.length });
      if (addr.c !== null) return move(addr, { c: null, at: dir < 0 ? addr.c[0] : addr.c[0] + 1 });
      return undefined;
    }
    const near = rows[n];
    if (isBlock(near)) {
      const t = dir > 0 ? 0 : near.tabs.length - 1;
      return move(addr, { c: [n, t], at: dir > 0 ? 0 : (near.tabs[t].rows || []).length });
    }
    if (!isHeader(item) && !isHeaderRow(near) && fits(near, item)) return move(addr, { c: addr.c, r: n, i: Math.min(addr.i, near.length) });
    if (row.length > 1) return move(addr, { c: addr.c, at: dir < 0 ? addr.r : addr.r + 1 });
    return move(addr, { c: addr.c, at: dir < 0 ? n : n + 1 });
  }

  function resize(addr, width) {
    const row = rowsOf(layout(), addr.c)[addr.r];
    const item = row[addr.i];
    if (isHeader(item)) return;
    const w = width == null ? null : Math.max(1, Math.min(maxWidth(row, addr.i), width));
    if (w === widthOf(item)) return;
    mutate((next) => { setSize(next, addr, "width", w); return { addr, from: addr }; },
      { message: w == null ? S.layResizedAuto : S.layResized(w, GRID) });
  }

  /** 키보드로 한 칸 넓히거나 줄인다. 더 갈 수 없으면 알리기만 한다. */
  function nudge(addr, dir) {
    const row = rowsOf(layout(), addr.c)[addr.r];
    if (isHeader(row[addr.i])) return;
    const shown = rowWidths(row)[addr.i];
    const max = maxWidth(row, addr.i);
    const to = Math.max(1, Math.min(max, shown + dir));
    if (to === shown) { ctx.announce(S.layResizeLimit(shown, max)); return; }
    resize(addr, to);
  }

  function moveBlock(index, at) {
    mutate((next) => { const to = moveEntry(next, index, at); return to < 0 ? { error: "same" } : { to }; },
      { message: (next, res) => S.blockMoved(res.to + 1, next.length), focus: `block:${at > index ? at - 1 : at}` });
  }

  function addBlock(at) {
    const label = S.newTabLabel(1);
    mutate((next) => { next.splice(at, 0, { tabs: [{ label, rows: [[{ header: label }]] }] }); return {}; }, { message: S.blockAdded, focus: `block:${at}` });
  }

  function removeEntry(addr) {
    const item = itemAt(layout(), addr);
    const key = chartOf(item);
    const probe = removeItem(clone(layout()), addr);
    if (key && chartByKey(key)) {
      // 차트는 chart.remove 하나로 지운다. 탭의 마지막 칸이었으면 서버가 그 탭(과 빈 탭 묶음)도 지운다(계약 5b 표)
      const title = chartByKey(key).title || key;
      if (!window.confirm(probe.error === "last" ? S.chartRemoveLastConfirm(title) : S.chartRemoveConfirm(title))) return;
      dirty = false;
      send([{ op: "chart.remove", key }], { message: S.chartRemoved(title), onDone: () => { sel = null; resetSelection(); } });
      return;
    }
    if (probe.error) { reject(probe.error); return; }
    if (!window.confirm(S.itemRemoveConfirm)) return;
    dirty = false;
    mutate((next) => ({ ...removeItem(next, addr), drop: true }), { message: S.itemRemoved });
  }

  /** 팔레트 항목을 place에 놓는다 → 초안 폼을 연다(저장은 "추가"를 눌러야 한다) */
  function startNew(what, place) {
    if (save.busy || !board()) return;
    if (what.type === "tabs") { addBlock("at" in place && place.c === null ? place.at : layout().length); return; }
    if (!("at" in place)) {
      const row = rowsOf(layout(), place.c)[place.r];
      if (what.type === "header" || isHeaderRow(row)) { reject("header"); return; }
      if (!fits(row, "x")) { reject("full"); return; }
    }
    if (!leave()) return;
    sel = { kind: "new", what, place };
    keyBox = { value: "" };
    pending = what.type === "chart" || what.type === "raw" ? newChart(what.type === "chart" ? typeOf(what.viz) : null, datasets()[0]) : { text: "" };
    original = null; dirty = true; listError = null;
    render();
    ctx.announce(S.newItemStarted(whatLabel(what)));
    const el = container.querySelector(".inspector input, .inspector select, .inspector textarea");
    if (el) el.focus();
  }

  function allChartKeys() {
    const out = new Set();
    for (const b of Object.values(boards())) for (const c of (b && Array.isArray(b.charts) ? b.charts : [])) if (c && c.key) out.add(c.key);
    return out;
  }

  function saveForm() {
    if (!sel || save.busy) return;
    const fail = (problems) => { formError = { title: S.errInvalid, body: null, errors: problems }; ctx.announce(S.errInvalid); render(); };
    const isChart = sel.kind === "item" ? !!sel.key : sel.what.type === "chart" || sel.what.type === "raw";

    if (!isChart) {
      const text = String(pending.text || "");
      if (!text.trim()) return fail([S.itemTextEmpty]);
      if (sel.kind === "new") {
        const { what, place } = sel;
        const item = what.type === "header" ? { header: text } : { markdown: text };
        return mutate((next) => ({ addr: insertItem(next, place, item) }), { form: true, message: S.itemAdded(whatLabel(what)) });
      }
      const addr = sel.addr;
      return mutate((next) => {
        const row = rowsOf(next, addr.c)[addr.r];
        row[addr.i] = isHeader(row[addr.i]) ? { header: text } : { ...row[addr.i], markdown: text };
        return { addr, from: addr };
      }, { form: true, message: S.saved });
    }

    const value = cleanChart(pending);
    const problems = [];
    if (chartForm && chartForm.invalidJson()) problems.push(S.chartParamsInvalid);
    if (!String(value.title || "").trim()) problems.push(S.chartTitleEmpty);
    if (!value.dataset) problems.push(S.chartDatasetEmpty);
    if (sel.kind === "new") {
      const key = keyBox.value.trim();
      if (!KEY_PATTERN.test(key)) problems.push(S.chartKeyInvalid);
      else if (allChartKeys().has(key)) problems.push(S.chartKeyTaken(key));
      if (sel.what.type === "raw" && !value.viz_type) problems.push(S.chartRawTypeEmpty);
      if (problems.length) return fail(problems);
      const { place } = sel;
      const lay = layout();
      const add = { op: "chart.add", dashboard: dash, chart: { key, ...value } };
      const ops = [add];
      if (place.c === null && "at" in place && place.at >= lay.length) {
        // 맨 아래 새 행: 서버의 기본 자리
      } else if (place.c === null && !("at" in place)) {
        add.row = place.r; add.index = place.i;   // row는 layout 배열에서의 자리(탭 묶음도 한 자리)
      } else {
        // 행 사이의 새 행, 탭 속: chart.add(맨 아래에 붙는다) 뒤에 layout.set으로 자리를 정한다(계약 5b 표)
        const next = clone(lay);
        insertItem(next, place, key);
        ops.push({ op: "layout.set", dashboard: dash, layout: next });
      }
      focusAfter = `chart:${key}`;
      return send(ops, { form: true, message: S.chartAdded(value.title), onDone: () => { sel = { kind: "item", key, addr: findChart(layout(), key) }; resetSelection(); } });
    }
    if (problems.length) return fail(problems);
    const values = diffValues(original, value);
    if (!Object.keys(values).length) { dirty = false; render(); return undefined; }
    focusAfter = `chart:${sel.key}`;
    return send([{ op: "chart.update", key: sel.key, values }], { form: true, message: S.saved, onDone: resetSelection });
  }

  async function saveHead() {
    const b = board();
    if (!b || save.busy) return;
    const values = {};
    if (headPending.title !== (b.title || "")) values.title = headPending.title;
    if (headPending.cross_filters !== (b.cross_filters === true)) values.cross_filters = headPending.cross_filters ? true : null;   // 끄면 키를 지운다(기본값이 꺼짐이다)
    if (!headPending.title.trim()) { headError = { title: S.errInvalid, body: null, errors: [S.dashboardTitleEmpty] }; ctx.announce(S.errInvalid); render(); return; }
    if (!Object.keys(values).length) return;
    if (!leave()) return;
    headError = null;
    const res = await save.send([{ op: "dashboard.update", dashboard: dash, values }]);
    if (!alive || !res) return;   // 화면을 떠났다: 잠금은 풀렸고, 다시 그리지 않는다
    if (res.ok) { loadHead(); resetSelection(); focusAfter = "head"; ctx.announce(S.saved); }
    else { if (res.info.detail === "stale_config") { loadHead(); resetSelection(); } headError = res.info; ctx.announce(res.info.title); }
    render();
  }

  /* ---------------- 탭 묶음 ---------------- */

  function renameTab(b, t, label, input) {
    const cur = layout()[b].tabs[t].label;
    if (!label.trim()) { input.value = cur; reject("label"); return; }
    if (label === cur) return;
    mutate((next) => { next[b].tabs[t].label = label; return {}; }, { message: S.saved, focus: `tab:${b}:${t}` });
  }
  function addTab(b) {
    const n = layout()[b].tabs.length;
    const label = S.newTabLabel(n + 1);
    mutate((next) => { next[b].tabs.push({ label, rows: [[{ header: label }]] }); return {}; }, { message: S.innerTabAdded(label), focus: `tab:${b}:${n}` });
  }
  function moveTab(b, t, dir) {
    const to = t + dir;
    if (to < 0 || to >= layout()[b].tabs.length) return;
    mutate((next) => { const [tab] = next[b].tabs.splice(t, 1); next[b].tabs.splice(to, 0, tab); return {}; }, { message: S.innerTabMoved(to + 1), focus: `tab:${b}:${to}` });
  }
  function dropTab(b, t) {
    const tab = layout()[b].tabs[t];
    if (!window.confirm(S.innerTabRemoveConfirm(tab.label))) return;
    mutate((next) => { removeTab(next, b, t); return {}; }, { message: S.innerTabRemoved(tab.label) });
  }

  /* ---------------- 끌어다 놓기 ---------------- */

  const clearMarks = () => container.querySelectorAll(".drop-left, .drop-right, .drop-into").forEach((el) => el.classList.remove("drop-left", "drop-right", "drop-into"));

  function target(e) {
    if (!drag) return null;
    const el = e.target instanceof Element ? e.target.closest(".lay-gap, .lay-row") : null;
    if (!el || !container.contains(el)) return null;
    const c = cParse(el.getAttribute("data-c"));
    const whole = drag.kind === "block" || (drag.kind === "palette" && drag.what.type === "tabs");   // 탭 묶음은 최상위의 행 사이에만 놓는다
    if (el.classList.contains("lay-gap")) {
      if (whole && c !== null) return null;
      return { el, place: { c, at: Number(el.getAttribute("data-at")) } };
    }
    if (whole) return null;
    const cards = Array.from(el.querySelectorAll(":scope > .lay-card:not(.is-pending)"));
    let slot = 0;
    for (const card of cards) { const r = card.getBoundingClientRect(); if (e.clientX > r.left + r.width / 2) slot++; }
    return { el, cards, place: { c, r: Number(el.getAttribute("data-r")), i: slot } };
  }

  function onDragOver(e) {
    const t = target(e);
    if (!t) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = drag.kind === "palette" ? "copy" : "move";
    clearMarks();
    if (!t.cards) t.el.classList.add("drop-into");
    else if (!t.cards.length) t.el.classList.add("drop-into");
    else if (t.place.i < t.cards.length) t.cards[t.place.i].classList.add("drop-left");
    else t.cards[t.cards.length - 1].classList.add("drop-right");
  }

  function onDrop(e) {
    const t = target(e);
    const d = drag;
    drag = null; clearMarks();
    container.classList.remove("is-dragging-any");
    if (!t || !d) return;
    e.preventDefault();
    if (d.kind === "item") move(d.addr, t.place);
    else if (d.kind === "block") moveBlock(d.index, t.place.at);
    else startNew(d.what, t.place);
  }

  function draggable(el, payload, label) {
    el.setAttribute("draggable", "true");
    el.addEventListener("dragstart", (e) => {
      if (save.busy || resizing) { e.preventDefault(); return; }
      e.stopPropagation();
      drag = payload;
      e.dataTransfer.effectAllowed = payload.kind === "palette" ? "copy" : "move";
      e.dataTransfer.setData("text/plain", label);
      el.classList.add("is-dragging");
      container.classList.add("is-dragging-any");
    });
    el.addEventListener("dragend", () => { drag = null; el.classList.remove("is-dragging"); container.classList.remove("is-dragging-any"); clearMarks(); });
  }

  /* ---------------- 너비 조절(가장자리 끌기) ---------------- */

  const setSpan = (el, n) => { for (let k = 1; k <= GRID; k++) el.classList.remove(`span-${k}`); el.classList.add(`span-${n}`); };

  function startResize(e, addr, rowEl) {
    if (save.busy || e.button > 0) return;
    e.preventDefault(); e.stopPropagation();
    const handle = e.currentTarget;
    const row = rowsOf(layout(), addr.c)[addr.r];
    const cards = Array.from(rowEl.querySelectorAll(":scope > .lay-card:not(.is-pending)"));
    const card = cards[addr.i];
    const start = rowWidths(row)[addr.i];
    const max = maxWidth(row, addr.i);
    const rect = rowEl.getBoundingClientRect();
    const gap = parseFloat(getComputedStyle(rowEl).columnGap) || 0;
    const unit = (rect.width + gap) / GRID;                // 한 칸 + 틈
    const left = card.getBoundingClientRect().left;
    let width = start;
    resizing = true;
    card.classList.add("is-resizing");
    try { handle.setPointerCapture(e.pointerId); } catch (err) { /* 포인터를 잡지 못해도 끌기는 된다 */ }
    const paint = () => {
      const probe = clone(row);
      setSize([probe], { c: null, r: 0, i: addr.i }, "width", width);
      rowWidths(probe).forEach((w, n) => { if (cards[n]) { setSpan(cards[n], w); const b = cards[n].querySelector(".lay-width"); if (b) b.textContent = S.layCols(w); } });
    };
    const onMove = (ev) => {
      const n = Math.max(1, Math.min(max, Math.round((ev.clientX - left + gap) / unit)));
      if (n !== width) { width = n; paint(); }
    };
    const finish = (commit) => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onCancel);
      resizing = false;
      card.classList.remove("is-resizing");
      if (commit && width !== start) { focusAfter = focusKey(layout(), addr); resize(addr, width); }
      else render();
    };
    const onUp = () => finish(true);
    const onCancel = () => finish(false);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onCancel);
  }

  /* ---------------- 그리기: 격자 ---------------- */

  function cardOf(item, addr, width, rowEl) {
    const lay = layout();
    const key = chartOf(item);
    const chart = key ? chartByKey(key) : null;
    const selected = sel && sel.kind === "item" && sameC(sel.addr.c, addr.c) && sel.addr.r === addr.r && sel.addr.i === addr.i;
    const auto = !isHeader(item) && !widthOf(item);
    let kind, title, body, name;
    if (isHeader(item)) {
      kind = "header"; name = S.itemHeader; title = item.header;
      body = [h("span", { class: "lay-ico" }, icon("heading")), h("span", { class: "lay-main" }, h("b", { class: "lay-title", text: item.header }), h("span", { class: "lay-sub muted", text: S.itemHeader }))];
    } else if (isText(item)) {
      kind = "text"; name = S.itemText; title = String(item.markdown).trim().split("\n")[0];
      body = [h("span", { class: "lay-ico" }, icon("doc")), h("span", { class: "lay-main" }, h("b", { class: "lay-title", text: title }), h("span", { class: "lay-sub muted", text: S.itemText }))];
    } else if (chart) {
      kind = "chart"; name = typeName(chart); title = chart.title || key;
      body = [h("span", { class: "lay-ico" }, icon(vizIconName(chart.viz))),
        h("span", { class: "lay-main" }, h("b", { class: "lay-title", text: title }),
          h("span", { class: "lay-sub" }, h("span", { text: name }), h("span", { class: "mono muted", text: chart.dataset || "" })))];
    } else {
      kind = "chart"; name = S.itemUnknown; title = String(key);
      body = [h("span", { class: "lay-ico" }, icon("warning")), h("span", { class: "lay-main" }, h("b", { class: "lay-title mono", text: title }), badge(S.itemUnknown, "warn"))];
    }
    const card = h("div", { class: `lay-card span-${width} is-${kind}${selected ? " is-selected" : ""}`, role: "button", tabindex: "0", "data-focus": focusKey(lay, addr), "data-kind": kind,
      "aria-label": S.cardAria(name, title, whereText(lay, addr), width), "aria-pressed": String(!!selected) },
      body,
      isHeader(item) ? null : h("span", { class: "lay-width num", text: S.layCols(width), title: auto ? S.layAuto : null, "data-auto": auto ? "true" : null }));
    card.addEventListener("click", () => selectItem(addr));
    card.addEventListener("keydown", (e) => {
      if (e.target !== card) return;
      const arrow = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
      if (arrow && e.altKey) { e.preventDefault(); if (arrow[0]) stepItem(addr, arrow[0]); else vertItem(addr, arrow[1]); }
      else if (arrow && arrow[0] && e.shiftKey) { e.preventDefault(); nudge(addr, arrow[0]); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectItem(addr); }
    });
    draggable(card, { kind: "item", addr }, title);
    if (!isHeader(item)) {
      const handle = h("span", { class: "lay-handle", "aria-hidden": "true", title: S.layHandle });
      handle.addEventListener("pointerdown", (e) => startResize(e, addr, rowEl));
      handle.addEventListener("click", (e) => e.stopPropagation());
      card.append(handle);
    }
    return card;
  }

  const pendingCard = (width) => h("div", { class: `lay-card span-${width} is-pending` },
    h("span", { class: "lay-ico" }, icon("plus")), h("span", { class: "lay-main" }, h("b", { class: "lay-title", text: whatLabel(sel.what) }), h("span", { class: "lay-sub muted", text: S.layPending })));

  /** 묶음 c의 행들(틈 포함) */
  function rowsView(rows, c) {
    const out = [];
    const place = sel && sel.kind === "new" && sameC(sel.place.c, c) ? sel.place : null;
    const gap = (at) => h("div", { class: "lay-gap", "data-c": cText(c), "data-at": at, "aria-hidden": "true" });
    const ghostRow = () => h("div", { class: "lay-row" }, pendingCard(GRID));
    rows.forEach((entry, r) => {
      out.push(gap(r));
      if (place && "at" in place && place.at === r) out.push(ghostRow());
      if (isBlock(entry)) { out.push(blockView(entry, r)); return; }
      if (!Array.isArray(entry)) return;
      const ghost = place && !("at" in place) && place.r === r;
      const widths = rowWidths(entry, ghost ? 1 : 0);
      const rowEl = h("div", { class: `lay-row${isHeaderRow(entry) ? " is-header-row" : ""}`, "data-c": cText(c), "data-r": r });
      entry.forEach((item, i) => rowEl.append(cardOf(item, { c, r, i }, widths[i], rowEl)));
      if (ghost) rowEl.insertBefore(pendingCard(widths[entry.length]), rowEl.children[place.i] || null);
      out.push(rowEl);
    });
    out.push(gap(rows.length));
    if (place && "at" in place && place.at >= rows.length) out.push(ghostRow());
    return out;
  }

  function blockView(block, b) {
    const n = layout().length;
    const head = h("div", { class: "lay-block-head", tabindex: "0", role: "group", "data-focus": `block:${b}`, "aria-label": S.blockAria(block.tabs.length, b + 1, n) },
      h("span", { class: "grip", "aria-hidden": "true" }, icon("grip")), icon("tabset"), h("b", { text: S.blockName }), h("span", { class: "muted", text: S.blockCount(block.tabs.length) }),
      h("span", { class: "spacer" }),
      h("span", { class: "row-actions" },
        iconButton("up", S.moveUp, { cls: "ghost", disabled: b === 0, onclick: () => moveBlock(b, b - 1) }),
        iconButton("down", S.moveDown, { cls: "ghost", disabled: b === n - 1, onclick: () => moveBlock(b, b + 2) }),
        button(S.innerTabAdd, { iconName: "plus", cls: "small", "data-action": "add-inner-tab", onclick: () => addTab(b) })));
    head.addEventListener("keydown", (e) => {
      if (e.target !== head || !e.altKey) return;
      if (e.key === "ArrowUp" && b > 0) { e.preventDefault(); moveBlock(b, b - 1); }
      if (e.key === "ArrowDown" && b < n - 1) { e.preventDefault(); moveBlock(b, b + 2); }
    });
    draggable(head, { kind: "block", index: b }, S.blockName);
    const tabs = block.tabs.map((tab, t) => {
      const label = h("input", { type: "text", class: "input lay-tab-label", value: tab.label ?? "", autocomplete: "off", "aria-label": S.innerTabLabel(t + 1), "data-focus": `tab:${b}:${t}` });
      label.addEventListener("change", () => renameTab(b, t, label.value, label));
      label.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); label.blur(); } });
      return h("section", { class: "lay-tab", "aria-label": S.innerTabAria(tab.label, t + 1, block.tabs.length) },
        h("div", { class: "lay-tab-head" }, label,
          h("span", { class: "row-actions" },
            iconButton("left", S.innerTabLeft, { cls: "ghost", disabled: t === 0, onclick: () => moveTab(b, t, -1) }),
            iconButton("right", S.innerTabRight, { cls: "ghost", disabled: t === block.tabs.length - 1, onclick: () => moveTab(b, t, 1) }),
            iconButton("trash", S.innerTabRemove, { cls: "ghost", onclick: () => dropTab(b, t) }))),
        h("div", { class: "lay-rows" }, rowsView(Array.isArray(tab.rows) ? tab.rows : [], [b, t])));
    });
    return h("div", { class: "lay-block", "data-block": b }, head, h("div", { class: "lay-tabs" }, tabs));
  }

  /* ---------------- 그리기: 팔레트 ---------------- */

  function chip(what, label, iconName) {
    const add = () => startNew(what, { c: null, at: layout().length });
    const el = h("div", { class: "chip", role: "button", tabindex: "0", "data-what": what.type === "chart" ? `chart:${what.viz}` : what.type, "aria-label": S.paletteAddItem(label), title: S.layPaletteHint, onclick: add },
      icon(iconName), h("span", { text: label }));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); add(); } });
    draggable(el, { kind: "palette", what }, label);
    return el;
  }

  /** 유형 묶음: registry에는 묶음이 없어서 필드의 생김새로 나눈다(시간 단위가 있는 것 / 필수 열이 없는 것 / 그 밖) */
  function paletteGroups() {
    const groups = [[S.paletteTime, []], [S.paletteNumber, []], [S.paletteCategory, []]];
    for (const t of types()) {
      const fields = Array.isArray(t.fields) ? t.fields : [];
      const timed = fields.some((f) => f.kind === "time_grain");
      const needsColumn = fields.some((f) => f.required && (f.kind === "column" || f.kind === "columns"));
      groups[timed ? 0 : needsColumn ? 2 : 1][1].push(t);
    }
    return groups.filter(([, list]) => list.length);
  }

  function palette() {
    return h("section", { class: "palette", "aria-label": S.palette },
      h("h3", { text: S.palette }), h("p", { class: "hint", text: S.layPaletteHint }),
      paletteGroups().map(([name, list]) => h("div", { class: "chip-group", role: "group", "aria-label": name },
        h("span", { class: "chip-group-name", text: name }),
        h("div", { class: "chips" }, list.map((t) => chip({ type: "chart", viz: t.viz }, t.label || t.viz, vizIconName(t.viz)))))),
      h("div", { class: "chip-group", role: "group", "aria-label": S.paletteOther },
        h("span", { class: "chip-group-name", text: S.paletteOther }),
        h("div", { class: "chips" },
          chip({ type: "header" }, S.itemHeader, "heading"), chip({ type: "markdown" }, S.itemText, "doc"),
          chip({ type: "tabs" }, S.blockName, "tabset"), chip({ type: "raw" }, S.paletteRaw, "code"))));
  }

  /* ---------------- 그리기: 옵션 ---------------- */

  /** 고른 칸의 자리·크기(바로 저장된다). 끌기의 키보드·좁은 화면 대안이다. */
  function placeBox(addr) {
    const lay = layout();
    const rows = rowsOf(lay, addr.c);
    const row = rows[addr.r];
    const item = row[addr.i];
    const shown = rowWidths(row)[addr.i];
    const box = h("fieldset", { class: "group place-box" }, h("legend", {}, h("span", { class: "legend-name", text: S.placeTitle })),
      h("p", { class: "hint", text: whereText(lay, addr) }),
      h("div", { class: "place-moves" },
        iconButton("left", S.placeLeft, { "data-move": "left", disabled: addr.i === 0, onclick: () => stepItem(addr, -1) }),
        iconButton("right", S.placeRight, { "data-move": "right", disabled: addr.i >= row.length - 1, onclick: () => stepItem(addr, 1) }),
        iconButton("up", S.placeUp, { "data-move": "up", onclick: () => vertItem(addr, -1) }),
        iconButton("down", S.placeDown, { "data-move": "down", onclick: () => vertItem(addr, 1) }),
        button(S.placeNewRow, { cls: "small", "data-move": "new-row", disabled: row.length < 2, onclick: () => move(addr, { c: addr.c, at: addr.r + 1 }) })));
    // 옮길 곳: 최상위 또는 탭(그 묶음의 맨 아래 새 행으로)
    const spots = [];
    if (addr.c !== null) spots.push({ value: "top", label: S.layTop });
    lay.forEach((entry, b) => { if (isBlock(entry)) entry.tabs.forEach((tab, t) => { if (!(addr.c && addr.c[0] === b && addr.c[1] === t)) spots.push({ value: `${b}.${t}`, label: S.layInTab(b + 1, tab.label) }); }); });
    if (spots.length) {
      const to = select(spots, "", { placeholder: S.placeToChoose, "data-move": "to", onchange: (v) => { if (!v) return; const c = cParse(v); move(addr, { c, at: rowsOf(lay, c).length }); } });
      box.append(field(S.placeTo, to, { cls: "compact" }));
    }
    if (!isHeader(item)) {
      const max = maxWidth(row, addr.i);
      const width = h("input", { type: "number", class: "input short", min: 1, max, step: 1, value: widthOf(item) ?? "", placeholder: String(shown), "data-size": "width" });
      width.addEventListener("change", () => { resize(addr, width.value === "" ? null : Math.round(Number(width.value)) || 1); });
      const height = h("input", { type: "number", class: "input short", min: isText(item) ? 5 : 10, max: 200, step: 1, value: (typeof item === "object" && item.height) || "", "data-size": "height" });
      height.addEventListener("change", () => {
        const v = height.value === "" ? null : Math.round(Number(height.value));
        mutate((next) => { setSize(next, addr, "height", v); return { addr, from: addr }; }, { message: S.saved });
      });
      box.append(h("div", { class: "place-sizes" },
        field(S.placeWidth, width, { hint: S.placeWidthHint(shown, max), cls: "compact" }),
        field(S.placeHeight, height, { hint: S.placeHeightHint, cls: "compact" })));
    }
    return box;
  }

  function inspector() {
    const box = h("section", { class: "panel inspector", "aria-label": S.inspector });
    chartForm = null;
    if (!sel) {
      box.append(stateBox({ iconName: "sliders", title: S.layInspectorEmptyTitle, body: S.layInspectorEmptyBody }));
      return box;
    }
    const isNew = sel.kind === "new";
    const item = isNew ? null : itemAt(layout(), sel.addr);
    const isChart = isNew ? sel.what.type === "chart" || sel.what.type === "raw" : !!sel.key;
    const touch = () => { if (!dirty) { dirty = true; paintActions(); } };
    const body = [];
    let heading;
    if (isChart) {
      heading = isNew ? S.newChartHeading(whatLabel(sel.what)) : S.chartHeading(chartByKey(sel.key).title || sel.key);
      if (!isNew) body.push(h("p", { class: "hint" }, S.chartKey, " ", h("span", { class: "mono", text: sel.key }), " · ", S.chartKeyFixed));
      const formBox = h("div", { class: "schema-form" });
      chartForm = renderChartForm(formBox, { pending, isNew, keyBox, types: types(), datasets: datasets(), columns: columnsOf, metrics: metricsOf, onChange: touch });
      body.push(formBox);
    } else if (!isNew && !isHeader(item) && !isText(item)) {
      // layout이 가리키는 차트가 charts에 없다(손으로 고친 파일): 옮기거나 지울 수만 있다
      heading = S.itemUnknown;
      body.push(alertBox({ tone: "warn", title: S.itemUnknown, body: S.itemUnknownBody }));
    } else {
      const header = isNew ? sel.what.type === "header" : isHeader(item);
      heading = isNew ? S.newItemHeading(whatLabel(sel.what)) : header ? S.itemHeader : S.itemText;
      const input = header ? h("input", { type: "text", class: "input", autocomplete: "off", value: pending.text, "data-path": "text" })
        : h("textarea", { class: "input", rows: 8, "data-path": "text", spellcheck: "false" });
      if (!header) input.value = pending.text;
      input.addEventListener("input", () => { pending.text = input.value; touch(); });
      body.push(field(header ? S.itemHeaderText : S.itemTextBody, input, { hint: header ? S.itemHeaderHint : S.itemTextHint, required: true, requiredText: S.required }));
    }

    const editable = isChart || isNew || isHeader(item) || isText(item);
    const saveBtn = button(isNew ? S.itemAddAction : S.save, { iconName: "check", cls: "primary", "data-action": "save", onclick: saveForm });
    const cancel = button(isNew ? S.cancel : S.revert, { "data-action": "cancel", onclick: () => {
      dirty = false; formError = null;
      if (isNew) { sel = null; pending = null; } else resetSelection();
      render();
    } });
    const remove = isNew ? null : button(S.remove, { iconName: "trash", "data-action": "remove", onclick: () => removeEntry(sel.addr) });
    const note = h("span", { class: "dirty-note", role: "status" });
    function paintActions() {
      saveBtn.disabled = save.busy || (!isNew && !dirty);
      cancel.disabled = save.busy || (!isNew && !dirty);
      note.textContent = save.busy ? S.saving : dirty && !isNew ? S.unsaved : "";
    }
    paintActions();
    const errorBox = h("div", { class: "form-error" }, formError ? alertBox({ title: formError.title, body: formError.body, errors: formError.errors }) : null);
    const form = h("form", { class: "form", novalidate: true }, h("h3", { text: heading }),
      isNew ? null : placeBox(sel.addr), body, errorBox,
      h("div", { class: "form-actions" }, editable ? [saveBtn, cancel] : null, remove, note));
    form.addEventListener("submit", (e) => { e.preventDefault(); if (editable) saveForm(); });
    box.append(form);
    return box;
  }

  function headForm() {
    const b = board();
    const title = h("input", { type: "text", class: "input", autocomplete: "off", value: headPending.title, "data-head": "title", "data-focus": "head" });
    const cross = h("input", { type: "checkbox", id: "head-cross", checked: headPending.cross_filters, "data-head": "cross_filters" });
    const btn = button(S.save, { iconName: "check", cls: "primary small", "data-action": "save-head", onclick: saveHead });
    const paint = () => { btn.disabled = save.busy || (headPending.title === (b.title || "") && headPending.cross_filters === (b.cross_filters === true)); };
    title.addEventListener("input", () => { headPending.title = title.value; paint(); });
    cross.addEventListener("change", () => { headPending.cross_filters = cross.checked; paint(); });
    paint();
    const form = h("form", { class: "panel head-form", novalidate: true },
      dashboardSelect(ctx, dash, (key) => { if (!leaveBoard()) return false; dash = pickDashboard(ctx, key); sel = null; pending = original = null; listError = null; loadHead(); render(); return true; }),
      field(S.dashboardTitle, title, { required: true, requiredText: S.required }),
      h("div", { class: "field check" }, cross, h("label", { for: cross.id, text: S.dashboardCross }), h("span", { class: "hint", text: S.dashboardCrossHint })),
      h("div", { class: "form-actions" }, btn),
      headError ? alertBox({ title: headError.title, body: headError.body, errors: headError.errors }) : null);
    form.addEventListener("submit", (e) => { e.preventDefault(); saveHead(); });
    return form;
  }

  function render() {
    if (!alive) return;   // 화면을 떠난 뒤 끝난 요청은 그리지 않는다
    const head = h("div", { class: "view-head" }, h("h2", { text: S.screenLayout }), h("p", { class: "intro", text: S.layLead }));
    if (!board()) {
      container.replaceChildren(head, stateBox({ tone: "warn", iconName: "warning", title: S.noDashboardsTitle, body: S.layNoDashboards }));
      return;
    }
    if (!headPending) loadHead();
    const grid = h("div", { class: "lay-board", "aria-label": S.layBoard(board().title || dash) }, h("div", { class: "lay-rows" }, rowsView(layout(), null)));
    grid.addEventListener("dragover", onDragOver);
    grid.addEventListener("drop", onDrop);
    const missing = charts().map((c) => c.key).filter((k) => !placedCharts(layout()).includes(k));

    container.replaceChildren(head, headForm(),
      h("div", { class: "lay-grid" },
        h("div", { class: "lay-col" },
          palette(),
          listError ? alertBox({ title: listError.title, body: listError.body, errors: listError.errors }) : null,
          missing.length ? alertBox({ tone: "warn", title: S.layMissingTitle, body: S.layMissing(missing.join(", ")) }) : null,
          grid,
          h("p", { class: "hint lay-keys", text: S.layKeyboardHint })),
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
      const el = container.querySelector(".lay-col > .alert");
      if (el) el.scrollIntoView({ block: "nearest" });
    }
  }

  render();
  return {
    /** 화면을 떠나도 되는지(저장하지 않은 폼). 저장 요청이 나가 있는 동안에는 묻지 않는다(그 초안은 이미 보냈다). */
    canLeave: () => save.busy || leaveBoard(),
    /** 셸이 설정을 다시 읽었다(적용 뒤: 열 목록이 DB 기준으로 바뀐다). 고치던 폼이 없을 때만 다시 그린다. */
    refresh: () => { if (!alive || save.busy || dirty || headDirty()) return; resetSelection(); render(); },
    destroy: () => { alive = false; },
  };
}
