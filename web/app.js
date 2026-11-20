// 웹앱 셸: /api/app으로 탭·섹션을 받아 그린다. 탭·섹션·데이터셋·열 이름은 코드에 없다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { loginUrl, postJson } from "./common.js";

const WIDGET_TYPE = /^[a-z][a-z0-9_]*$/;         // 위젯 유형 이름 형식(import 경로에 쓰기 전 한 번 더 확인)
const THEME_KEY = "dashboard-theme";             // localStorage 키(수동 테마 선택)
const CONNECT_TIMEOUT_MS = 30000;                // Superset iframe 응답 대기 상한
const SIZE_POLL_MS = 2000;                       // 높이 자동 맞춤 주기(보이는 탭만)
const HEX = /^#[0-9A-Fa-f]{6}$/;

const root = document.documentElement;
const $ = (id) => document.getElementById(id);
const media = (q) => (window.matchMedia ? window.matchMedia(q) : null);
const darkQuery = media("(prefers-color-scheme: dark)");
const reducedMotion = !!media("(prefers-reduced-motion: reduce)")?.matches;

/** 요소 생성. 문자열 자식은 텍스트 노드로 넣는다(innerHTML 없음). */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/* ---------------- 테마 ---------------- */

const embeds = new Set(); // 임베드된 대시보드(SDK 반환 객체)

function currentTheme() {
  const t = root.getAttribute("data-theme");
  if (t === "dark" || t === "light") return t;
  return darkQuery && darkQuery.matches ? "dark" : "light";
}
const supersetMode = () => (currentTheme() === "dark" ? "dark" : "default");

function setEmbedTheme(d) {
  try { d.setThemeMode(supersetMode()); } catch (e) { /* iframe이 사라진 경우 */ }
}

function paintTheme() {
  const cur = currentTheme();
  const next = cur === "dark" ? "light" : "dark";
  $("themeLabel").textContent = next === "dark" ? S.themeToDark : S.themeToLight;
  $("themeBtn").setAttribute("aria-label", S.themeButtonLabel(next));
  $("themeBtn").title = S.theme;
  $("themeIco").replaceChildren(icon(next === "dark" ? "moon" : "sun"));
  embeds.forEach(setEmbedTheme);
}

function initTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "dark" || saved === "light") root.setAttribute("data-theme", saved);
  } catch (e) { /* 저장소 사용 불가 */ }
  $("themeBtn").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 저장소 사용 불가 */ }
    paintTheme();
  });
  if (darkQuery) {
    const onChange = () => { if (!root.hasAttribute("data-theme")) paintTheme(); };
    darkQuery.addEventListener ? darkQuery.addEventListener("change", onChange) : darkQuery.addListener(onChange);
  }
  paintTheme();
}

