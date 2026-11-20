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
  else if (res.status === 403 && detail === "password_change_required") leave("/account.html");
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

/** 정렬·페이지 나눔 표. columns: [{key, label, sort?, num?, wrap?, render(item)}]
 *  select: {id(item), label(item), onChange(Set)}를 주면 맨 앞에 고르기 체크박스 열(고른 것은 페이지를 넘겨도 남는다). */
function pagedTable({ columns, load, empty, defaultSort, select }) {
  const state = { page: 1, size: 50, sort: defaultSort, selected: new Set() };
  const wrap = h("div");
  async function draw() {
    let data;
    try {
      data = await load(state);
    } catch (err) { wrap.replaceChildren(message(errorText(err))); return; }
    if (!data.items.length && state.page === 1) { wrap.replaceChildren(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: empty }))); return; }
    const sortKey = state.sort.replace(/^-/, ""), desc = state.sort.startsWith("-");
    const changed = () => { if (select) select.onChange(state.selected); };
    let allBox = null;
    const rowBoxes = [];
    if (select) {
      allBox = h("input", { type: "checkbox", "aria-label": T.selectAll });
      allBox.checked = data.items.length > 0 && data.items.every((i) => state.selected.has(select.id(i)));
      allBox.addEventListener("change", () => {
        data.items.forEach((i) => (allBox.checked ? state.selected.add(select.id(i)) : state.selected.delete(select.id(i))));
        rowBoxes.forEach((b) => { b.checked = allBox.checked; });
        changed();
      });
    }
    const thead = h("tr", {}, select ? h("th", { scope: "col", class: "pick" }, allBox) : null, columns.map((c) => {
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
    const rows = data.items.map((item) => {
      let pick = null;
      if (select) {
        const box = h("input", { type: "checkbox", "aria-label": select.label(item) });
        box.checked = state.selected.has(select.id(item));
        box.addEventListener("change", () => {
          if (box.checked) state.selected.add(select.id(item)); else state.selected.delete(select.id(item));
          allBox.checked = data.items.every((i) => state.selected.has(select.id(i)));
          changed();
        });
        rowBoxes.push(box);
        pick = h("td", { class: "pick" }, box);
      }
      return h("tr", {}, pick, columns.map((c) =>
        h("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : ""].join(" ").trim() || null }, c.render(item))));
    });
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

/* ---------------- 사용자 ---------------- */

const fmtDay = (iso) => (iso ? T.fmtDateTime(iso).slice(0, 10) : "—");
const ROLE_TONE = { admin: "primary", operator: "good", user: "neutral" };
const STATUS_TONE = { active: "good", suspended: "bad", invited: "primary", pending: "warn" };

function rolePill(role) { return h("span", { class: "pill tone", "data-tone": ROLE_TONE[role] || "neutral", text: S.roleLabel[role] || role }); }
function statusPill(u) {
  return h("span", { class: "pills" }, h("span", { class: "pill tone", "data-tone": STATUS_TONE[u.status] || "neutral", text: T.status[u.status] || u.status }),
    u.locked ? h("span", { class: "pill tone", "data-tone": "warn" }, icon("lock"), T.locked) : null);
}

let me = null;   // 로그인한 운용자 {email, role}
let allGroups = null;
/** 모든 그룹(마지막 페이지까지). 다 읽지 못하면 실패한다(일부만 가지고 사용자의 그룹을 통째로 저장하지 않게). */
async function groupList() {
  if (allGroups) return allGroups;
  const items = [];
  for (let page = 1; ; page += 1) {
    const r = await api("GET", `/api/admin/groups?size=200&page=${page}`);
    items.push(...r.items);
    if (!r.items.length || items.length >= r.total) {
      if (items.length < r.total) throw new ApiFail(0, "network");
      break;
    }
  }
  allGroups = items;
  return allGroups;
}

function groupChecks(groups, selected) {
  if (!groups.length) return { el: h("p", { class: "a-note", text: T.noGroupsToPick }), values: () => [] };
  const boxes = groups.map((g) => ({ id: g.id, ...checkbox(g.name, selected.has(g.id)) }));
  return { el: h("div", { class: "check-list cols" }, boxes.map((b) => b.el)), values: () => boxes.filter((b) => b.input.checked).map((b) => b.id) };
}

function roleSelect(value) {
  const sel = h("select", { class: "sel" });
  for (const r of ["user", "operator", "admin"]) {
    const o = h("option", { value: r, text: S.roleLabel[r] });
    if (r !== "user" && me.role !== "admin") o.disabled = true;
    if (r === value) o.selected = true;
    sel.append(o);
  }
  return sel;
}

async function usersView() {
  const note = notice();
  const groups = await groupList().catch(() => []);
  const bar = h("div", { class: "a-bulk", hidden: true });
  const table = pagedTable({
    defaultSort: "email",
    empty: T.noUsers,
    load: (st) => api("GET", "/api/admin/users" + qs(st)),
    select: {
      id: (u) => u.id,
      label: (u) => T.selectUser(u.email),
      onChange: (set) => { bar.hidden = set.size === 0; count.textContent = T.selected(set.size); },
    },
    columns: [
      { key: "email", label: T.email, sort: true, render: (u) => h("a", { class: "a-link", href: `#user/${encodeURIComponent(u.id)}`, text: u.email }) },
      { key: "role", label: T.role, sort: true, render: (u) => rolePill(u.role) },
      { key: "status", label: T.state, sort: true, render: statusPill },
      { key: "groups", label: T.groupsCol, sort: true, wrap: true, render: (u) => u.groups.map((g) => g.name).join(", ") || "—" },
      { key: "created_at", label: T.createdAt, sort: true, render: (u) => h("span", { class: "num", text: fmtDay(u.created_at) }) },
      { key: "login_count", label: T.loginCount, sort: true, num: true, render: (u) => String(u.login_count) },
      { key: "last_login_at", label: T.lastLogin, sort: true, render: (u) => h("span", { class: "num", text: fmtDay(u.last_login_at) }) },
      { key: "source", label: T.source, sort: true, render: (u) => T.sourceLabel[u.source] || u.source },
    ],
  });

  // 일괄 그룹 지정
  const count = h("span", { class: "a-note" });
  const groupSel = h("select", { class: "sel", "aria-label": T.bulkGroup }, groups.map((g) => h("option", { value: g.id, text: g.name })));
  const bulk = async (mode) => {
    const ids = [...table.state.selected];
    if (!ids.length || !groupSel.value) return;
    try {
      await api("POST", "/api/admin/users/bulk-groups", { user_ids: ids, add: mode === "add" ? [groupSel.value] : [], remove: mode === "remove" ? [groupSel.value] : [] });
      table.state.selected.clear(); bar.hidden = true; note.set(T.done, "good"); table.draw();
    } catch (err) { note.set(errorText(err)); }
  };
  bar.append(count, groupSel,
    h("button", { type: "button", class: "btn small", disabled: !groups.length, onclick: () => bulk("add") }, T.bulkAdd),
    h("button", { type: "button", class: "btn small", disabled: !groups.length, onclick: () => bulk("remove") }, T.bulkRemove));

  // 사용자 추가
  const email = h("input", { type: "email", maxlength: 320, autocomplete: "off", spellcheck: "false" });
  const role = roleSelect("user");
  const gpick = groupChecks(groups, new Set());
  const addForm = h("form", { class: "a-card", hidden: true },
    h("h3", { text: T.addUser }), h("p", { text: T.addUserIntro }),
    h("div", { class: "a-form" }, field(T.email, email), field(T.role, role, me.role === "admin" ? null : T.adminOnlyRoles),
      h("div", { class: "full" }, h("span", { class: "a-label", text: T.groupsPick }), gpick.el)),
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn primary" }, T.invite),
      h("button", { type: "button", class: "btn", onclick: () => { addForm.hidden = true; } }, T.cancel)));
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!email.value.trim()) { email.focus(); return; }
    try {
      const u = await api("POST", "/api/admin/users", { email: email.value.trim(), role: role.value, group_ids: gpick.values() });
      location.hash = `#user/${encodeURIComponent(u.id)}`;
    } catch (err) { note.set(errorText(err)); }
  });

  // 일괄 초대
  const area = h("textarea", { rows: 6, spellcheck: "false", autocomplete: "off" });
  const bpick = groupChecks(groups, new Set());
  const bulkResult = h("div");
  const bulkForm = h("form", { class: "a-card", hidden: true },
    h("h3", { text: T.bulkInvite }), h("p", { text: T.bulkInviteIntro }),
    field(T.bulkEmails, area), h("div", {}, h("span", { class: "a-label", text: T.groupsPick }), bpick.el), bulkResult,
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn primary" }, T.invite),
      h("button", { type: "button", class: "btn", onclick: () => { bulkForm.hidden = true; } }, T.cancel)));
  bulkForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const list = area.value.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
    if (!list.length) { area.focus(); return; }
    try {
      const r = await api("POST", "/api/admin/users/bulk-invite", { emails: list, group_ids: bpick.values() });
      const made = r.results.filter((x) => x.result === "created").length;
      bulkResult.replaceChildren(message(T.bulkSummary(made, r.results.length), made === r.results.length ? "good" : "warn"),
        h("div", { class: "a-table-wrap" }, h("table", { class: "a-table" }, h("tbody", {}, r.results.map((x) =>
          h("tr", {}, h("td", { text: x.email }), h("td", { text: T.bulkResult[x.result] || x.result })))))));
      table.draw();
    } catch (err) { note.set(errorText(err)); }
  });

  const addBtn = h("button", { type: "button", class: "btn primary", onclick: () => { addForm.hidden = false; bulkForm.hidden = true; email.focus(); } }, T.addUser);
  const bulkBtn = h("button", { type: "button", class: "btn", onclick: () => { bulkForm.hidden = false; addForm.hidden = true; area.focus(); } }, T.bulkInvite);
  view().replaceChildren(head(T.usersTitle, T.usersIntro, addBtn, bulkBtn), note.el, addForm, bulkForm, bar, h("div", { class: "a-gap" }), table.el);
  table.draw();
}

