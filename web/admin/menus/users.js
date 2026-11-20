// 운용 화면: 사용자 목록(#users)과 사용자 상세(#user/<id>). 메뉴 목록의 첫 메뉴라 모르는 주소도 여기로 온다.
import S from "../../strings.js";
import { icon } from "../../icons.js";
import { h } from "../../common.js";
import { T, view, session, ApiFail, api, errorText, message, notice, head, field, checkbox, pagedTable, qs, fmtDay, detailId } from "../kit.js";

const ROLE_TONE = { admin: "primary", operator: "good", user: "neutral" };
const STATUS_TONE = { active: "good", suspended: "bad", invited: "primary", pending: "warn" };

function rolePill(role) { return h("span", { class: "pill tone", "data-tone": ROLE_TONE[role] || "neutral", text: S.roleLabel[role] || role }); }
function statusPill(u) {
  return h("span", { class: "pills" }, h("span", { class: "pill tone", "data-tone": STATUS_TONE[u.status] || "neutral", text: T.status[u.status] || u.status }),
    u.locked ? h("span", { class: "pill tone", "data-tone": "warn" }, icon("lock"), T.locked) : null);
}

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
    if (r !== "user" && session.me.role !== "admin") o.disabled = true;
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
    h("div", { class: "a-form" }, field(T.email, email), field(T.role, role, session.me.role === "admin" ? null : T.adminOnlyRoles),
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
  const isSelf = u.email === session.me.email;
  const canTouch = session.me.role === "admin" || u.role === "user";      // operator는 user만(서버도 검사한다)
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
  if (session.me.role === "admin") {
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

export default {
  key: "users",
  label: T.menu.users,
  match: /^(users|user\/.+)$/,
  render(hash) {
    allGroups = null;      // 그룹 목록은 화면을 옮길 때마다 다시 읽는다
    const id = detailId(hash, "user");
    if (id !== null) userView(id);
    else usersView();
  },
};
