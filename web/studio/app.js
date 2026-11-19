// Studio 셸: 프로젝트 목록·화면 메뉴(왼쪽), 편집(가운데), 미리보기 iframe(오른쪽), 적용·차이·바뀐 파일(위).
// 프로젝트·탭·테이블 이름은 코드에 없다. 모두 Studio API(api.js)에서 온다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { api, ApiError } from "./api.js";
import { h, $, fill, button, iconButton, badge, alertBox, stateBox, skeleton } from "./dom.js";
import { mount as mountWizard } from "./view-wizard.js";
import { mount as mountData } from "./view-data.js";
import { mount as mountTabs } from "./view-tabs.js";
import { mount as mountApp } from "./view-app.js";

const THEME_KEY = "studio-theme";          // localStorage 키(수동 테마 선택)
const PREVIEW_KEY = "studio-preview";      // localStorage 키(미리보기 접기)
const SCREENS = [                          // 5a단계의 화면(프로젝트 안)
  { id: "data", label: S.screenData, iconName: "database", mount: mountData },
  { id: "tabs", label: S.screenTabs, iconName: "layers", mount: mountTabs },
  { id: "app", label: S.screenApp, iconName: "sliders", mount: mountApp },
];
const DEFAULT_SCREEN = "tabs";
const APPLY_POLL_MS = 2000;                // 스트림이 끊긴 뒤 적용 상태를 묻는 주기
const APPLY_POLL_MAX = 900;                // 최대 30분

const root = document.documentElement;
const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
const narrowQuery = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;

const state = {
  projects: [], current: null, webUrl: "",
  registry: { charts: [], widgets: [], icons: [], tones: [] },
  route: { kind: "none" },     // {kind: "new"} | {kind: "project", name, screen} | {kind: "none"}
  config: null, configError: null, configLoading: false,
  changes: [],
  ready: false,                // 첫 읽기가 끝났는지
  applying: false,
  created: null,               // 방금 만든 프로젝트 이름(적용 권유)
  dock: null,                  // "apply" | "plan" | "changes" | null
  previewOpen: true,
};
let view = null;               // 지금 화면 모듈이 돌려준 객체({canLeave?, refresh?})
let skipHash = false;

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* 저장소 사용 불가 */ } },
};

function announce(message) {
  const el = $("announcer");
  el.textContent = "";
  window.setTimeout(() => { el.textContent = message; }, 30);
}

/** ApiError → 화면에 보여 줄 {title, body, errors} */
function describeError(e) {
  if (!(e instanceof ApiError)) return { title: S.errUnexpected, body: String((e && e.message) || e), errors: [] };
  if (e.network) return { title: S.errNetworkTitle, body: S.errNetworkBody, errors: [] };
  const coded = e.detail && S.errByDetail[e.detail];
  const body = [e.message !== e.detail ? e.message : null, e.detail ? S.errCode(e.detail) : null].filter(Boolean).join(" ");
  if (coded) return { title: coded, body, errors: e.errors, detail: e.detail };
  if (e.status === 422) return { title: S.errInvalid, body, errors: e.errors };
  return { title: S.errServer(e.status), body, errors: e.errors };
}

/* ---------------- 테마 ---------------- */

function currentTheme() {
  const t = root.getAttribute("data-theme");
  if (t === "dark" || t === "light") return t;
  return darkQuery && darkQuery.matches ? "dark" : "light";
}

/* ---------------- 주소(해시) ---------------- */

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "new") return { kind: "new" };
  if (parts[0] === "p" && parts[1]) {
    const screen = SCREENS.some((s) => s.id === parts[2]) ? parts[2] : DEFAULT_SCREEN;
    return { kind: "project", name: parts[1], screen };
  }
  return { kind: "none" };
}
const hashOf = (r) => (r.kind === "new" ? "#/new" : r.kind === "project" ? `#/p/${encodeURIComponent(r.name)}/${r.screen}` : "#/");
const sameRoute = (a, b) => hashOf(a) === hashOf(b);

