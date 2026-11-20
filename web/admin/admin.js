// 운용 화면(contracts/auth.md "운용 화면"). 주소의 # 뒤로 메뉴를 고른다. 문구는 strings.js(S.admin)에만 둔다.
// 권한은 API가 지킨다. user 등급이 이 주소를 열면 API가 403이고 화면은 웹앱으로 돌려보낸다.
import S from "../strings.js";
import { icon } from "../icons.js";
import { applySavedTheme, h, loginUrl, postJson, readDetail } from "../common.js";

const T = S.admin;
const $ = (id) => document.getElementById(id);
const view = () => $("view");

/* ---------------- 네트워크 ---------------- */

class ApiFail extends Error {
  constructor(status, detail) { super(detail || String(status)); this.status = status; this.detail = detail; }
}

let leaving = false;
function leave(url) {
  if (leaving) return;
  leaving = true;
  location.replace(url);
}

/** JSON API. 401이면 로그인 화면, 403 forbidden이면 웹앱으로 보낸다. 실패는 ApiFail. */
async function api(method, path, body) {
  let res;
  try {
    res = method === "GET"
      ? await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } })
      : await fetch(path, {
        method, cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
  } catch (e) { throw new ApiFail(0, "network"); }
  if (res.status === 204) return null;
  if (res.ok) return res.json();
  const detail = await readDetail(res);
  if (res.status === 401) leave(loginUrl(location.pathname + location.hash, "expired"));
  else if (res.status === 403 && detail === "password_change_required") leave("/");
  else if (res.status === 403 && detail === "forbidden" && method === "GET") leave("/");
  throw new ApiFail(res.status, detail);
}

function errorText(err) {
  if (err instanceof ApiFail) {
    if (err.detail && typeof T.errors[err.detail] === "string") return T.errors[err.detail];
    if (err.status === 0) return T.errors.network;
    return T.errors.server(err.status);
  }
  return String(err && err.message || err);
}

function message(text, tone = "bad") {
  return h("div", { class: "a-msg", "data-tone": tone, role: tone === "bad" ? "alert" : "status" }, text);
}

/** 영역 하나의 안내 문구 자리. set(text, tone)으로 바꾸고 set(null)로 지운다. */
function notice() {
  const box = h("div");
  return { el: box, set(text, tone) { box.replaceChildren(text ? message(text, tone) : ""); } };
}

/* ---------------- 공통 조각 ---------------- */

const fmtDate = (iso) => (iso ? iso.slice(0, 10).replace(/-/g, ".") : "—");

function head(title, intro, ...actions) {
  return h("div", { class: "a-head" },
    h("div", {}, h("h2", { text: title }), intro ? h("p", { text: intro }) : null),
    actions.length ? h("div", { class: "a-actions" }, ...actions) : null);
}

function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("span", { class: "hint", text: hint }) : null);
}

function checkbox(label, checked, hint) {
  const input = h("input", { type: "checkbox" });
  input.checked = !!checked;
  return { input, el: h("label", { class: "check" }, input, h("span", {}, label, hint ? h("small", { text: hint }) : null)) };
}

