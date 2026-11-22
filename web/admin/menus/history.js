// 운용 화면: 설정 변경 이력(#history, admin). 구조 변경마다 남는 변경 전 사본(contracts/ingest.md "설정 변경 이력").
// "이 변경 되돌리기"는 그 작업이 바꾼 파일을 함께 변경 전으로 되돌리고 apply한다(되돌리기도 이력이 하나 더 생긴다).
// "설정 파일 내려받기"(GET /export)로 받은 zip을 사람이 저장소에 커밋한다. 문구는 strings.js의 S.admin.data에만 둔다.
import { h } from "../../common.js";
import { btn, runJob } from "../datakit.js";
import { configResult, configFail } from "../structure.js";
import { T, view, session, api, head, pagedTable } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
let gen = 0;

const isAdmin = () => ((session.manage && session.manage.role) || (session.me && session.me.role)) === "admin";

function historyView(my) {
  const alive = () => my === gen;
  let cache = null;            // {items, revision}
  const out = h("div", { class: "d-out" });

  async function restore(item, trigger) {
    if (!window.confirm(D.restoreConfirm(T.fmtDateTime(item.at), D.historyAction[item.action] || item.action))) return;
    trigger.disabled = true;
    try {
      // revision은 목록을 읽을 때 받은 값(그 사이 설정이 바뀌었으면 서버가 409 stale_config)
      const result = await runJob(() => api("POST", `${BASE}/history/${encodeURIComponent(item.id)}/restore`, { revision: cache.revision }), out, alive);
      if (!result || !alive()) return;
      out.replaceChildren(configResult(result, D.historyRestored));
      cache = null;
      table.state.page = 1;
      table.draw();
    } catch (err) {
      if (!alive()) return;
      trigger.disabled = false;
      out.replaceChildren(configFail(err, { retry: () => { cache = null; table.draw(); out.replaceChildren(); } }));
    }
  }

  const table = pagedTable({
    defaultSort: "-at",
    empty: D.historyEmpty,
    load: async (st) => {
      if (!cache) {
        const r = await api("GET", `${BASE}/history`);
        cache = { items: r.items || [], revision: r.revision };
      }
      const from = (st.page - 1) * st.size;
      return { items: cache.items.slice(from, from + st.size), total: cache.items.length };
    },
    columns: [
      { key: "at", label: D.historyAt, render: (m) => h("span", { class: "num", text: T.fmtDateTime(m.at) }) },
      { key: "action", label: D.historyActionLabel, render: (m) => h("span", { class: "d-stack" },
        h("span", { title: m.action, text: D.historyAction[m.action] || m.action }),
        m.irreversible ? h("small", { class: "a-key", text: D.irreversibleBadge }) : null) },
      { key: "files", label: D.historyFiles, wrap: true, render: (m) => h("span", { class: "d-stack" },
        (m.files || []).map((f) => h("span", { class: "mono", text: f.before == null ? `${f.path} ${D.fileCreated}` : f.after == null ? `${f.path} ${D.fileDeleted}` : f.path }))) },
      { key: "act", label: D.actions, render: (m) => {
        if (m.irreversible) return null;
        const b = btn(D.restoreThis, () => restore(m, b), "small");
        b.setAttribute("aria-label", D.restoreThisAt(T.fmtDateTime(m.at)));
        return b;
      } },
    ],
  });
  const download = h("a", { class: "btn", href: `${BASE}/export`, download: "" }, D.exportConfig);
  const refresh = btn(D.refresh, () => { cache = null; table.state.page = 1; table.draw(); out.replaceChildren(); });
  view().replaceChildren(head(D.historyTitle, D.historyIntro, download, refresh), out, h("div", { class: "a-gap" }), table.el);
  table.draw();
}

export default {
  key: "history",
  label: T.menu.history,
  match: /^history$/,
  when: () => !!session.manage && isAdmin(),
  render() {
    gen += 1;
    historyView(gen);
  },
};