function go(route) {
  if (sameRoute(route, state.route)) return;
  location.hash = hashOf(route);
}

async function onHash() {
  if (skipHash) { skipHash = false; return; }
  if (!state.ready) return;
  let next = parseHash();
  if (next.kind === "none") {
    const first = state.projects.find((p) => p.name === state.current) || state.projects[0];
    next = first ? { kind: "project", name: first.name, screen: DEFAULT_SCREEN } : { kind: "new" };
  }
  if (view && view.canLeave && !sameRoute(next, state.route) && !view.canLeave()) {
    skipHash = true; location.hash = hashOf(state.route); return;   // 저장하지 않은 폼: 머문다
  }
  const prev = state.route;
  state.route = next;
  $("side").classList.remove("is-open");
  $("navBtn").setAttribute("aria-expanded", "false");
  if (hashOf(next) !== location.hash) { skipHash = true; location.hash = hashOf(next); }
  const switched = next.kind !== "project" || prev.kind !== "project" || prev.name !== next.name;
  if (switched && !state.applying) state.dock = null;   // 아래 패널은 프로젝트를 바꾸면 닫는다(적용 중이면 로그를 계속 보여 준다)
  if (next.kind === "project" && (switched || !state.config)) await loadConfig(next.name);
  renderAll();
  $("editor").focus({ preventScroll: true });
}

/* ---------------- 데이터 읽기 ---------------- */

async function loadProjects() {
  const res = await api.projects();
  state.projects = Array.isArray(res.projects) ? res.projects : [];
  state.current = res.current || null;
  state.webUrl = typeof res.web_url === "string" ? res.web_url : "";
}

async function loadConfig(name, { quiet = false } = {}) {
  if (!quiet) { state.config = null; state.configLoading = true; state.configError = null; renderAll(); }
  try {
    const cfg = await api.config(name);
    if (state.route.kind !== "project" || state.route.name !== name) return;
    state.config = cfg; state.configError = null;
  } catch (e) {
    if (!quiet) state.configError = describeError(e);
  }
  state.configLoading = false;
}

async function refreshChanges() {
  try {
    const res = await api.changes();
    state.changes = Array.isArray(res.files) ? res.files : [];
  } catch (e) { /* 목록은 다음에 다시 읽는다 */ }
  renderBar();
  if (state.dock === "changes") renderDock();
}

/* ---------------- 화면 모듈에 넘기는 것 ---------------- */

const ctx = {
  api,
  get config() { return state.config; },
  get registry() { return state.registry; },
  get project() { return state.route.kind === "project" ? state.route.name : null; },
  announce,
  describeError,
  refreshChanges,
  apply: () => runApply(),
  /** 변경 연산을 보내고, 성공하면 설정을 응답으로 바꾼다. 실패하면 ApiError를 던진다(파일은 그대로). */
  async patch(ops) {
    const name = ctx.project;
    let res;
    try {
      res = await api.patchConfig(name, ops, state.config && state.config.revision);
    } catch (e) {
      // 그 사이 파일이 바뀌었다 → 새 내용을 읽어 온 뒤 오류를 알린다(보낸 변경은 저장되지 않았다)
      if (e instanceof ApiError && e.detail === "stale_config" && ctx.project === name) await loadConfig(name, { quiet: true });
      throw e;
    }
    if (state.config && ctx.project === name) {
      if (res.dashboard) state.config.dashboard = res.dashboard;
      if (res.schema) state.config.schema = res.schema;
      if (res.revision) state.config.revision = res.revision;
      state.config.valid = true; state.config.errors = [];
    }
    renderNotice(); renderSide();
    refreshChanges();
    // 앱 제목이 바뀌면 왼쪽 목록의 제목도 바뀐다
    if (ops.some((o) => o.op === "app.update")) loadProjects().then(() => { renderSide(); renderBar(); }, () => {});
    return res;
  },
  async reloadConfig(opts) {
    if (ctx.project) await loadConfig(ctx.project, opts);
    renderNotice();
    if (view && view.refresh) view.refresh();
  },
  async projectCreated(name) {
    view = null;   // 마법사를 떠난다(확인 창 없이)
    state.created = name;
    try { await loadProjects(); } catch (e) { /* 목록은 그대로 */ }
    refreshChanges();
    go({ kind: "project", name, screen: DEFAULT_SCREEN });
  },
};

