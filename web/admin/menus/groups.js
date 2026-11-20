// 운용 화면: 그룹 목록(#groups)과 그룹 상세(#group/<id>).
import S from "../../strings.js";
import { h } from "../../common.js";
import { T, view, api, errorText, message, notice, head, field, checkbox, pagedTable, qs, detailId } from "../kit.js";

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

export default {
  key: "groups",
  label: T.menu.groups,
  match: /^(groups|group\/.+)$/,
  render(hash) {
    const id = detailId(hash, "group");
    if (id !== null) groupView(id);
    else groupsView();
  },
};