/* 메인 컬러 덮어쓰기: --brand-*를 채우면 styles.css의 3단 토큰이 그대로 이 값을 쓴다. */
const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const toHex = (c) => "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const mix = (a, b, t) => toHex(rgb(a).map((v, i) => v * t + rgb(b)[i] * (1 - t)));
function luminance(hex) {
  const [r, g, b] = rgb(hex).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const contrast = (a, b) => { const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
const onColor = (bg, light, dark) => (contrast(bg, light) >= contrast(bg, dark) ? light : dark);

function applyBrand(theme) {
  const p = theme && theme.primary;
  if (!p || !HEX.test(p.light || "") || !HEX.test(p.dark || "")) return;
  const s = root.style;
  s.setProperty("--brand-light", p.light);
  s.setProperty("--brand-soft-light", mix(p.light, "#FFFFFF", 0.11));
  s.setProperty("--brand-on-light", onColor(p.light, "#FFFFFF", "#141833"));
  s.setProperty("--brand-dark", p.dark);
  s.setProperty("--brand-soft-dark", mix(p.dark, "#171B2E", 0.16));
  s.setProperty("--brand-on-dark", onColor(p.dark, "#FFFFFF", "#0E1120"));
}

/* ---------------- 상태 화면 ---------------- */

function stateBox({ tone, iconName, title, body, onRetry, role = "alert" }) {
  return h("div", { class: "state", role, "data-tone": tone },
    h("div", { class: "badge" }, icon(iconName)),
    h("h3", { text: title }),
    body ? h("p", { text: body }) : null,
    onRetry ? h("div", { class: "actions" },
      h("button", { type: "button", class: "btn primary", onclick: onRetry }, icon("refresh"), S.retry)) : null);
}

function dashboardSkeleton() {
  return h("div", { class: "sk", role: "status", "aria-busy": "true" },
    h("div", { class: "sk-note" }, S.dashboardLoading),
    h("div", { class: "sk-row" }, h("div", { class: "sk-block tile" }), h("div", { class: "sk-block tile" }), h("div", { class: "sk-block tile" })),
    h("div", { class: "sk-row" }, h("div", { class: "sk-block chart" }), h("div", { class: "sk-block chart" })));
}

function widgetSkeleton() {
  return h("div", { class: "sk", role: "status", "aria-busy": "true" },
    h("div", { class: "sk-note" }, S.sectionLoading),
    h("div", { class: "sk-block line" }),
    h("div", { class: "sk-block bar" }), h("div", { class: "sk-block bar" }), h("div", { class: "sk-block bar" }));
}

/* ---------------- 네트워크 ---------------- */

/** 세션이 없거나 만료됐다(401). 로그인 화면으로 보내고 돌아올 곳(지금 주소)을 넘긴다. */
let leaving = false;
function toLogin(reason) {
  if (leaving) return;
  leaving = true;
  location.replace(loginUrl(location.pathname + location.search + location.hash, reason));
}

class SectionError extends Error {
  constructor(kind, body) { super(kind); this.kind = kind; this.body = body; }
}

async function readDetail(res) {
  try { const j = await res.json(); return j && typeof j.detail === "string" ? j.detail : null; } catch (e) { return null; }
}

function withTimeout(promise, ms) {
  let t;
  return Promise.race([
    promise.finally(() => clearTimeout(t)),
    new Promise((_, reject) => { t = setTimeout(() => reject(new SectionError("connect")), ms); }),
  ]);
}

/* ---------------- Superset 섹션 ---------------- */

function supersetSection(container, sec, ctx) {
  const wrap = h("div", { class: "embed" });
  const mount = h("div", { class: "embed-mount" });
  const overlay = h("div");
  wrap.append(mount, overlay);
  container.append(wrap);

  let dash = null, gen = 0, poll = null;

  function teardown() {
    clearInterval(poll); poll = null;
    if (dash) { embeds.delete(dash); try { dash.unmount(); } catch (e) { /* 무시 */ } dash = null; }
    mount.replaceChildren();
  }

  function showError(err) {
    teardown();
    wrap.className = "embed is-error";
    const k = err instanceof SectionError ? err.kind : "connect";
    const box =
      k === "notApplied" ? { tone: "primary", iconName: "upload", title: S.notAppliedTitle, body: S.notAppliedBody }
      : k === "token" ? { tone: "warn", iconName: "key", title: S.tokenErrorTitle, body: err.body }
      : { tone: "bad", iconName: "plug", title: S.connectErrorTitle, body: S.connectErrorBody(ctx.supersetUrl) };
    overlay.replaceChildren(stateBox({ ...box, onRetry: load }));
  }

  async function fetchGuestToken() {
    let res;
    try {
      res = await fetch("/api/guest-token", {
        method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ dashboard: sec.dashboard }),
      });
    } catch (e) { throw new SectionError("token", S.tokenErrorBody.network); }
    if (res.status === 401) { toLogin("expired"); throw new SectionError("token", S.sessionExpired); }
    if (res.ok) {
      const j = await res.json().catch(() => ({}));
      if (j && typeof j.token === "string") return j.token;
      throw new SectionError("token", S.tokenErrorBody.server(res.status));
    }
    const detail = await readDetail(res);
    if (res.status === 409 && detail === "not_applied") throw new SectionError("notApplied");
    if (res.status === 404 && detail === "unknown_dashboard") throw new SectionError("token", S.tokenErrorBody.unknown_dashboard);
    throw new SectionError("token", S.tokenErrorBody.server(res.status));
  }

  function iframe() { return mount.querySelector("iframe"); }

  function measure() {
    const d = dash, f = iframe();
    if (!d || !f || sec.height) return;
    d.getScrollSize().then((size) => {
      if (d !== dash || !size || !(size.height > 0)) return;
      const px = Math.ceil(size.height) + "px";
      if (f.style.height !== px) f.style.height = px;
    }).catch(() => { /* 다음 주기에 다시 */ });
  }

  // 첫 로드는 boot()에서 받은 embed_id를 쓴다. 다시 시도할 때는 그 사이 make apply(또는 reset)로
  // 임베드 UUID가 생기거나 바뀌었을 수 있으므로 /api/app에서 이 대시보드의 최신 값을 다시 읽는다.
  let firstLoad = true;

  async function latestEmbedId() {
    try {
      const res = await fetch("/api/app", { cache: "no-store", headers: { Accept: "application/json" } });
      if (!res.ok) return sec.embed_id;
      const cfg = await res.json();
      for (const tab of (cfg && cfg.tabs) || []) {
        for (const s of tab.sections || []) {
          if (s.type === "superset" && s.dashboard === sec.dashboard) return s.embed_id || null;
        }
      }
    } catch (e) { /* 백엔드에 닿지 않으면 가진 값으로 시도한다 */ }
    return sec.embed_id;
  }

  async function load() {
    const my = ++gen;
    teardown();
    if (!firstLoad) {
      wrap.className = "embed is-loading";
      overlay.replaceChildren(dashboardSkeleton());
      const id = await latestEmbedId();
      if (my !== gen) return;
      sec.embed_id = id;
    }
    firstLoad = false;
    if (!sec.embed_id) { showError(new SectionError("notApplied")); return; }
    wrap.className = "embed is-loading";
    overlay.replaceChildren(dashboardSkeleton());
    const started = Date.now();
    try {
      const sdk = window.supersetEmbeddedSdk;
      if (!sdk || typeof sdk.embedDashboard !== "function") throw new SectionError("connect");
      const pending = sdk.embedDashboard({
        id: sec.embed_id,
        supersetDomain: ctx.supersetUrl,
        mountPoint: mount,
        fetchGuestToken,
        iframeTitle: sec.title || ctx.tabLabel,
        // 좁은 화면(하단 탭 바 구간)에서는 필터 패널이 차트를 가리므로 접은 채로 시작한다.
        dashboardUiConfig: { hideTitle: true, filters: { expanded: !window.matchMedia("(max-width: 600px)").matches } },
      });
      // 시간 초과 뒤에 늦게 완료되면 바로 해제한다.
      pending.then((d) => { if (my !== gen) { try { d.unmount(); } catch (e) { /* 무시 */ } } }, () => {});
      const d = await withTimeout(pending, CONNECT_TIMEOUT_MS);
      if (my !== gen) return;
      dash = d;
      embeds.add(d);
      setEmbedTheme(d);
      // iframe 안의 Superset이 응답해야 연결된 것이다(접속 불가 주소여도 iframe load 이벤트는 온다).
      // 성공·오류 응답 모두 연결 증거다. 응답이 없으면 시간 초과.
      const left = Math.max(1000, CONNECT_TIMEOUT_MS - (Date.now() - started));
      const size = await withTimeout(d.getScrollSize().catch(() => null), left);
      if (my !== gen) return;
      const f = iframe();
      if (f && sec.height) f.style.height = sec.height + "px";
      else if (f && size && size.height > 0) f.style.height = Math.ceil(size.height) + "px";
      wrap.className = "embed";
      overlay.replaceChildren();
      if (!sec.height) poll = setInterval(() => { if (ctx.isActive()) measure(); }, SIZE_POLL_MS);
    } catch (err) {
      if (my !== gen) return;
      if (!(err instanceof SectionError)) console.warn(err);
      showError(err);
    }
  }

  ctx.onResize(() => measure());
  ctx.onShow(() => measure());
  load();
}