/* ---------------- 위쪽 막대 ---------------- */

function renderBar() {
  const r = state.route;
  const inProject = r.kind === "project";
  const p = inProject ? state.projects.find((x) => x.name === r.name) : null;
  fill($("barProject"),
    inProject ? h("span", { class: "mono bar-name", text: r.name }) : h("span", { class: "bar-name", text: S.wizardTitle }),
    inProject && state.current === r.name ? badge(S.applied, "good", "check") : null,
    inProject && p && p.valid === false ? badge(S.invalidCount(p.errors || 0), "bad", "warning") : null);

  const next = currentTheme() === "dark" ? "light" : "dark";
  fill($("barActions"),
    button(S.changes(state.changes.length), { iconName: "file", cls: "bar-btn", "data-action": "changes", "aria-pressed": String(state.dock === "changes"), onclick: () => toggleDock("changes") }),
    button(S.plan, { iconName: "diff", cls: "bar-btn", "data-action": "plan", disabled: !inProject, "aria-pressed": String(state.dock === "plan"), onclick: () => showPlan() }),
    button(state.applying ? S.applying : S.apply, { iconName: "play", cls: "primary bar-btn", "data-action": "apply", disabled: !inProject || state.applying || !state.config, onclick: () => runApply() }),
    iconButton("panel", state.previewOpen ? S.previewHide : S.previewShow, { cls: "preview-toggle", "aria-pressed": String(state.previewOpen && inProject), disabled: !inProject, onclick: togglePreview }),
    iconButton(next === "dark" ? "moon" : "sun", S.themeButton(next), { "data-action": "theme", onclick: () => {
      root.setAttribute("data-theme", next); store.set(THEME_KEY, next); renderBar();
    } }));
}

/* ---------------- 왼쪽: 프로젝트와 메뉴 ---------------- */

function renderSide() {
  const r = state.route;
  const projects = state.projects.map((p) => {
    const active = r.kind === "project" && r.name === p.name;
    return h("li", {},
      h("a", { class: "side-item", href: hashOf({ kind: "project", name: p.name, screen: active ? r.screen : DEFAULT_SCREEN }), "aria-current": active ? "true" : null, "data-project": p.name },
        h("span", { class: "side-main" }, h("b", { class: "mono", text: p.name }), p.title ? h("span", { class: "muted", text: p.title }) : null),
        h("span", { class: "side-marks" },
          state.current === p.name ? badge(S.applied, "good", "check") : null,
          p.valid === false ? badge(S.invalidShort, "bad", "warning") : null)));
  });
  const menu = r.kind === "project" ? SCREENS.map((s) =>
    h("li", {}, h("a", { class: "side-item", href: hashOf({ kind: "project", name: r.name, screen: s.id }), "aria-current": r.screen === s.id ? "page" : null, "data-screen": s.id },
      icon(s.iconName), h("span", { text: s.label })))) : [];

  fill($("side"),
    h("section", { class: "side-sec" },
      h("h2", { text: S.projects }),
      projects.length ? h("ul", { class: "side-list", "aria-label": S.projects }, projects) : h("p", { class: "hint", text: S.noProjects }),
      h("a", { class: "btn side-new", href: "#/new", "aria-current": r.kind === "new" ? "page" : null }, icon("plus"), h("span", { text: S.newProject }))),
    menu.length ? h("section", { class: "side-sec" }, h("h2", { text: S.screens }), h("ul", { class: "side-list", "aria-label": S.screens }, menu)) : null);
}

/* ---------------- 가운데: 알림과 화면 ---------------- */

