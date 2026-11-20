// 운용 화면: 테스트 메일함(#outbox). 메일 모드가 test일 때만 메뉴에 있다.
import S from "../../strings.js";
import { icon } from "../../icons.js";
import { h } from "../../common.js";
import { T, view, session, api, head, pagedTable } from "../kit.js";

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

export default {
  key: "outbox",
  label: T.menu.outbox,
  match: /^outbox$/,
  when: () => session.mailMode === "test",
  render: outboxView,
};