/* ---------------- 위젯 섹션(web/widgets/<유형>/widget.js) ---------------- */

/** /api/app의 widgets → 셸이 import()할 수 있는 유형 이름 집합. 서버가 준 이름과 정확히 같을 때만 경로에 쓴다. */
function widgetTypeSet(list) {
  const types = new Set();
  for (const w of Array.isArray(list) ? list : []) {
    if (w && typeof w.type === "string" && WIDGET_TYPE.test(w.type)) types.add(w.type);
  }
  return types;
}

function widgetSection(container, sec, ctx) {
  const body = h("div");
  container.append(body);
  let inst = null, gen = 0;

  function fail(title, text, retry = true) {
    body.replaceChildren(stateBox({ tone: "bad", iconName: "database", title, body: text, onRetry: retry ? load : null }));
  }

  async function load() {
    const my = ++gen;
    if (inst && typeof inst.destroy === "function") { try { inst.destroy(); } catch (e) { /* 무시 */ } }
    inst = null;
    if (typeof sec.type !== "string" || !ctx.widgetTypes.has(sec.type)) { fail(S.widgetErrorTitle, S.unsupportedSection(String(sec.type)), false); return; }
    body.replaceChildren(widgetSkeleton());

    let data = null;
    if (sec.dataset != null) try {   // dataset 옵션이 없는 유형은 데이터를 받지 않는다(rows = [])
      let res;
      try {
        res = await fetch(`/api/data/${encodeURIComponent(sec.dataset)}`, { cache: "no-store", headers: { Accept: "application/json" } });
      } catch (e) { throw new SectionError("data", S.dataErrorBody.network); }
      if (res.status === 401) { toLogin("expired"); throw new SectionError("data", S.sessionExpired); }
      if (!res.ok) {
        const detail = await readDetail(res);
        const known = (res.status === 404 && detail === "unknown_dataset") || (res.status === 503 && (detail === "not_loaded" || detail === "db_unavailable"));
        throw new SectionError("data", known ? S.dataErrorBody[detail] : S.dataErrorBody.server(res.status));
      }
      data = await res.json();
    } catch (err) {
      if (my !== gen) return;
      fail(S.dataErrorTitle, err instanceof SectionError ? err.body : S.dataErrorBody.server("?"));
      return;
    }

    try {
      const mod = await import(`./widgets/${sec.type}/widget.js`);
      if (my !== gen) return;
      const el = h("div", { class: `widget widget-${sec.type}` });
      body.replaceChildren(el);
      const rows = Array.isArray(data && data.rows) ? data.rows : [];
      inst = mod.default(el, rows, sec, {
        asOf: ctx.asOf, strings: S, reducedMotion, truncated: !!(data && data.truncated),
      }) || null;
    } catch (err) {
      if (my !== gen) return;
      console.warn(err);
      fail(S.widgetErrorTitle, err && err.message ? err.message : "");
    }
  }

  load();
}

