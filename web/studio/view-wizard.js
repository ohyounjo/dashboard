// 프로젝트 마법사: CSV 여러 개 → 추론 결과 표(고칠 수 있음) → 프로젝트 생성.
// 추론 결과의 형식은 contracts/scaffold.md. 표에서 고친 값은 inference.tables에 그대로 적어 서버로 돌려보낸다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { h, fill, field, select, button, badge, alertBox, skeleton, fileDrop, filePicker, clone } from "./dom.js";

const TYPES = ["text", "integer", "number", "boolean", "date", "timestamp", "enum"];   // contracts/scaffold.md의 유형
const IDENT = /^[a-z][a-z0-9_]{0,62}$/;
const PROJECT_NAME = /^[a-z0-9][a-z0-9_-]*$/;
const CONFIDENCE = { high: ["good", "check"], medium: ["neutral", "info"], low: ["warn", "warning"] };

export function mount(container, ctx) {
  let step = "drop";       // drop | inferring | editing | creating
  let inference = null, uploadId = null, serverProblems = [];
  let edited = false;       // 표를 고쳤는지(→ resuggest)
  let touched = new Set();  // 고친 칸("테이블 위치:열 위치")
  let name = "";
  let error = null;         // {title, body, errors}
  let skipped = [];         // CSV가 아니라서 뺀 파일 이름

  /* ---------------- 올리기 ---------------- */

  async function upload(files) {
    const csv = files.filter((f) => /\.csv$/i.test(f.name));
    skipped = files.filter((f) => !csv.includes(f)).map((f) => f.name);
    if (!csv.length) { error = { title: S.wizardNoCsvTitle, body: S.wizardNoCsvBody, errors: skipped }; render(); return; }
    step = "inferring"; error = null; render();
    try {
      const res = await ctx.api.infer(csv);
      inference = clone(res.inference); uploadId = res.upload_id;
      serverProblems = Array.isArray(inference.problems) ? inference.problems.slice() : [];
      edited = false; touched = new Set(); step = "editing";
      ctx.announce(S.wizardInferred((inference.tables || []).length));
    } catch (e) {
      step = "drop"; error = ctx.describeError(e); ctx.announce(error.title);
    }
    render();
  }

  /* ---------------- 문제(생성 버튼을 막는 이유) ---------------- */

  function problems() {
    const out = [];
    const tables = inference.tables || [];
    // 서버가 알려 준 문제: "<원본 파일>: …" 꼴이다. 그 테이블에 기본 키가 생기면 해결된 것으로 본다(최종 판정은 생성할 때 서버가 한다).
    for (const p of serverProblems) {
      const t = tables.find((x) => p.startsWith(`${x.source}:`));
      if (!t || !(t.primary_key || []).length) out.push(p);
    }
    const seenTables = new Set();
    for (const t of tables) {
      if (!(t.primary_key || []).length && !out.some((p) => p.startsWith(`${t.source}:`))) out.push(S.problemNoKey(t.source));
      if (!IDENT.test(t.name || "")) out.push(S.problemTableName(t.source));
      else if (seenTables.has(t.name)) out.push(S.problemDuplicate(t.source, t.name));
      seenTables.add(t.name);
      const seen = new Set();
      for (const c of t.columns || []) {
        if (!IDENT.test(c.name || "")) out.push(S.problemColumnName(t.source, c.source));
        else if (seen.has(c.name)) out.push(S.problemDuplicate(t.source, c.name));
        seen.add(c.name);
        if (c.type === "enum" && !(c.values || []).length) out.push(S.problemEnumEmpty(t.source, c.name || c.source));
      }
    }
    return out;
  }

  /* ---------------- 고치기 ---------------- */

  function edit(mark, structural = true) {
    edited = true; touched.add(mark);
    if (structural) render(); else paintGate();
  }

  function renameTable(t, next) {
    const prev = t.name;
    t.name = next;
    for (const other of inference.tables) for (const c of other.columns) {
      if (typeof c.ref === "string" && c.ref.startsWith(`${prev}.`)) c.ref = `${next}.${c.ref.slice(prev.length + 1)}`;
    }
  }

  function renameColumn(t, c, next) {
    const prev = c.name;
    c.name = next;
    t.primary_key = (t.primary_key || []).map((k) => (k === prev ? next : k));
    for (const other of inference.tables) for (const oc of other.columns) if (oc.ref === `${t.name}.${prev}`) oc.ref = `${t.name}.${next}`;
  }

  /** ref 후보: 다른 테이블의 단일 열 기본 키 */
  function refTargets(t) {
    return inference.tables.filter((o) => o !== t && (o.primary_key || []).length === 1).map((o) => `${o.name}.${o.primary_key[0]}`);
  }

  async function create() {
    if (step !== "editing" || problems().length || !PROJECT_NAME.test(name)) return;
    step = "creating"; error = null; render();
    try {
      const body = { name, upload_id: uploadId, inference };
      if (edited) body.resuggest = true;
      const res = await ctx.api.createProject(body);
      ctx.announce(S.wizardCreated(res.name || name));
      ctx.projectCreated(res.name || name);
    } catch (e) {
      step = "editing"; error = ctx.describeError(e); ctx.announce(error.title);
      render();
    }
  }

  /* ---------------- 그리기 ---------------- */

  function confidence(level) {
    const [tone, ic] = CONFIDENCE[level] || CONFIDENCE.medium;
    return badge(S.confidence(level), tone, ic);
  }

  function dropView() {
    const zone = h("div", { class: "dropzone big" },
      h("div", { class: "state-ico", "data-tone": "primary" }, icon("upload")),
      h("h3", { text: S.wizardDropTitle }),
      h("p", { class: "muted", text: S.wizardDropBody }),
      filePicker(S.wizardPick, { multiple: true, cls: "primary", onFiles: upload }));
    return [error ? alertBox({ title: error.title, body: error.body, errors: error.errors }) : null, zone];
  }

  function columnRow(t, ti, c, ci) {
    const mark = `${ti}:${ci}`;
    const low = c.confidence === "low" || c.ref_confidence === "low";
    const lbl = (text) => `${text}: ${c.source}`;

    const nameInput = h("input", { type: "text", class: "input mono", autocomplete: "off", value: c.name, "aria-label": lbl(S.colName) });
    nameInput.addEventListener("change", () => { renameColumn(t, c, nameInput.value.trim()); edit(mark); });

    const typeSel = select(TYPES, c.type, { "aria-label": lbl(S.colType), "data-col": "type", onchange: (v) => {
      c.type = v;
      if (v === "enum") { if (!Array.isArray(c.values)) c.values = []; } else delete c.values;
      edit(mark);
    } });

    const req = h("input", { type: "checkbox", checked: !!c.required, "aria-label": lbl(S.colRequired) });
    req.addEventListener("change", () => { c.required = req.checked; edit(mark, false); });

    let values = h("span", { class: "muted", text: S.dash });
    if (c.type === "enum") {
      values = h("input", { type: "text", class: "input", autocomplete: "off", value: (c.values || []).join(", "), "aria-label": lbl(S.colValues), placeholder: S.colValuesHint });
      values.addEventListener("change", () => { c.values = values.value.split(",").map((v) => v.trim()).filter(Boolean); edit(mark); });
    }

    const pk = h("input", { type: "checkbox", checked: (t.primary_key || []).includes(c.name), "aria-label": lbl(S.colKey), "data-col": "pk" });
    pk.addEventListener("change", () => {
      const keys = new Set(t.primary_key || []);
      if (pk.checked) keys.add(c.name); else keys.delete(c.name);
      t.primary_key = t.columns.map((x) => x.name).filter((n) => keys.has(n));
      edit(`${ti}:pk`);
    });

    const targets = refTargets(t);
    if (c.ref && !targets.includes(c.ref)) targets.push(c.ref);
    const ref = select(targets, c.ref || "", { "aria-label": lbl(S.colRef), placeholder: S.none, onchange: (v) => {
      if (v) c.ref = v; else delete c.ref;
      delete c.ref_basis; delete c.ref_confidence;
      edit(mark);
    } });

    const basis = h("div", { class: "basis" },
      h("div", { class: "basis-line" }, confidence(c.confidence), touched.has(mark) ? badge(S.editedMark, "primary") : null),
      c.basis ? h("span", { text: c.basis }) : null,
      c.ref && c.ref_basis ? h("span", {}, `ref: ${c.ref_basis} `, c.ref_confidence ? confidence(c.ref_confidence) : null) : null,
      (c.notes || []).map((n) => h("span", { class: "note", text: n })));

    return h("tr", { class: low ? "is-low" : "" },
      h("td", {}, nameInput), h("td", { class: "src", text: c.source }), h("td", {}, typeSel), h("td", { class: "center" }, req),
      h("td", {}, values), h("td", { class: "center" }, pk), h("td", {}, ref), h("td", {}, basis));
  }

  function tablePanel(t, ti) {
    const nameInput = h("input", { type: "text", class: "input mono", autocomplete: "off", value: t.name });
    nameInput.addEventListener("change", () => { renameTable(t, nameInput.value.trim()); edit(`${ti}:name`); });
    const keys = t.primary_key || [];
    const keyLine = h("p", { class: `key-line${keys.length ? "" : " is-missing"}` },
      h("b", { text: S.primaryKey }), " ",
      keys.length ? h("span", { class: "mono", text: keys.join(", ") }) : badge(S.primaryKeyMissing, "bad", "warning"), " ",
      touched.has(`${ti}:pk`) ? badge(S.editedMark, "primary") : [t.primary_key_confidence ? confidence(t.primary_key_confidence) : null, " ", t.primary_key_basis ? h("span", { class: "muted", text: t.primary_key_basis }) : null]);
    const head = [S.colName, S.colSource, S.colType, S.colRequired, S.colValues, S.colKey, S.colRef, S.colBasis];
    return h("section", { class: "panel wiz-table", "data-table": t.name },
      h("div", { class: "wiz-table-head" },
        field(S.tableName, nameInput, { cls: "compact" }),
        h("p", { class: "muted" }, h("span", { class: "mono", text: t.source }), ` · ${S.rowCount(t.rows)}`)),
      keyLine,
      (t.notes || []).length ? h("ul", { class: "notes" }, t.notes.map((n) => h("li", { text: n }))) : null,
      h("div", { class: "table-wrap", tabindex: "0", role: "group", "aria-label": S.columnsOf(t.name) },
        h("table", { class: "grid" },
          h("thead", {}, h("tr", {}, head.map((x) => h("th", { scope: "col", text: x })))),
          h("tbody", {}, (t.columns || []).map((c, ci) => columnRow(t, ti, c, ci))))));
  }

  let gate = null;   // 문제 목록 + 이름 + 생성 버튼(표를 다시 그리지 않고 이 부분만 갱신)

  function paintGate() {
    if (!gate) return;
    const list = problems();
    const nameOk = PROJECT_NAME.test(name);
    const creating = step === "creating";
    const input = h("input", { type: "text", class: "input mono", autocomplete: "off", value: name, placeholder: S.projectNameHint, disabled: creating, "data-wizard": "name" });
    const createBtn = button(creating ? S.creating : S.createProject, { iconName: "check", cls: "primary", "data-action": "create", disabled: creating || !!list.length || !nameOk, onclick: create });
    const why = h("p", { class: "hint", role: "status" });
    const paintWhy = () => {
      const ok = PROJECT_NAME.test(name);
      createBtn.disabled = step === "creating" || !!problems().length || !ok;
      why.textContent = problems().length ? S.createBlockedProblems : !name ? S.createBlockedName : !ok ? S.projectNameInvalid : "";
    };
    input.addEventListener("input", () => { name = input.value.trim(); paintWhy(); });
    paintWhy();
    const d = inference.dashboard || {};
    const charts = Object.values(d.dashboards || {}).reduce((n, x) => n + ((x && x.charts) || []).length, 0);
    const hadFocus = gate.contains(document.activeElement) && document.activeElement.getAttribute("data-wizard") === "name";
    fill(gate,
      list.length ? alertBox({ tone: "bad", title: S.problemsTitle(list.length), body: S.problemsBody, errors: list }) : null,
      error ? alertBox({ title: error.title, body: error.body, errors: error.errors }) : null,
      h("div", { class: "panel wiz-create" },
        field(S.projectName, input, { hint: S.projectNameRule, required: true, requiredText: S.required }),
        h("div", { class: "form-actions" }, createBtn,
          button(S.wizardRestart, { disabled: creating, onclick: () => { step = "drop"; inference = null; error = null; render(); } })),
        why,
        h("p", { class: "hint", text: edited ? S.draftResuggest : S.draftCounts(Object.keys(d.dashboards || {}).length, charts, (d.tabs || []).length) })));
    if (hadFocus) input.focus();
  }

  function render() {
    const head = h("div", { class: "view-head" }, h("h2", { text: S.wizardTitle }), h("p", { class: "intro", text: S.wizardLead }));
    gate = null;
    if (step === "drop") fill(container, head, dropView());
    else if (step === "inferring") fill(container, head, skeleton(S.wizardInferring));
    else {
      gate = h("div", { class: "wiz-gate" });
      fill(container, head,
        skipped.length ? alertBox({ tone: "warn", title: S.wizardSkipped, errors: skipped, role: "status" }) : null,
        gate,
        (inference.tables || []).map(tablePanel));
      paintGate();
    }
  }

  // 화면 어디에 놓아도 받는다(추론 전 단계에서만)
  fileDrop(container, (files) => { if (step === "drop") upload(files); });
  render();
  return { canLeave: () => step !== "editing" || !edited || window.confirm(S.discardConfirm) };
}
