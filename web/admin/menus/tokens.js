// 운용 화면: 자동 적재용 API 토큰 관리(#tokens, admin 전용, MANAGE_FEATURES=tokens).
import { h } from "../../common.js";
import { uid } from "../../shared/dom.js";
import { box, btn, pill, dataFailText } from "../datakit.js";
import { T, view, session, api, head, fmtDay, isAdmin, isManageFeature, message } from "../kit.js";

const D = T.data;
const BASE = "/api/manage";
let gen = 0;

function defaultExpiryDate() {
  const d = new Date(Date.now() + 30 * 86400 * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function maxExpiryDate() {
  const d = new Date(Date.now() + 365 * 86400 * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function issuedBanner(issued, onClose) {
  let raw = issued.token;
  const msg = h("div", { class: "a-gap" });
  const code = h("code", { class: "mono", text: raw });
  const copyBtn = btn(D.tokenCopy, async () => {
    try {
      await navigator.clipboard.writeText(raw);
      msg.replaceChildren(box("good", D.tokenCopied));
    } catch (e) {
      msg.replaceChildren(box("bad", D.curlCopyFail));
    }
  });
  const closeBtn = btn(D.tokenCloseBanner, () => {
    raw = "";
    code.textContent = "";
    onClose();
  });
  return h("section", { class: "a-card", "data-screen": "token-issued" },
    h("h3", { text: D.tokenIssuedTitle }),
    h("p", { class: "a-note", text: D.tokenIssuedWarn }),
    h("div", { class: "a-actions" }, code, copyBtn, closeBtn),
    msg);
}

function createForm(appendTables, alive, onIssued) {
  const out = h("div", { class: "d-out" });
  const nameId = uid("tok");
  const expId = uid("tok");
  const nameInput = h("input", { id: nameId, type: "text", class: "input", maxlength: 100, autocomplete: "off" });
  const expInput = h("input", { id: expId, type: "date", class: "input", value: defaultExpiryDate(), max: maxExpiryDate() });
  const selected = new Set();
  const tableChecks = appendTables.map((t) => {
    const cb = h("input", { type: "checkbox", value: t.name });
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(t.name); else selected.delete(t.name);
    });
    return h("label", { class: "check" }, cb, h("span", { text: t.name }));
  });
  const submit = h("button", { type: "submit", class: "btn primary", disabled: appendTables.length === 0 }, D.tokenIssueSubmit);
  const form = h("form", { class: "d-form", novalidate: true },
    h("div", { class: "d-fields" },
      h("div", { class: "field d-field" }, h("label", { for: nameId, text: D.tokenName }), nameInput, h("span", { class: "hint", text: D.tokenNameHint })),
      h("div", { class: "field d-field" }, h("label", { for: expId, text: D.tokenExpires }), expInput, h("span", { class: "hint", text: D.tokenExpiresHint }))),
    h("div", { class: "field d-field" },
      h("span", { text: D.tokenTables }),
      appendTables.length ? h("div", { class: "a-actions" }, ...tableChecks) : h("p", { class: "a-note", text: D.tokenNoAppendTables })),
    h("div", { class: "a-actions" }, submit),
    out);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    out.replaceChildren();
    submit.disabled = true;
    try {
      const res = await api("POST", `${BASE}/tokens`, {
        name: nameInput.value,
        tables: Array.from(selected),
        expires_at: expInput.value,
      });
      if (!alive()) return;
      nameInput.value = "";
      selected.clear();
      tableChecks.forEach((lbl) => { const cb = lbl.querySelector("input"); if (cb) cb.checked = false; });
      onIssued(res);
    } catch (err) {
      if (alive()) out.replaceChildren(box("bad", dataFailText(err)));
    } finally {
      submit.disabled = appendTables.length === 0;
    }
  });
  return h("section", { class: "a-card" }, h("h3", { text: D.tokenCreateTitle }), form);
}

function statusPill(status) {
  const tone = status === "active" ? "good" : status === "revoked" ? "bad" : "neutral";
  return pill(D.tokenStatusLabel[status] || status, tone);
}

async function renderTokens(my) {
  const alive = () => my === gen;
  const bannerBox = h("div");
  const noteBox = h("div");
  const listBox = h("div");
  const formBox = h("div");
  view().replaceChildren(
    head(D.tokensTitle, D.tokensIntro),
    bannerBox,
    noteBox,
    formBox,
    h("div", { class: "a-gap" }),
    listBox,
  );

  const loadList = async () => {
    try {
      const data = await api("GET", `${BASE}/tokens`);
      if (!alive()) return;
      const items = data.items || [];
      if (!items.length) {
        listBox.replaceChildren(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: D.tokenEmpty })));
        return;
      }
      const rows = items.map((item) => {
        const revokeBtn = item.status === "active"
          ? btn(D.tokenRevoke, async () => {
              if (!window.confirm(D.tokenRevokeConfirm(item.name))) return;
              revokeBtn.disabled = true;
              try {
                await api("DELETE", `${BASE}/tokens/${encodeURIComponent(item.id)}`);
                if (!alive()) return;
                noteBox.replaceChildren(box("good", D.tokenRevoked));
                await loadList();
              } catch (err) {
                if (alive()) noteBox.replaceChildren(box("bad", dataFailText(err)));
                revokeBtn.disabled = false;
              }
            }, "danger")
          : null;
        return h("tr", {},
          h("td", { text: item.name }),
          h("td", { class: "wrap", text: (item.tables || []).join(", ") }),
          h("td", {}, statusPill(item.status)),
          h("td", { text: fmtDay(item.expires_at) }),
          h("td", { text: item.last_used_at ? T.fmtDateTime(item.last_used_at) : "—" }),
          h("td", { text: item.created_by_email || "—" }),
          h("td", {}, revokeBtn || "—"));
      });
      listBox.replaceChildren(h("div", { class: "a-table-wrap" },
        h("table", { class: "a-table" },
          h("thead", {}, h("tr", {},
            h("th", { scope: "col", text: D.tokenColName }),
            h("th", { scope: "col", text: D.tokenColTables }),
            h("th", { scope: "col", text: D.tokenColStatus }),
            h("th", { scope: "col", text: D.tokenColExpires }),
            h("th", { scope: "col", text: D.tokenColLastUsed }),
            h("th", { scope: "col", text: D.tokenColCreator }),
            h("th", { scope: "col", text: D.tokenColActions }))),
          h("tbody", {}, ...rows))));
    } catch (err) {
      if (alive()) listBox.replaceChildren(message(dataFailText(err)));
    }
  };

  try {
    const tablesRes = await api("GET", `${BASE}/tables`);
    if (!alive()) return;
    const appendTables = (tablesRes.items || []).filter((t) => t.mode === "append");
    formBox.replaceChildren(createForm(appendTables, alive, (issued) => {
      bannerBox.replaceChildren(issuedBanner(issued, () => bannerBox.replaceChildren()));
      loadList();
    }));
  } catch (err) {
    if (alive()) formBox.replaceChildren(message(dataFailText(err)));
  }
  await loadList();
}

export default {
  key: "tokens",
  label: T.menu.tokens,
  match: /^tokens$/,
  when: () => session.manage !== null && isManageFeature("tokens") && isAdmin(),
  render() {
    gen += 1;
    renderTokens(gen);
  },
};