function renderNotice() {
  const r = state.route;
  const items = [];
  if (r.kind === "project" && state.created === r.name) {
    items.push(alertBox({ tone: "good", title: S.createdTitle(r.name), body: S.createdBody,
      actions: [button(S.apply, { iconName: "play", cls: "primary small", disabled: state.applying, onclick: () => runApply() }),
        button(S.close, { cls: "small", onclick: () => { state.created = null; renderNotice(); } })] }));
  }
  if (r.kind === "project" && state.config && state.config.valid === false) {
    items.push(alertBox({ tone: "bad", title: S.configInvalidTitle, body: S.configInvalidBody, errors: state.config.errors || [] }));
  }
  fill($("notice"), items);
}

function renderView() {
  const r = state.route;
  const box = $("view");
  // 화면 모듈이 컨테이너에 건 리스너와 진행 중인 렌더링을 정리하게 한다
  if (view && typeof view.destroy === "function") view.destroy();
  view = null;
  box.className = "";
  if (r.kind === "new") { box.replaceChildren(); view = mountWizard(box, ctx); return; }
  if (r.kind !== "project") { fill(box, skeleton(S.loading)); return; }
  if (state.configLoading) { fill(box, skeleton(S.configLoading)); return; }
  if (state.configError || !state.config) {
    const err = state.configError || { title: S.errUnexpected, body: "", errors: [] };
    fill(box, stateBox({ tone: "bad", iconName: "warning", title: err.title, body: err.body, role: "alert",
      actions: button(S.retry, { iconName: "refresh", cls: "primary", onclick: async () => { await loadConfig(r.name); renderAll(); } }) }));
    return;
  }
  if (!state.config.dashboard || typeof state.config.dashboard !== "object") {
    fill(box, stateBox({ tone: "warn", iconName: "warning", title: S.configUnreadableTitle, body: S.configUnreadableBody }));
    return;
  }
  const screen = SCREENS.find((s) => s.id === r.screen) || SCREENS[0];
  box.replaceChildren();
  view = screen.mount(box, ctx);
}

/* ---------------- 아래 패널: 적용 로그, 차이, 바뀐 파일 ---------------- */

const applyRun = { lines: [], result: null, error: null, project: null };
const planRun = { loading: false, plan: null, error: null };

function toggleDock(kind) {
  state.dock = state.dock === kind ? null : kind;
  if (state.dock === "changes") refreshChanges();
  renderBar(); renderDock();
}

function dockShell(title, body) {
  const dock = $("dock");
  dock.hidden = false;
  fill(dock, h("div", { class: "dock-head" }, h("h2", { text: title }),
    iconButton("x", S.close, { cls: "ghost", onclick: () => { state.dock = null; renderBar(); renderDock(); } })), body);
}

function renderDock() {
  const dock = $("dock");
  if (!state.dock) { dock.hidden = true; dock.replaceChildren(); return; }
  if (state.dock === "changes") {
    dockShell(S.changesTitle, state.changes.length
      ? [h("p", { class: "hint", text: S.changesHint }),
        h("ul", { class: "file-list" }, state.changes.map((f) => h("li", {}, icon("file"), h("span", { class: "mono", text: f.path }), f.at ? h("span", { class: "muted num", text: S.formatTime(f.at) }) : null)))]
      : h("p", { class: "hint", text: S.changesEmpty }));
  } else if (state.dock === "plan") {
    dockShell(S.planTitle, planRun.loading ? skeleton(S.planLoading)
      : planRun.error ? alertBox({ title: planRun.error.title, body: planRun.error.body, errors: planRun.error.errors })
      : planBody(planRun.plan));
  } else if (state.dock === "apply") {
    const log = h("ol", { class: "log mono", role: "log", "aria-label": S.applyLog, tabindex: "0" }, applyRun.lines.map((l) => h("li", { text: l })));
    const res = applyRun.result;
    dockShell(S.applyTitle(applyRun.project || ""), [
      state.applying ? h("p", { class: "busy-line", role: "status", text: S.applyRunning }) : null,
      applyRun.error ? alertBox({ title: applyRun.error.title, body: applyRun.error.body, errors: applyRun.error.errors }) : null,
      res ? alertBox(res.ok
        ? { tone: "good", title: S.applyOk, body: S.applyOkBody }
        : { tone: "bad", title: S.applyFailed(res.step), body: S.applyFailedBody, errors: res.errors }) : null,
      log]);
    log.scrollTop = log.scrollHeight;
  }
}