function infoRow(label, value) {
  return h("div", { class: "a-info" }, h("dt", { text: label }), h("dd", {}, value));
}

async function userView(id) {
  view().replaceChildren(h("div", { class: "sk-note", text: T.loading }));
  let u, groups;
  try {
    [u, groups] = await Promise.all([api("GET", `/api/admin/users/${encodeURIComponent(id)}`), groupList()]);
  } catch (err) {
    view().replaceChildren(h("a", { class: "a-crumb", href: "#users" }, "← ", T.backToUsers), message(errorText(err)));
    return;
  }
  const note = notice();
  const path = `/api/admin/users/${encodeURIComponent(id)}`;
  const isSelf = u.email === me.email;
  const canTouch = me.role === "admin" || u.role === "user";      // operator는 user만(서버도 검사한다)
  const redraw = (msg, tone) => userView(id).then(() => { if (msg) document.querySelector("#view .a-msg-slot")?.replaceChildren(message(msg, tone)); });
  const act = async (fn, okMsg) => {
    try { await fn(); redraw(okMsg || T.done, "good"); } catch (err) { note.set(errorText(err)); window.scrollTo(0, 0); }
  };

  const info = h("dl", { class: "a-infos" },
    infoRow(T.role, rolePill(u.role)),
    infoRow(T.state, h("span", {}, statusPill(u), u.locked_until && u.locked ? " " + T.lockedUntil(T.fmtDateTime(u.locked_until)) : "")),
    infoRow(T.groupsCol, u.groups.map((g) => g.name).join(", ") || "—"),
    infoRow(T.createdAt, fmtDay(u.created_at)),
    infoRow(T.source, T.sourceLabel[u.source] || u.source),
    infoRow(T.createdBy, u.created_by_email || "—"),
    infoRow(T.loginCount, String(u.login_count)),
    infoRow(T.lastLogin, u.last_login_at ? T.fmtDateTime(u.last_login_at) : "—"),
    infoRow(T.passwordChanged, u.password_changed_at ? T.fmtDateTime(u.password_changed_at) : "—"),
    infoRow(T.failedCount, String(u.failed_count)),
    u.must_change_password ? infoRow("", T.mustChange) : null);

  // 그룹 편집
  const gpick = groupChecks(groups, new Set(u.groups.map((g) => g.id)));
  const groupsCard = h("form", { class: "a-card" }, h("h3", { text: T.groupsCol }), gpick.el,
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn primary", disabled: !canTouch || !groups.length }, T.saveGroups)));
  groupsCard.addEventListener("submit", (e) => { e.preventDefault(); act(() => api("PATCH", path, { group_ids: gpick.values() })); });

  // 동작
  const temp = h("div");
  const buttons = [];
  if (me.role === "admin") {
    const role = roleSelect(u.role);
    buttons.push(h("div", { class: "a-inline" }, role, h("button", { type: "button", class: "btn", disabled: isSelf, onclick: () => {
      if (role.value === u.role || !window.confirm(T.confirmRole(u.email, S.roleLabel[role.value]))) return;
      act(() => api("PATCH", path, { role: role.value }));
    } }, T.changeRole)));
  }
  if (u.status === "active") {
    buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: () => {
      if (window.confirm(T.confirmResetMail(u.email))) act(() => api("POST", `${path}/reset-password`, { method: "mail" }));
    } }, T.resetMail));
  }
  if (u.status === "active" || u.status === "suspended") {
    buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: async () => {
      if (!window.confirm(T.confirmTemp(u.email))) return;
      try {
        const r = await api("POST", `${path}/reset-password`, { method: "temporary" });
        const code = h("code", { class: "a-secret", text: r.password });
        const copy = h("button", { type: "button", class: "btn small", onclick: async () => {
          try { await navigator.clipboard.writeText(r.password); copy.textContent = T.copied; } catch (e) { /* 직접 복사 */ }
        } }, T.copy);
        temp.replaceChildren(h("div", { class: "a-msg", "data-tone": "warn", role: "alert" }, T.tempIssued, h("div", { class: "a-inline" }, code, copy)));
      } catch (err) { note.set(errorText(err)); }
    } }, T.resetTemp));
  }
  if (u.status === "invited") buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: () => act(() => api("POST", `${path}/resend-invite`, {}), T.created) }, T.resendInvite));
  if (u.locked || u.failed_count > 0) buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: () => act(() => api("POST", `${path}/unlock`, {})) }, T.unlock));
  buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: () => act(() => api("POST", `${path}/logout-all`, {})) }, T.logoutAll));
  if (u.status === "active") buttons.push(h("button", { type: "button", class: "btn danger", disabled: !canTouch || isSelf, onclick: () => {
    if (window.confirm(T.confirmSuspend(u.email))) act(() => api("PATCH", path, { status: "suspended" }));
  } }, T.suspend));
  if (u.status === "suspended") buttons.push(h("button", { type: "button", class: "btn", disabled: !canTouch, onclick: () => act(() => api("PATCH", path, { status: "active" })) }, T.unsuspend));
  const actions = h("div", { class: "a-card" }, h("h3", { text: T.actions }), temp, h("div", { class: "a-actions" }, buttons));

  // 삭제(이메일 다시 입력)
  const confirmEmail = h("input", { type: "email", autocomplete: "off", spellcheck: "false" });
  const delForm = h("form", { class: "a-card danger-zone" }, h("h3", { text: T.deleteUser }), h("p", { text: T.deleteIntro }),
    field(T.deleteConfirmLabel, confirmEmail),
    h("div", { class: "a-actions" }, h("button", { type: "submit", class: "btn danger", disabled: !canTouch || isSelf }, T.deleteUser)));
  delForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (confirmEmail.value.trim().toLowerCase() !== u.email) { note.set(T.deleteMismatch); confirmEmail.focus(); return; }
    try { await api("DELETE", path); location.hash = "#users"; } catch (err) { note.set(errorText(err)); }
  });

  view().replaceChildren(
    h("a", { class: "a-crumb", href: "#users" }, "← ", T.backToUsers),
    head(u.email, null),
    h("div", { class: "a-msg-slot" }), note.el,
    h("div", { class: "a-grid" }, h("div", { class: "a-col" }, h("div", { class: "a-card" }, info), groupsCard), h("div", { class: "a-col" }, actions, delForm)));
}

