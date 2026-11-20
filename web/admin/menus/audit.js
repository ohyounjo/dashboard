// 운용 화면: 감사 로그(#audit).
import { h } from "../../common.js";
import { T, view, api, head, pagedTable } from "../kit.js";

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

export default {
  key: "audit",
  label: T.menu.audit,
  match: /^audit$/,
  render: auditView,
};