/** 정렬·페이지 나눔 표. columns: [{key, label, sort?, num?, wrap?, render(item)}] */
function pagedTable({ columns, load, empty, defaultSort }) {
  const state = { page: 1, size: 50, sort: defaultSort };
  const wrap = h("div");
  async function draw() {
    let data;
    try {
      data = await load(state);
    } catch (err) { wrap.replaceChildren(message(errorText(err))); return; }
    if (!data.items.length && state.page === 1) { wrap.replaceChildren(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: empty }))); return; }
    const sortKey = state.sort.replace(/^-/, ""), desc = state.sort.startsWith("-");
    const thead = h("tr", {}, columns.map((c) => {
      const th = h("th", { scope: "col", class: c.num ? "num" : null });
      if (!c.sort) { th.textContent = c.label; return th; }
      if (sortKey === c.key) th.setAttribute("aria-sort", desc ? "descending" : "ascending");
      th.append(h("button", { type: "button", onclick: () => {
        state.sort = sortKey === c.key && !desc ? "-" + c.key : c.key;
        state.page = 1;
        draw();
      } }, c.label, sortKey === c.key ? (desc ? " ↓" : " ↑") : ""));
      return th;
    }));
    const rows = data.items.map((item) => h("tr", {}, columns.map((c) =>
      h("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : ""].join(" ").trim() || null }, c.render(item)))));
    const from = (state.page - 1) * state.size + 1, to = from + data.items.length - 1;
    const pager = h("div", { class: "a-pager" },
      h("span", { text: T.pageInfo(from, to, data.total) }),
      h("button", { type: "button", class: "btn small", disabled: state.page <= 1, onclick: () => { state.page -= 1; draw(); } }, T.prev),
      h("button", { type: "button", class: "btn small", disabled: to >= data.total, onclick: () => { state.page += 1; draw(); } }, T.next));
    wrap.replaceChildren(h("div", { class: "a-table-wrap" }, h("table", { class: "a-table" }, h("thead", {}, thead), h("tbody", {}, rows))),
      data.total > state.size ? pager : "");
  }
  return { el: wrap, draw, state };
}

const qs = (state) => `?page=${state.page}&size=${state.size}&sort=${encodeURIComponent(state.sort)}`;

/* ---------------- 그룹 ---------------- */

function groupsView() {
  const note = notice();
  const table = pagedTable({
    defaultSort: "name",
    empty: T.noGroups,
    load: (st) => api("GET", "/api/admin/groups" + qs(st)),
    columns: [
      { key: "name", label: T.groupName, sort: true, render: (g) => h("a", { class: "a-link", href: `#group/${encodeURIComponent(g.id)}`, text: g.name }) },
      { key: "description", label: T.groupDescription, wrap: true, render: (g) => g.description || "—" },
      { key: "member_count", label: T.memberCount, sort: true, num: true, render: (g) => String(g.member_count) },
      { key: "tab_count", label: T.tabCount, sort: true, num: true, render: (g) => String(g.tab_count) },
      { key: "is_default", label: T.groupDefault, sort: true, render: (g) => (g.is_default ? h("span", { class: "pill soft", text: T.groupDefault }) : "") },
    ],
  });

  const name = h("input", { type: "text", maxlength: 100, required: true, autocomplete: "off" });
  const desc = h("input", { type: "text", maxlength: 500, autocomplete: "off" });
  const def = checkbox(T.groupDefault, false, T.groupDefaultHint);
  const form = h("form", { class: "a-card", hidden: true },
    h("div", { class: "a-form" }, field(T.groupName, name), field(T.groupDescription, desc), h("div", { class: "full" }, def.el)),
    h("div", { class: "a-actions" },
      h("button", { type: "submit", class: "btn primary" }, T.save),
      h("button", { type: "button", class: "btn", onclick: () => { form.hidden = true; } }, T.cancel)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!name.value.trim()) { name.focus(); return; }
    try {
      const g = await api("POST", "/api/admin/groups", { name: name.value, description: desc.value, is_default: def.input.checked });
      location.hash = `#group/${encodeURIComponent(g.id)}`;
    } catch (err) { note.set(errorText(err)); }
  });
  const add = h("button", { type: "button", class: "btn primary", onclick: () => { form.hidden = false; name.focus(); } }, T.newGroup);
  view().replaceChildren(head(T.groupsTitle, T.groupsIntro, add), note.el, form, h("div", { class: "a-gap" }), table.el);
  table.draw();
}

function tabLabel(t) {
  return h("span", {}, t.label || t.tab_key, " ", h("small", { class: "num a-key", text: t.tab_key }));
}

async function groupView(id) {
  view().replaceChildren(h("div", { class: "sk-note", text: T.loading }));
  let g, tabs;
  try {
    [g, tabs] = await Promise.all([api("GET", `/api/admin/groups/${encodeURIComponent(id)}`), api("GET", "/api/admin/tabs")]);
  } catch (err) {
    view().replaceChildren(h("a", { class: "a-crumb", href: "#groups" }, "← ", T.backToGroups), message(errorText(err)));
    return;
  }
  const note = notice();
  const redraw = (msg, tone) => groupView(id).then(() => { if (msg) document.querySelector("#view .a-msg-slot")?.replaceChildren(message(msg, tone)); });

  // 이름·설명·기본 그룹
  const name = h("input", { type: "text", maxlength: 100, value: g.name, required: true, autocomplete: "off" });
  const desc = h("input", { type: "text", maxlength: 500, value: g.description, autocomplete: "off" });
  const def = checkbox(T.groupDefault, g.is_default, T.groupDefaultHint);
  const basics = h("form", { class: "a-card" },
    h("div", { class: "a-form" }, field(T.groupName, name), field(T.groupDescription, desc), h("div", { class: "full" }, def.el)),
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn primary" }, T.save)));
  basics.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("PATCH", `/api/admin/groups/${encodeURIComponent(id)}`, { name: name.value, description: desc.value, is_default: def.input.checked });
      redraw(T.saved, "good");
    } catch (err) { note.set(errorText(err)); }
  });

  // 허용 탭(현재 프로젝트)
  const current = new Set(g.tabs.filter((t) => t.current).map((t) => t.tab_key));
  const boxes = tabs.tabs.map((t) => ({ key: t.key, ...checkbox(t.label, current.has(t.key)) }));
  boxes.forEach((b) => b.el.querySelector("span").append(" ", h("small", { class: "num a-key", text: b.key })));
  const tabsForm = h("form", { class: "a-card" },
    h("h3", { text: T.groupTabs }), h("p", { text: T.groupTabsIntro(tabs.project) }),
    h("div", { class: "check-list" }, boxes.map((b) => b.el)),
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn primary" }, T.save)));
  tabsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    // 서버는 설정에 있는 탭만 바꾸고, 없는 탭 기록은 그대로 둔다(아래 묶음에서 하나씩 뺀다)
    const keys = boxes.filter((b) => b.input.checked).map((b) => b.key);
    try {
      await api("PUT", `/api/admin/groups/${encodeURIComponent(id)}/tabs`, { project: tabs.project, tab_keys: keys });
      redraw(T.saved, "good");
    } catch (err) { note.set(errorText(err)); }
  });

  // 다른 프로젝트·없는 탭
  const stale = g.tabs.filter((t) => !(t.current && t.exists));
  let staleCard = null;
  if (stale.length) {
    staleCard = h("div", { class: "a-card" }, h("h3", { text: T.staleTabs }), h("p", { text: T.staleTabsIntro }),
      h("div", {}, stale.map((t) => h("div", { class: "a-row" },
        h("div", { class: "who" }, h("b", {}, tabLabel(t)),
          h("span", { text: [t.current ? "" : T.otherProject(t.project), t.exists ? "" : T.missingTab].filter(Boolean).join(" · ") })),
        h("button", { type: "button", class: "btn small", onclick: async () => {
          // 이 항목 하나만 지운다(같은 프로젝트의 다른 없는 탭 기록은 그대로)
          try {
            await api("DELETE", `/api/admin/groups/${encodeURIComponent(id)}/tabs/${encodeURIComponent(t.project)}/${encodeURIComponent(t.tab_key)}`);
            redraw(T.saved, "good");
          } catch (err) { note.set(errorText(err)); }
        } }, T.remove)))));
  }

  // 구성원
  const area = h("textarea", { rows: 3, autocomplete: "off", spellcheck: "false" });
  const memberNote = notice();
  const addForm = h("form", { class: "a-form" },
    h("div", { class: "full" }, field(T.addMembers, area, T.addMembersHint)),
    h("div", { class: "a-actions full" }, h("button", { type: "submit", class: "btn primary" }, T.add)));
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const list = area.value.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
    if (!list.length) { area.focus(); return; }
    try {
      const r = await api("PUT", `/api/admin/groups/${encodeURIComponent(id)}/members`, { add: list, remove: [] });
      redraw(r.not_found.length ? T.notFound(r.not_found) : T.saved, r.not_found.length ? "warn" : "good");
    } catch (err) { memberNote.set(errorText(err)); }
  });
  const memberRows = g.members.length
    ? g.members.map((m) => h("div", { class: "a-row" },
      h("div", { class: "who" }, h("b", { text: m.email }), h("span", { text: `${S.roleLabel[m.role] || m.role} · ${T.status[m.status] || m.status}` })),
      h("button", { type: "button", class: "btn small", onclick: async () => {
        try {
          await api("PUT", `/api/admin/groups/${encodeURIComponent(id)}/members`, { add: [], remove: [m.id] });
          redraw(T.saved, "good");
        } catch (err) { memberNote.set(errorText(err)); }
      } }, T.remove)))
    : [h("div", { class: "a-empty", text: T.noMembers })];
  const membersCard = h("div", { class: "a-card" }, h("h3", { text: `${T.members} (${g.members.length})` }),
    memberNote.el, h("div", {}, memberRows), addForm);

  const del = h("button", { type: "button", class: "btn danger", onclick: async () => {
    if (!window.confirm(T.deleteGroupConfirm(g.name))) return;
    try {
      await api("DELETE", `/api/admin/groups/${encodeURIComponent(id)}`);
      location.hash = "#groups";
    } catch (err) { note.set(errorText(err)); }
  } }, T.del);

  view().replaceChildren(
    h("a", { class: "a-crumb", href: "#groups" }, "← ", T.backToGroups),
    head(g.name, g.description || null, del),
    h("div", { class: "a-msg-slot" }), note.el, basics,
    h("div", { class: "a-grid" }, h("div", { class: "a-col" }, tabsForm, staleCard), membersCard));
}