/* ---------------- 셸 ---------------- */

function renderSection(view, sec, tabCtx) {
  const isEmbed = sec.type === "superset";
  const panel = h("section", { class: isEmbed ? "panel embed-panel" : "panel" });
  if (sec.title) {
    const id = `sec-${tabCtx.index}-${tabCtx.sectionIndex++}`;
    panel.append(h("h2", { id, text: sec.title }));
    panel.setAttribute("aria-labelledby", id);
  }
  view.append(panel);
  if (isEmbed) supersetSection(panel, sec, tabCtx);
  else widgetSection(panel, sec, tabCtx);
}

function showAppState(box) {
  $("views").replaceChildren(h("div", { class: "app-state" }, box));
}

async function boot() {
  showAppState(h("div", { class: "sk-note", role: "status" }, S.appLoading));
  let cfg;
  try {
    const res = await fetch("/api/app", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.status === 401) { toLogin(); return; }
    if (!res.ok) throw new Error(String(res.status));
    cfg = await res.json();
    if (!cfg || !cfg.app || !Array.isArray(cfg.tabs)) throw new Error("shape");
  } catch (e) {
    showAppState(stateBox({ tone: "bad", iconName: "warning", title: S.appErrorTitle, body: S.appErrorBody, onRetry: boot }));
    return;
  }
  renderApp(cfg);
}

/* ---------------- 사용자 메뉴 ---------------- */

function renderUserMenu(user) {
  const menu = $("userMenu"), btn = $("userBtn"), pop = $("userPop");
  if (!user || typeof user.email !== "string") { menu.hidden = true; return; }
  $("userIco").replaceChildren(icon("user"));
  $("userLabel").textContent = S.account;
  btn.setAttribute("aria-label", S.accountMenuLabel(user.email));
  btn.title = user.email;
  $("userEmail").textContent = user.email;
  $("userRole").textContent = S.roleLabel[user.role] || user.role || "";
  const logout = h("button", { type: "button" }, icon("logout"), S.logout);
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    try { await postJson("/api/auth/logout", {}); } catch (e) { /* 서버에 닿지 않아도 로그인 화면으로 간다 */ }
    leaving = true;
    location.replace(loginUrl("/", "logout"));
  });
  const actions = [];
  // 운용 화면 링크는 운용자에게만 보인다(권한은 API가 지킨다)
  if (user.role === "admin" || user.role === "operator") actions.push(h("a", { href: "/admin/" }, icon("users"), S.adminLink));
  actions.push(logout);
  $("userActions").replaceChildren(...actions);

  const close = (focusButton) => {
    if (pop.hidden) return;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (focusButton) btn.focus();
  };
  btn.addEventListener("click", () => {
    const open = pop.hidden;
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) { const first = pop.querySelector("a, button"); if (first) first.focus(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(true); });
  document.addEventListener("click", (e) => { if (!menu.contains(e.target)) close(false); });
  menu.hidden = false;
}