function appendLog(line) {
  applyRun.lines.push(line);
  const log = $("dock").querySelector(".log");
  if (!log || state.dock !== "apply") return;
  log.append(h("li", { text: line }));
  log.scrollTop = log.scrollHeight;
}

async function runApply() {
  const name = ctx.project;
  if (!name || state.applying) return;
  Object.assign(applyRun, { lines: [], result: null, error: null, project: name });
  state.applying = true; state.dock = "apply";
  renderBar(); renderNotice(); renderDock();
  announce(S.applyRunning);
  try {
    await finishApply(name, await api.apply(name, appendLog));
  } catch (e) {
    // 스트림이 끊겼다(작업은 서버에서 끝까지 간다) → 상태를 물어 결과를 받는다
    const dropped = e instanceof ApiError && (e.network || e.detail === "stream_ended");
    const last = dropped ? await waitApply(name) : null;
    if (last) { await finishApply(name, last); }
    else { applyRun.error = describeError(e); announce(applyRun.error.title); }
  }
  state.applying = false;
  renderBar(); renderSide(); renderNotice(); renderPreview(); renderDock();
}

async function finishApply(name, res) {
  applyRun.result = res;
  announce(res.ok ? S.applyOk : S.applyFailed(res.step));
  if (!res.ok) return;
  if (state.created === name) state.created = null;
  try { await loadProjects(); } catch (e) { /* 목록은 그대로 */ }
  if (ctx.project === name) await ctx.reloadConfig({ quiet: true });
  reloadPreview();
}

/** 적용이 끝날 때까지 상태를 묻는다. name의 마지막 결과({ok, step, errors})를 돌려준다(알 수 없으면 null). */
async function waitApply(name) {
  for (let i = 0; i < APPLY_POLL_MAX; i++) {
    let st;
    try { st = await api.applyStatus(); } catch (e) { return null; }
    if (!st.running) {
      const last = st.last;
      if (!last || last.project !== name) return null;
      if (Array.isArray(last.log)) { applyRun.lines = last.log.map(String); if (state.dock === "apply") renderDock(); }
      return { ok: !!last.ok, step: last.step || null, errors: Array.isArray(last.errors) ? last.errors.map(String) : [] };
    }
    await new Promise((r) => window.setTimeout(r, APPLY_POLL_MS));
  }
  return null;
}

/** 화면을 열었을 때 적용이 돌고 있으면(다른 창, 새로 고침) 끝날 때까지 "적용 중"으로 둔다 */
async function resumeApply() {
  let st;
  try { st = await api.applyStatus(); } catch (e) { return; }
  if (!st || !st.running || !st.project) return;
  const name = st.project;
  Object.assign(applyRun, { lines: [], result: null, error: null, project: name });
  state.applying = true; state.dock = "apply";
  renderBar(); renderDock();
  const last = await waitApply(name);
  if (last) await finishApply(name, last);
  state.applying = false;
  renderBar(); renderSide(); renderNotice(); renderPreview(); renderDock();
}

async function showPlan() {
  const name = ctx.project;
  if (!name) return;
  if (state.dock === "plan") { toggleDock("plan"); return; }
  state.dock = "plan";
  Object.assign(planRun, { loading: true, plan: null, error: null });
  renderBar(); renderDock();
  try { planRun.plan = await api.plan(name); } catch (e) { planRun.error = describeError(e); }
  planRun.loading = false;
  if (state.dock === "plan") renderDock();
}