/* ---------------- 통계 ---------------- */

function statsView() {
  const state = { days: 30 };
  const note = notice();
  const sel = h("select", { class: "sel", "aria-label": T.period }, [7, 30, 90].map((d) => h("option", { value: d, text: T.days(d), selected: d === state.days })));
  const intro = h("p", { class: "a-note" });
  const tabs = pagedTable({
    defaultSort: "order",
    empty: T.noStats,
    load: async (st) => {
      const r = await api("GET", `/api/admin/stats/tabs?days=${state.days}&page=${st.page}&size=${st.size}&sort=${encodeURIComponent(st.sort)}`);
      intro.textContent = T.statsIntro(r.project);
      return r;
    },
    columns: [
      { key: "order", label: T.tab, sort: true, render: (t) => h("span", {}, t.label, " ", h("small", { class: "a-key", text: t.key })) },
      { key: "views", label: T.views, sort: true, num: true, render: (t) => String(t.views) },
      { key: "viewers", label: T.viewers, sort: true, num: true, render: (t) => String(t.viewers) },
      { key: "last_viewed_at", label: T.lastViewed, sort: true, render: (t) => (t.never ? h("span", { class: "pill tone", "data-tone": "warn", text: T.neverOpened }) : h("span", { class: "num", text: T.fmtDateTime(t.last_viewed_at) })) },
    ],
  });
  const users = pagedTable({
    defaultSort: "-views",
    empty: T.noStats,
    load: (st) => api("GET", `/api/admin/stats/users?days=${state.days}&page=${st.page}&size=${st.size}&sort=${encodeURIComponent(st.sort)}`),
    columns: [
      { key: "email", label: T.email, sort: true, render: (u) => h("a", { class: "a-link", href: `#user/${encodeURIComponent(u.id)}`, text: u.email }) },
      { key: "views", label: T.views, sort: true, num: true, render: (u) => String(u.views) },
      { key: "last_viewed_at", label: T.lastViewed, sort: true, render: (u) => h("span", { class: "num", text: u.last_viewed_at ? T.fmtDateTime(u.last_viewed_at) : "—" }) },
      { key: "last_login_at", label: T.lastLogin, sort: true, render: (u) => h("span", {}, h("span", { class: "num", text: u.last_login_at ? fmtDay(u.last_login_at) : "—" }),
        u.inactive ? [" ", h("span", { class: "pill tone", "data-tone": "warn", text: T.inactive })] : null) },
    ],
  });
  sel.addEventListener("change", () => { state.days = Number(sel.value); tabs.state.page = users.state.page = 1; tabs.draw(); users.draw(); });
  view().replaceChildren(head(T.statsTitle, null, h("label", { class: "a-inline" }, T.period, sel)), intro, note.el,
    h("h3", { class: "a-sub", text: T.byTab }), tabs.el, h("h3", { class: "a-sub", text: T.byUser }), users.el);
  tabs.draw();
  users.draw();
}