/* ---------------- 테스트 메일함 ---------------- */

function outboxView() {
  const table = pagedTable({
    defaultSort: "-created_at",
    empty: T.outboxEmpty,
    // 서버가 최근 순으로 준다(정렬 열 없음)
    load: (st) => api("GET", `/api/admin/outbox?page=${st.page}&size=${st.size}`),
    columns: [
      { key: "to_email", label: T.outboxTo, render: (m) => h("span", {}, m.to_email, " ", h("small", { class: "a-key", text: S.roleLabel[m.role] || m.role })) },
      { key: "kind", label: T.outboxKind, render: (m) => T.mailKind[m.kind] || m.kind },
      { key: "created_at", label: T.outboxAt, render: (m) => h("span", { class: "num", text: T.fmtDateTime(m.created_at) }) },
      { key: "link", label: T.outboxLink, render: (m) => (m.link ? h("a", { class: "a-link", href: m.link, target: "_blank", rel: "noopener noreferrer" }, T.openLink) : "—") },
    ],
  });
  view().replaceChildren(head(T.outboxTitle, T.outboxIntro, h("button", { type: "button", class: "btn", onclick: () => table.draw() }, icon("refresh"), S.retry)), table.el);
  table.draw();
}

/* ---------------- 셸 ---------------- */