/** contracts/plan.md의 결과 형식을 줄 목록으로 */
function planBody(plan) {
  if (!plan) return null;
  const rows = [];
  const row = (group, name, text, tone = "neutral") => rows.push(h("tr", {},
    h("th", { scope: "row", text: group }), h("td", { class: "mono", text: name }), h("td", {}, h("span", { class: "plan-text", "data-tone": tone, text }))));
  const cols = (c) => (c ? [
    (c.added || []).length ? S.planColsAdded(c.added.join(", ")) : null,
    (c.removed || []).length ? S.planColsRemoved(c.removed.join(", ")) : null,
    (c.retyped || []).length ? S.planColsRetyped(c.retyped.map((x) => `${x.column}: ${x.from}→${x.to}`).join(", ")) : null,
    c.reordered ? S.planColsReordered : null].filter(Boolean) : []);

  const quiet = [];
  for (const d of plan.data || []) {
    if (d.status === "unchanged") { quiet.push(d.table); continue; }
    if (d.status === "unrecorded") { row(S.planData, d.table, S.planUnrecorded(d.rows), "warn"); continue; }
    const text = [S.planRows(d.added ?? 0, d.removed ?? 0, d.changed ?? 0), S.planStatus[d.status] || d.status, d.comparable === false && d.reason ? d.reason : null, ...cols(d.columns)].filter(Boolean).join(" · ");
    row(S.planData, d.table, text, "primary");
  }
  if (quiet.length) row(S.planData, quiet.join(", "), S.planNoChange);
  for (const v of plan.views || []) {
    if (v.status === "unchanged") continue;
    const text = v.status === "unknown" ? S.planUnknown(v.reason) : [S.planStatus[v.status] || v.status, ...cols(v.columns)].join(" · ");
    row(S.planViews, v.dataset, text, v.status === "unknown" ? "warn" : "primary");
  }
  const ss = plan.superset || {};
  if (ss.first) row(S.planSuperset, "", S.planFirst, "primary");
  if (ss.replaces) row(S.planSuperset, ss.replaces.project, S.planReplaces(ss.replaces.objects), "warn");
  if (ss.pending_prune) row(S.planSuperset, "", S.planPrune(ss.pending_prune), "warn");
  for (const [kind, label] of [["datasets", S.planDatasets], ["charts", S.planCharts], ["dashboards", S.planDashboards]]) {
    const g = ss[kind];
    if (!g) continue;
    const names = (list) => (list || []).map((x) => (typeof x === "string" ? x : x.detail || x.reason ? `${x.key} (${x.detail || x.reason})` : x.key)).join(", ");
    if ((g.added || []).length) row(label, names(g.added), S.planAdded(g.added.length), "good");
    if ((g.changed || []).length) row(label, names(g.changed), S.planChanged(g.changed.length), "primary");
    if ((g.removed || []).length) row(label, names(g.removed), S.planRemoved(g.removed.length), "bad");
    if ((g.unknown || []).length) row(label, names(g.unknown), S.planUnknown(""), "warn");
  }
  return [
    alertBox(plan.changed ? { tone: "primary", title: S.planChangedTitle, body: S.planChangedBody, role: "status" } : { tone: "good", title: S.planSameTitle, body: S.planSameBody, role: "status" }),
    rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "grid plan" }, h("tbody", {}, rows))) : null,
  ];
}

/* ---------------- 오른쪽: 미리보기 ---------------- */

let frame = null;

function reloadPreview() {
  if (frame && state.webUrl) frame.setAttribute("src", state.webUrl);   // 다른 출처라 src를 다시 넣어 새로 고친다
}

function togglePreview() {
  state.previewOpen = !state.previewOpen;
  store.set(PREVIEW_KEY, state.previewOpen ? "open" : "closed");
  renderBar(); renderPreview();
}

// 미리보기 틀은 한 번만 만든다. iframe은 DOM에서 뺐다 넣으면 다시 불러오므로 자리를 옮기지 않는다.
let previewParts = null;

