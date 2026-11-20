// 운용 화면: 데이터셋 목록(#datasets)과 데이터셋 상세(#dataset/<키>): 그래프가 실제로 보는 뷰(dash.<키>)의 행(설계서 9-1).
// 관리 서비스(/api/manage)가 떠 있을 때만 메뉴에 있다. 행 표는 테이블 미리보기와 같은 부품(../rows.js)이다.
// sql: 데이터셋은 전체 행 수가 없어 "다음"만 보이고, "건수 세기"로 count: true를 보내 다시 읽는다.
import { h } from "../../common.js";
import { rowsView, dataFailText } from "../rows.js";
import { T, view, session, api, head, pagedTable, detailId, message } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
let gen = 0;

const kindPill = (kind) => h("span", { class: "pill tone", "data-tone": kind === "table" ? "primary" : "neutral", text: D.kindLabel[kind] || kind });
const tableLink = (name) => (name ? h("a", { class: "a-link", href: `#table/${encodeURIComponent(name)}`, text: name }) : "—");

function listView() {
  let cache = null;
  const table = pagedTable({
    defaultSort: "key",
    empty: D.noDatasets,
    load: async (st) => {
      if (!cache) cache = (await api("GET", `${BASE}/datasets`)).items || [];
      const from = (st.page - 1) * st.size;
      return { items: cache.slice(from, from + st.size), total: cache.length };
    },
    rowHref: (d) => `#dataset/${encodeURIComponent(d.key)}`,
    columns: [
      { key: "key", label: D.datasetKey, wrap: true, render: (d) => h("span", { class: "d-stack" },
        h("a", { class: "a-link", href: `#dataset/${encodeURIComponent(d.key)}`, text: d.key }),
        d.description ? h("small", { class: "a-key", text: d.description }) : null) },
      { key: "kind", label: D.datasetKind, render: (d) => kindPill(d.kind) },
      { key: "table", label: D.readsTable, render: (d) => tableLink(d.table) },
      { key: "used_by", label: D.usedBy, num: true, render: (d) => D.num(d.used_by || 0) },
    ],
  });
  const refresh = h("button", { type: "button", class: "btn", onclick: () => { cache = null; table.state.page = 1; table.draw(); } }, D.refresh);
  view().replaceChildren(head(D.datasetsTitle, D.datasetsIntro, refresh), table.el);
  table.draw();
}

async function detailView(key, my) {
  const alive = () => my === gen;
  const crumb = h("a", { class: "a-crumb", href: "#datasets" }, "← ", D.backToDatasets);
  view().replaceChildren(crumb, h("div", { class: "sk-note", text: T.loading }));
  let ds;
  try {
    const items = (await api("GET", `${BASE}/datasets`)).items || [];
    ds = items.find((d) => d.key === key);
  } catch (err) {
    if (alive()) view().replaceChildren(crumb, message(dataFailText(err)));
    return;
  }
  if (!alive()) return;
  if (!ds) { view().replaceChildren(crumb, message(D.errors.unknown_dataset)); return; }
  const info = h("dl", { class: "a-infos" },
    h("div", { class: "a-info" }, h("dt", { text: D.datasetKind }), h("dd", {}, kindPill(ds.kind))),
    h("div", { class: "a-info" }, h("dt", { text: D.readsTable }), h("dd", {}, tableLink(ds.table))),
    h("div", { class: "a-info" }, h("dt", { text: D.usedBy }), h("dd", { text: D.num(ds.used_by || 0) })));
  const rows = rowsView({ path: `${BASE}/datasets/${encodeURIComponent(ds.key)}/rows/query`, labels: ds.labels || {}, alive });
  view().replaceChildren(crumb, head(ds.key, ds.description || (ds.kind === "sql" ? D.sqlDatasetNote : D.tableDatasetNote)),
    h("div", { class: "a-card" }, info), h("div", { class: "a-gap" }), rows.el);
}

export default {
  key: "datasets",
  label: T.menu.datasets,
  match: /^(datasets|dataset\/.+)$/,
  when: () => !!session.manage,
  render(hash) {
    gen += 1;
    const key = detailId(hash, "dataset");
    if (key !== null) detailView(key, gen);
    else listView();
  },
};