function renderApp(cfg) {
  const app = cfg.app;
  renderUserMenu(cfg.user);
  const supersetUrl = String((cfg.superset && cfg.superset.url) || "").replace(/\/+$/, "");
  applyBrand(app.theme);

  document.title = app.title || "";
  $("title").textContent = app.title || "";
  $("logo").textContent = app.logo_text || Array.from(app.title || " ")[0];
  $("asOfLabel").textContent = S.asOf;
  $("asOf").textContent = app.as_of ? S.formatAsOf(app.as_of) : "";
  $("asOfWrap").hidden = !app.as_of;
  $("readOnly").replaceChildren(icon("lock"), S.readOnly);
  $("readOnly").title = S.readOnlyTitle;
  $("meta").hidden = false;

  const tabs = cfg.tabs;
  const widgetTypes = widgetTypeSet(cfg.widgets);
  const nav = $("tabs");
  const views = $("views");
  nav.replaceChildren();
  views.replaceChildren();
  if (!tabs.length) {
    // 일반사용자는 속한 그룹이 허용한 탭만 받는다. 없으면 운용자에게 요청하라고 안내한다
    const limited = cfg.user && cfg.user.role === "user";
    showAppState(stateBox(limited
      ? { tone: "neutral", iconName: "lock", title: S.noAccessTitle, body: S.noAccessBody, role: "status" }
      : { tone: "neutral", iconName: "warning", title: S.noTabs, role: "status" }));
    return;
  }

  const resizeHandlers = new Set();
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => resizeHandlers.forEach((fn) => fn()), 150);
  });

  let active = -1;
  const entries = tabs.map((tab, index) => {
    const link = h("a", { href: "#" + encodeURIComponent(tab.key), "data-key": tab.key },
      h("span", { class: "ico" }, icon(tab.icon)), h("span", { class: "lbl", text: tab.label }));
    link.title = tab.label;
    nav.append(link);
    const view = h("section", { class: "view", "aria-label": tab.label });
    views.append(view);
    const showHandlers = new Set();
    const ctx = {
      index, sectionIndex: 0, tabLabel: tab.label, supersetUrl, asOf: app.as_of, widgetTypes,
      isActive: () => active === index,
      onShow: (fn) => showHandlers.add(fn),
      onResize: (fn) => resizeHandlers.add(() => { if (active === index) fn(); }),
    };
    return { tab, link, view, ctx, showHandlers, rendered: false };
  });

  function route() {
    let key = "";
    try { key = decodeURIComponent(location.hash.slice(1)); } catch (e) { key = ""; }
    let idx = entries.findIndex((e) => e.tab.key === key);
    if (idx < 0) idx = 0;
    if (idx === active) return;
    active = idx;
    entries.forEach((e, i) => {
      const on = i === idx;
      e.view.classList.toggle("active", on);
      if (on) e.link.setAttribute("aria-current", "page"); else e.link.removeAttribute("aria-current");
    });
    const cur = entries[idx];
    try { cur.link.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) { /* 무시 */ }
    document.title = `${cur.tab.label} · ${app.title || ""}`;
    if (!cur.rendered) {
      cur.rendered = true;
      (cur.tab.sections || []).forEach((sec) => renderSection(cur.view, sec, cur.ctx));
    } else {
      cur.showHandlers.forEach((fn) => fn());
    }
  }
  window.addEventListener("hashchange", route);
  route();
}

/* ---------------- 시작 ---------------- */

$("skip").textContent = S.skipToContent;
$("skip").addEventListener("click", (e) => { e.preventDefault(); $("main").focus(); });
$("tabs").setAttribute("aria-label", S.navLabel);
root.lang = S.lang;
initTheme();
boot();