function renderPreview() {
  const box = $("preview");
  const open = state.previewOpen && state.route.kind !== "new";   // 마법사에서는 표가 넓어야 해서 접는다
  $("shell").classList.toggle("no-preview", !open);
  box.hidden = !open;   // 접어도 iframe은 남겨 둔다(다시 펴면 그대로 보인다)
  if (!open && !previewParts) return;
  if (!previewParts) {
    previewParts = { head: h("div", { class: "preview-head" }), note: h("div", { class: "preview-note-box" }), body: h("div", { class: "preview-body" }) };
    fill(box, previewParts.head, previewParts.note, previewParts.body);
  }
  const r = state.route;
  const other = r.kind === "project" && state.current !== r.name;
  fill(previewParts.head,
    h("h2", { text: S.previewTitle }),
    state.current ? badge(state.current, "neutral") : null,
    h("span", { class: "spacer" }),
    iconButton("refresh", S.previewReload, { cls: "ghost", "data-action": "preview-reload", disabled: !state.webUrl, onclick: reloadPreview }),
    state.webUrl ? h("a", { class: "btn icon-only ghost", href: state.webUrl, target: "_blank", rel: "noopener noreferrer", "aria-label": S.previewOpen, title: S.previewOpen }, icon("external")) : null,
    iconButton("x", S.previewHide, { cls: "ghost", onclick: togglePreview }));
  fill(previewParts.note,
    other ? h("p", { class: "preview-note", role: "status" }, icon("info"), h("span", { text: state.current ? S.previewOther(state.current) : S.previewNone })) : null);
  if (state.webUrl && !frame) {
    // allow: 웹앱 안의 Superset 임베드가 요청하는 권한(전체 화면, 복사)을 미리보기에서도 넘겨준다
    frame = h("iframe", { class: "preview-frame", title: S.previewTitle, src: state.webUrl, allow: "fullscreen; clipboard-write" });
    fill(previewParts.body, frame);
  } else if (!state.webUrl) {
    fill(previewParts.body, stateBox({ iconName: "panel", title: S.previewMissingTitle, body: S.previewMissingBody }));
  }
}

/* ---------------- 시작 ---------------- */

function renderAll() {
  renderBar(); renderSide(); renderNotice(); renderView(); renderPreview(); renderDock();
}

async function start() {
  root.lang = S.lang;
  document.title = S.appName;
  $("skip").textContent = S.skipToContent;
  $("brandName").textContent = S.appName;
  $("side").setAttribute("aria-label", S.navLabel);
  $("preview").setAttribute("aria-label", S.previewTitle);
  const navBtn = $("navBtn");
  navBtn.append(icon("menu"));
  navBtn.setAttribute("aria-label", S.navLabel);
  navBtn.addEventListener("click", () => {
    const open = $("side").classList.toggle("is-open");
    navBtn.setAttribute("aria-expanded", String(open));
  });

  const saved = store.get(THEME_KEY);
  if (saved === "dark" || saved === "light") root.setAttribute("data-theme", saved);
  if (darkQuery) darkQuery.addEventListener("change", () => { if (!root.hasAttribute("data-theme")) renderBar(); });
  const pref = store.get(PREVIEW_KEY);
  state.previewOpen = pref ? pref === "open" : !(narrowQuery && narrowQuery.matches);

  renderBar();
  window.addEventListener("hashchange", onHash);
  await load();
}

/** 프로젝트 목록과 registry를 읽고 첫 화면을 연다(실패하면 다시 시도 버튼) */
async function load() {
  fill($("view"), skeleton(S.loading));
  try {
    const [, registry] = await Promise.all([loadProjects(), api.registry()]);
    state.registry = { charts: [], widgets: [], icons: [], tones: [], ...registry };
  } catch (e) {
    const err = describeError(e);
    fill($("view"), stateBox({ tone: "bad", iconName: "warning", title: S.startErrorTitle, body: [err.title, err.body].filter(Boolean).join(" · "), role: "alert",
      actions: button(S.retry, { iconName: "refresh", cls: "primary", onclick: () => load() }) }));
    return;
  }
  state.ready = true;
  refreshChanges();
  await onHash();
  resumeApply();
}

start();
