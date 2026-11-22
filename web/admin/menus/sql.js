// 운용 화면: 읽기 전용 SQL 콘솔(#sql, MANAGE_FEATURES=sql).
import { h } from "../../common.js";
import { uid } from "../../shared/dom.js";
import { box, btn, dataFailText } from "../datakit.js";
import { T, view, session, api, head, isAdmin, isManageFeature } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
let gen = 0;

function renderSqlConsole(my) {
  const alive = () => my === gen;
  let lastSql = "";
  let currentOffset = 0;

  const sqlId = uid("sql");
  const limitId = uid("lim");
  const sqlInput = h("textarea", { id: sqlId, class: "input mono", rows: 7, spellcheck: "false" });
  const limitSelect = h("select", { id: limitId, class: "input" },
    [50, 100, 200].map((n) => h("option", { value: String(n), text: String(n) })));
  const runBtn = h("button", { type: "button", class: "btn primary" }, D.sqlRun);
  const makeDatasetBtn = isAdmin() ? btn(D.sqlMakeDataset, () => {
    if (!lastSql) return;
    session.pendingDatasetSql = lastSql;
    location.hash = "#datasets";
  }) : null;
  if (makeDatasetBtn) makeDatasetBtn.disabled = true;

  const statusBox = h("div");
  const resultBox = h("div");

  const runQuery = async (offset) => {
    const sqlText = sqlInput.value.trim();
    if (!sqlText) {
      statusBox.replaceChildren(box("bad", D.sqlNeedInput));
      return;
    }
    const limit = Number(limitSelect.value) || 50;
    runBtn.disabled = true;
    statusBox.replaceChildren();
    try {
      const res = await api("POST", `${BASE}/sql`, { sql: sqlText, limit, offset });
      if (!alive()) return;
      lastSql = sqlText;
      currentOffset = offset;
      if (makeDatasetBtn) makeDatasetBtn.disabled = false;

      const cols = res.columns || [];
      const rows = res.rows || [];
      const meta = h("p", { class: "a-note", text: D.sqlMeta(rows.length, res.elapsed_ms) });
      const truncNote = (res.truncated_cells || []).length
        ? h("p", { class: "a-note", text: D.sqlTruncatedNote })
        : null;

      const prevBtn = btn(D.sqlPrevPage, () => runQuery(Math.max(0, currentOffset - limit)));
      prevBtn.disabled = currentOffset <= 0;
      const nextBtn = btn(D.sqlNextPage, () => runQuery(currentOffset + limit));
      nextBtn.disabled = !res.has_more;
      const pager = h("div", { class: "a-pager" }, prevBtn, nextBtn);

      if (!rows.length) {
        resultBox.replaceChildren(meta, h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: D.sqlEmpty })), pager);
        return;
      }
      const thead = h("tr", {}, ...cols.map((c) => h("th", { scope: "col", text: c.name })));
      const tbodyRows = rows.map((row) =>
        h("tr", {}, ...cols.map((_c, idx) => {
          const val = row[idx];
          return h("td", { class: "wrap", text: val === null || val === undefined ? "—" : String(val) });
        })));
      resultBox.replaceChildren(
        meta,
        truncNote,
        h("div", { class: "a-table-wrap" }, h("table", { class: "a-table" }, h("thead", {}, thead), h("tbody", {}, ...tbodyRows))),
        pager,
      );
    } catch (err) {
      if (alive()) statusBox.replaceChildren(box("bad", dataFailText(err)));
    } finally {
      runBtn.disabled = false;
    }
  };

  runBtn.addEventListener("click", () => runQuery(0));
  sqlInput.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery(0);
    }
  });

  view().replaceChildren(
    head(D.sqlTitle, D.sqlIntro, makeDatasetBtn),
    h("section", { class: "a-card" },
      h("div", { class: "field d-field" },
        h("label", { for: sqlId, text: D.sqlLabel }),
        sqlInput,
        h("span", { class: "hint", text: D.sqlHint })),
      h("div", { class: "a-actions" },
        h("label", { class: "field" }, h("span", { text: D.sqlLimitLabel }), limitSelect),
        runBtn)),
    h("div", { class: "a-gap" }),
    statusBox,
    resultBox,
  );
  sqlInput.focus();
}

export default {
  key: "sql",
  label: T.menu.sql,
  match: /^sql$/,
  when: () => session.manage !== null && isManageFeature("sql"),
  render() {
    gen += 1;
    renderSqlConsole(gen);
  },
};