let mailMode = "test";

const MENUS = [
  { key: "groups", label: T.menu.groups, match: /^(groups|group\/.+)$/ },
  { key: "outbox", label: T.menu.outbox, match: /^outbox$/, when: () => mailMode === "test" },
];
const menus = () => MENUS.filter((m) => !m.when || m.when());

function route() {
  const hash = location.hash.replace(/^#/, "") || "groups";
  const menu = menus().find((m) => m.match.test(hash)) || MENUS[0];
  $("nav").querySelectorAll("a").forEach((a) => {
    if (a.dataset.key === menu.key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = `${menu.label} · ${T.title}`;
  const m = hash.match(/^group\/(.+)$/);
  if (m) { let id = ""; try { id = decodeURIComponent(m[1]); } catch (e) { id = ""; } groupView(id); }
  else if (menu.key === "outbox") outboxView();
  else groupsView();
  $("main").focus({ preventScroll: true });
}

async function boot() {
  applySavedTheme();
  document.documentElement.lang = S.lang;
  $("skip").textContent = S.skipToContent;
  $("skip").addEventListener("click", (e) => { e.preventDefault(); $("main").focus(); });
  $("title").textContent = T.title;
  $("back").replaceChildren(icon("home"), T.back);
  $("nav").setAttribute("aria-label", T.title);
  try {
    const res = await fetch("/api/auth/config", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.ok) mailMode = (await res.json()).mail_mode || mailMode;
  } catch (e) { /* 기본값 */ }
  if (mailMode === "test") { $("banner").textContent = S.auth.mailTestBanner; $("banner").hidden = false; }
  $("nav").replaceChildren(...menus().map((m) => h("a", { href: `#${m.key}`, "data-key": m.key, text: m.label })));
  view().replaceChildren(h("div", { class: "sk-note", text: T.loading }));
  let me;
  try {
    const res = await fetch("/api/app", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.status === 401) { leave(loginUrl(location.pathname + location.hash)); return; }
    if (!res.ok) throw new ApiFail(res.status, await readDetail(res));
    me = (await res.json()).user;
  } catch (err) {
    if (err instanceof ApiFail && err.detail === "password_change_required") { leave("/"); return; }
    view().replaceChildren(message(errorText(err)));
    return;
  }
  if (!me || (me.role !== "admin" && me.role !== "operator")) { leave("/"); return; }
  $("who").textContent = `${me.email} · ${S.roleLabel[me.role] || me.role}`;
  const logout = $("logout");
  logout.textContent = S.logout;
  logout.hidden = false;
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    try { await postJson("/api/auth/logout", {}); } catch (e) { /* 그래도 로그인 화면으로 */ }
    leave(loginUrl("/", "logout"));
  });
  window.addEventListener("hashchange", route);
  route();
}

boot();