/* ---------------- 감사 로그 ---------------- */

function auditDetail(d) {
  if (!d || typeof d !== "object") return "";
  return Object.entries(d).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
}

function auditView() {
  const filter = { action: "", from: "", to: "" };
  const actionSel = h("select", { class: "sel", "aria-label": T.auditAction });
  const from = h("input", { type: "date", class: "sel", "aria-label": T.from });
  const to = h("input", { type: "date", class: "sel", "aria-label": T.to });
  let known = null;
  const fillActions = (actions) => {
    if (known && known.join() === actions.join()) return;
    known = actions;
    const cur = actionSel.value;
    actionSel.replaceChildren(h("option", { value: "", text: T.allActions }),
      ...Object.entries(T.actionGroups).map(([v, l]) => h("option", { value: v, text: l })),
      ...actions.map((a) => h("option", { value: a, text: T.actionLabel[a] ? `${T.actionLabel[a]} (${a})` : a })));
    actionSel.value = cur;
  };
  fillActions([]);
  const table = pagedTable({
    defaultSort: "-at",
    empty: T.noAudit,
    load: async (st) => {
      const q = new URLSearchParams({ page: st.page, size: st.size });
      if (filter.action) q.set("action", filter.action);
      if (filter.from) q.set("from", filter.from);
      if (filter.to) q.set("to", filter.to);
      const r = await api("GET", `/api/admin/audit?${q}`);
      fillActions(r.actions || []);
      return r;
    },
    columns: [
      { key: "at", label: T.auditAt, render: (e) => h("span", { class: "num", text: T.fmtDateTime(e.at) }) },
      { key: "actor_email", label: T.auditActor, render: (e) => e.actor_email || "—" },
      { key: "action", label: T.auditAction, render: (e) => h("span", { title: e.action, text: T.actionLabel[e.action] || e.action }) },
      { key: "target", label: T.auditTarget, wrap: true, render: (e) => e.target_label || "—" },
      { key: "detail", label: T.auditDetail, wrap: true, render: (e) => h("span", { class: "a-detail", text: auditDetail(e.detail) }) },
      { key: "ip", label: T.auditIp, render: (e) => h("span", { class: "num", text: e.ip || "—" }) },
    ],
  });
  const form = h("form", { class: "a-filter" }, actionSel, h("label", { class: "a-inline" }, T.from, from), h("label", { class: "a-inline" }, T.to, to),
    h("button", { type: "submit", class: "btn" }, T.apply));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    Object.assign(filter, { action: actionSel.value, from: from.value, to: to.value });
    table.state.page = 1;
    table.draw();
  });
  view().replaceChildren(head(T.auditTitle, T.auditIntro), form, h("div", { class: "a-gap" }), table.el);
  table.draw();
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
  { key: "users", label: T.menu.users, match: /^(users|user\/.+)$/ },
  { key: "groups", label: T.menu.groups, match: /^(groups|group\/.+)$/ },
  { key: "stats", label: T.menu.stats, match: /^stats$/ },
  { key: "audit", label: T.menu.audit, match: /^audit$/ },
  { key: "outbox", label: T.menu.outbox, match: /^outbox$/, when: () => mailMode === "test" },
];
const menus = () => MENUS.filter((m) => !m.when || m.when());

function route() {
  const hash = location.hash.replace(/^#/, "") || "users";
  const menu = menus().find((m) => m.match.test(hash)) || MENUS[0];
  $("nav").querySelectorAll("a").forEach((a) => {
    if (a.dataset.key === menu.key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = `${menu.label} · ${T.title}`;
  allGroups = null;      // 그룹 목록은 화면을 옮길 때마다 다시 읽는다
  const m = hash.match(/^(group|user)\/(.+)$/);
  let id = "";
  if (m) { try { id = decodeURIComponent(m[2]); } catch (e) { id = ""; } }
  if (m && m[1] === "group") groupView(id);
  else if (m && m[1] === "user") userView(id);
  else if (menu.key === "outbox") outboxView();
  else if (menu.key === "stats") statsView();
  else if (menu.key === "audit") auditView();
  else if (menu.key === "groups") groupsView();
  else usersView();
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
  try {
    const res = await fetch("/api/app", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.status === 401) { leave(loginUrl(location.pathname + location.hash)); return; }
    if (!res.ok) throw new ApiFail(res.status, await readDetail(res));
    me = (await res.json()).user;
  } catch (err) {
    if (err instanceof ApiFail && err.detail === "password_change_required") { leave("/account.html"); return; }
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
