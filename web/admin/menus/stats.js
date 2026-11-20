// 운용 화면: 통계(#stats). 탭별·사용자별 조회 수.
import { h } from "../../common.js";
import { T, view, api, notice, head, pagedTable, fmtDay } from "../kit.js";

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

export default {
  key: "stats",
  label: T.menu.stats,
  match: /^stats$/,
  render: statsView,
};
