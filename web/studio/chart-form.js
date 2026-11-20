// 차트 폼: registry의 `charts[].fields`에서 만든다(보강설계서 6-1: 차트 편집기를 새로 만들지 않는다).
// 필드의 kind가 컨트롤을 정한다(contracts/chart-shorthand.md의 kind 표):
//   column → 데이터셋 열 선택, columns → 여러 개(순서 있음), metric → 데이터셋 지표 또는 { sql, label },
//   metrics → 여러 개, time_grain·enum → 선택, bool → 체크, int → 숫자, string·d3format → 글자, order_by → [열 또는 지표, asc|desc] 목록.
// 유형 이름·필드 이름은 registry에서만 온다(이 파일에 차트 유형 이름은 없다).
// 원본 방식 차트(viz_type + params)는 제목·데이터셋과 params JSON 글상자만 둔다.
import S from "./strings.js";
import { h, uid, field, select, button, iconButton, clone, same } from "./dom.js";

export const KEY_PATTERN = /^[a-z][a-z0-9_]{0,62}$/;      // 계약 스키마의 $defs.key
export const TIME_GRAINS = ["P1D", "P1W", "P1M", "P3M", "P1Y"];   // 계약 스키마의 $defs.time_grain
const SQL_CHOICE = "::sql::";                             // 지표 선택 상자의 "직접 입력" 값(지표 이름과 겹치지 않는다)
const COMMON = ["title", "dataset", "viz", "viz_type", "where", "description", "params"];
const LIST_KINDS = ["columns", "metrics", "order_by"];

const blank = (v) => v === undefined || v === null || v === "";
// JSON 글상자에 친 글 가운데 아직 객체로 읽히지 않는 것(닫는 괄호가 빠진 초안 등): 초안 객체 → {필드: 원문}.
// 폼은 데이터셋을 바꾸거나 저장이 거절될 때마다 다시 그려지므로, 원문을 DOM이 아니라 초안 객체에 묶어 둔다.
// 초안 객체가 바뀌면(취소, 저장 성공, 다른 칸 선택) 함께 사라진다.
const brokenJson = new WeakMap();
const brokenOf = (pending) => { if (!brokenJson.has(pending)) brokenJson.set(pending, {}); return brokenJson.get(pending); };
const isSql = (v) => !!v && typeof v === "object";

/** 유형을 바꿨을 때: 새 유형에 없는 필드를 초안에서 뺀다(저장할 때 null로 나간다) */
export function retype(pending, type) {
  const keep = new Set([...COMMON, ...(type ? type.fields.map((f) => f.name) : [])]);
  for (const k of Object.keys(pending)) if (!keep.has(k)) delete pending[k];
}

/** 보낼 값: 빈 글자·없는 값은 뺀다. 목록은 그대로 둔다(빈 목록을 지울지는 폼이 정한다). */
export function cleanChart(pending) {
  const out = {};
  for (const [k, v] of Object.entries(pending)) if (!blank(v)) out[k] = clone(v);
  return out;
}

/** 얕은 병합용 차이: 바뀐 키는 새 값, 없어진 키는 null */
export function diffValues(original, value) {
  const values = {};
  for (const k of new Set([...Object.keys(original), ...Object.keys(value)])) {
    if (!(k in value)) values[k] = null;
    else if (!same(value[k], original[k])) values[k] = value[k];
  }
  return values;
}

/** 새 차트의 초안: 필수 필드의 자리만 만든다 */
export function newChart(type, dataset) {
  const out = { title: "", dataset: dataset || "" };
  if (!type) { out.viz_type = ""; out.params = {}; return out; }
  out.viz = type.viz;
  for (const f of type.fields) if (f.required && LIST_KINDS.includes(f.kind)) out[f.name] = [];
  return out;
}

/** 위·아래·삭제 버튼이 달린 순서 있는 목록 한 줄 */
function listRow(name, i, n, body, { move, remove }) {
  return h("li", { class: "arr-item is-scalar" },
    h("span", { class: "arr-no num", text: String(i + 1), "aria-hidden": "true" }),
    h("div", { class: "arr-body" }, body),
    h("div", { class: "arr-actions" },
      iconButton("up", S.itemUp(name, i + 1), { "data-path": `${name}.${i}.__up`, disabled: i === 0, onclick: () => move(i - 1) }),
      iconButton("down", S.itemDown(name, i + 1), { disabled: i === n - 1, onclick: () => move(i + 1) }),
      iconButton("trash", S.itemRemove(name, i + 1), { onclick: remove })));
}

/**
 * 폼을 box 안에 그린다. pending은 그 자리에서 고친다(차트에서 key를 뺀 객체).
 * @param {HTMLElement} box
 * @param {{pending: object, isNew: boolean, keyBox?: {value: string}, types: object[], datasets: string[],
 *   columns: (dataset: string) => ({name: string, type?: string}[]|null), metrics: (dataset: string) => string[],
 *   onChange: () => void}} opts
 */
export function renderChartForm(box, { pending, isNew, keyBox, types, datasets, columns, metrics, onChange }) {
  const raw = !("viz" in pending);
  const typeOf = () => types.find((t) => t.viz === pending.viz) || null;
  // 이 차트에 처음부터 있던 필드. bool을 끄면 처음부터 있던 것은 false로, 없던 것은 지운다
  const had = new Set(Object.keys(pending));

  function rerender(focusPath) {
    const active = focusPath || (document.activeElement && box.contains(document.activeElement) ? document.activeElement.getAttribute("data-path") : null);
    box.replaceChildren(...build());
    if (active) {
      const el = box.querySelector(`[data-path="${CSS.escape(active)}"]`);
      if (el) el.focus();
    }
  }
  const changed = (structural, focusPath) => { onChange(); if (structural) rerender(focusPath); };
  const set = (name, v) => { if (blank(v)) delete pending[name]; else pending[name] = v; };

  function text(name, { mono = false, area = false, keep = false } = {}) {
    const el = area ? h("textarea", { class: `input${mono ? " mono" : ""}`, rows: 2, "data-path": name, spellcheck: "false" })
      : h("input", { type: "text", class: `input${mono ? " mono" : ""}`, autocomplete: "off", "data-path": name });
    el.value = pending[name] ?? "";
    el.addEventListener("input", () => { if (keep) pending[name] = el.value; else set(name, el.value); changed(false); });
    return el;
  }

  function columnSelect(value, path, { required, onPick }) {
    const cols = columns(pending.dataset);
    if (!cols || !cols.length) {
      const el = h("input", { type: "text", class: "input mono", autocomplete: "off", "data-path": path, value: value ?? "" });
      el.addEventListener("input", () => onPick(el.value));
      return { el, hint: S.noColumnList };
    }
    const opts = cols.map((c) => ({ value: c.name, label: c.type ? `${c.name} (${c.type})` : c.name }));
    if (!blank(value) && !cols.some((c) => c.name === value)) opts.push({ value, label: S.notInList(value) });
    return { el: select(opts, value, { "data-path": path, placeholder: required ? S.chooseColumn : S.none, onchange: onPick }) };
  }

  /** 지표 하나: 데이터셋 지표 선택 또는 직접 입력({ sql, label }) */
  function metricControl(value, path, { required, onPick }) {
    const names = metrics(pending.dataset);
    const opts = names.map((m) => ({ value: m, label: m }));
    if (typeof value === "string" && value && !names.includes(value)) opts.push({ value, label: S.notInList(value) });
    opts.push({ value: SQL_CHOICE, label: S.metricCustom });
    const pick = select(opts, isSql(value) ? SQL_CHOICE : value, { "data-path": path, placeholder: required ? S.chooseMetric : S.none,
      onchange: (v) => { onPick(v === SQL_CHOICE ? { sql: "", label: "" } : v || undefined, true); } });
    if (!isSql(value)) return h("div", { class: "metric" }, pick);
    const sql = h("input", { type: "text", class: "input mono", autocomplete: "off", value: value.sql ?? "", placeholder: S.metricSql, "aria-label": S.metricSql, "data-path": `${path}.sql` });
    const label = h("input", { type: "text", class: "input", autocomplete: "off", value: value.label ?? "", placeholder: S.metricLabel, "aria-label": S.metricLabel, "data-path": `${path}.label` });
    sql.addEventListener("input", () => { value.sql = sql.value; onPick(value, false); });
    label.addEventListener("input", () => { value.label = label.value; onPick(value, false); });
    return h("div", { class: "metric" }, pick, h("div", { class: "metric-sql" }, sql, label));
  }

  /** 순서 있는 목록 필드의 틀: 항목 줄 + 추가 컨트롤 */
  function listField(f, rows, adder) {
    const set_ = h("fieldset", { class: "group", "data-field": f.name },
      h("legend", {}, h("span", { class: "legend-name mono", text: f.name }), f.required ? h("span", { class: "req", text: ` ${S.required}` }) : null));
    const hint = hintOf(f);
    if (hint) set_.append(h("p", { class: "hint", text: hint }));
    if (rows.length) set_.append(h("ol", { class: "arr" }, rows)); else set_.append(h("p", { class: "hint", text: S.arrayEmpty }));
    set_.append(adder);
    return set_;
  }
  const listOf = (name) => (Array.isArray(pending[name]) ? pending[name] : []);
  function listOps(f) {
    const name = f.name;
    return {
      move: (i, to) => { const a = listOf(name); const [x] = a.splice(i, 1); a.splice(to, 0, x); changed(true, `${name}.${to}.__up`); },
      remove: (i) => { const a = listOf(name); a.splice(i, 1); if (!a.length && !f.required) delete pending[name]; changed(true, `${name}.__add`); },
      add: (v) => { if (!Array.isArray(pending[name])) pending[name] = []; pending[name].push(v); changed(true, `${name}.__add`); },
    };
  }

  function hintOf(f) {
    return [f.description, "default" in f ? S.fieldDefault(JSON.stringify(f.default)) : null,
      f.min != null || f.max != null ? S.range(f.min, f.max) : null,
      f.min_items ? S.fieldMinItems(f.min_items) : null].filter(Boolean).join(" · ");
  }

  function fieldControl(f) {
    const name = f.name;
    const meta = { hint: hintOf(f), required: !!f.required, requiredText: S.required };
    const label = h("span", { class: "mono", text: name });
    const wrap = (el, extra) => { const row = field(label, el, { ...meta, ...(extra || {}) }); row.setAttribute("data-field", name); return row; };

    if (f.kind === "column") {
      const c = columnSelect(pending[name], name, { required: f.required, onPick: (v) => { set(name, v); changed(false); } });
      return wrap(c.el, c.hint ? { hint: [meta.hint, c.hint].filter(Boolean).join(" · ") } : null);
    }
    if (f.kind === "columns") {
      const ops = listOps(f);
      const arr = listOf(name);
      const rows = arr.map((v, i) => listRow(name, i, arr.length, h("span", { class: "mono arr-text", text: v }), { move: (to) => ops.move(i, to), remove: () => ops.remove(i) }));
      const cols = columns(pending.dataset);
      let adder;
      if (cols && cols.length) {
        const rest = cols.filter((c) => !arr.includes(c.name)).map((c) => ({ value: c.name, label: c.type ? `${c.name} (${c.type})` : c.name }));
        adder = select(rest, "", { "data-path": `${name}.__add`, placeholder: S.addColumn, "aria-label": S.addTo(name), disabled: !rest.length, onchange: (v) => { if (v) ops.add(v); } });
      } else {
        const input = h("input", { type: "text", class: "input mono", autocomplete: "off", "data-path": `${name}.__add`, placeholder: S.noColumnList, "aria-label": S.addTo(name) });
        const add = () => { const v = input.value.trim(); if (v && !arr.includes(v)) ops.add(v); };
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
        adder = h("div", { class: "add-row" }, input, button(S.itemAdd, { iconName: "plus", cls: "small", onclick: add }));
      }
      return listField(f, rows, adder);
    }
    if (f.kind === "metric") {
      const el = metricControl(pending[name], name, { required: f.required, onPick: (v, structural) => { set(name, v); changed(structural, name); } });
      const row = h("div", { class: "field", "data-field": name }, h("span", { class: "field-label" }, label, f.required ? h("span", { class: "req", text: ` ${S.required}` }) : null), el,
        meta.hint ? h("span", { class: "hint", text: meta.hint }) : null);
      el.querySelector("select").setAttribute("aria-label", name);
      return row;
    }
    if (f.kind === "metrics") {
      const ops = listOps(f);
      const arr = listOf(name);
      const rows = arr.map((v, i) => listRow(name, i, arr.length,
        metricControl(v, `${name}.${i}`, { required: true, onPick: (nv, structural) => { arr[i] = nv === undefined ? "" : nv; changed(structural, `${name}.${i}`); } }),
        { move: (to) => ops.move(i, to), remove: () => ops.remove(i) }));
      rows.forEach((row, i) => row.querySelector("select").setAttribute("aria-label", S.itemLabel(i + 1)));
      const names = metrics(pending.dataset).filter((m) => !arr.includes(m));
      const adder = select([...names.map((m) => ({ value: m, label: m })), { value: SQL_CHOICE, label: S.metricCustom }], "",
        { "data-path": `${name}.__add`, placeholder: S.addMetric, "aria-label": S.addTo(name), onchange: (v) => { if (v) ops.add(v === SQL_CHOICE ? { sql: "", label: "" } : v); } });
      return listField(f, rows, adder);
    }
    if (f.kind === "order_by") {
      const ops = listOps(f);
      const arr = listOf(name);
      // 정렬 기준: 뷰의 열 또는 이 차트가 쓰는 지표의 이름(라벨)
      const used = [];
      for (const t of typeOf() ? typeOf().fields : []) {
        if (t.kind === "metric" && !blank(pending[t.name])) used.push(pending[t.name]);
        if (t.kind === "metrics") used.push(...listOf(t.name));
      }
      const metricNames = used.map((m) => (isSql(m) ? m.label : m)).filter((m) => !blank(m));
      const cols = columns(pending.dataset) || [];
      const choices = [...new Set([...cols.map((c) => c.name), ...metricNames])];
      const rows = arr.map((pair, i) => {
        const cur = Array.isArray(pair) ? pair : [];
        const opts = choices.slice();
        // 방향은 렌더링 때의 값(cur)이 아니라 지금 초안(arr[i])에서 읽는다. 방향을 바꾼 뒤 열을 바꿔도 방향이 유지된다
        const dirNow = () => (Array.isArray(arr[i]) && arr[i][1]) || "asc";
        const byNow = () => (Array.isArray(arr[i]) ? arr[i][0] : "") ?? "";
        if (!blank(cur[0]) && !opts.includes(cur[0])) opts.push({ value: cur[0], label: S.notInList(cur[0]) });
        const by = choices.length
          ? select(opts, cur[0], { "data-path": `${name}.${i}`, placeholder: S.choose, "aria-label": S.orderByTarget(i + 1), onchange: (v) => { arr[i] = [v, dirNow()]; changed(true, `${name}.${i}`); } })
          : h("input", { type: "text", class: "input mono", autocomplete: "off", value: cur[0] ?? "", "data-path": `${name}.${i}`, "aria-label": S.orderByTarget(i + 1) });
        if (!choices.length) by.addEventListener("input", () => { arr[i] = [by.value, dirNow()]; changed(false); });
        const dir = select([{ value: "asc", label: S.orderAsc }, { value: "desc", label: S.orderDesc }], cur[1] || "asc",
          { "data-path": `${name}.${i}.dir`, "aria-label": S.orderByDir(i + 1), onchange: (v) => { arr[i] = [byNow(), v]; changed(false); } });
        return listRow(name, i, arr.length, h("div", { class: "pair" }, by, dir), { move: (to) => ops.move(i, to), remove: () => ops.remove(i) });
      });
      const adder = button(S.itemAdd, { iconName: "plus", cls: "small", "data-path": `${name}.__add`, onclick: () => ops.add(["", "asc"]) });
      return listField(f, rows, adder);
    }
    if (f.kind === "time_grain" || f.kind === "enum") {
      const values = f.kind === "time_grain" ? TIME_GRAINS : Array.isArray(f.values) ? f.values : [];
      const opts = values.map((v) => ({ value: String(v), label: f.kind === "time_grain" ? S.grainLabel(v) : String(v) }));
      const cur = pending[name];
      if (!blank(cur) && !values.some((v) => String(v) === String(cur))) opts.push({ value: String(cur), label: S.notInList(cur) });
      return wrap(select(opts, cur, { "data-path": name, placeholder: f.required ? S.choose : S.none, onchange: (v) => {
        const hit = values.find((x) => String(x) === v);
        set(name, hit === undefined ? v || undefined : hit); changed(false);
      } }));
    }
    if (f.kind === "bool") {
      const box_ = h("input", { type: "checkbox", id: uid("opt"), "data-path": name, checked: pending[name] === true });
      box_.addEventListener("change", () => {
        if (box_.checked) pending[name] = true; else if (had.has(name)) pending[name] = false; else delete pending[name];
        changed(false);
      });
      return h("div", { class: "field check", "data-field": name }, box_, h("label", { for: box_.id }, h("span", { class: "mono", text: name })),
        meta.hint ? h("span", { class: "hint", text: meta.hint }) : null);
    }
    if (f.kind === "int") {
      const el = h("input", { type: "number", class: "input", "data-path": name, min: f.min, max: f.max, step: 1, value: pending[name] ?? "" });
      el.addEventListener("input", () => { set(name, el.value === "" || Number.isNaN(Number(el.value)) ? undefined : Number(el.value)); changed(false); });
      return wrap(el);
    }
    // string, d3format, 그 밖(이 Studio가 모르는 kind)은 글자로 받는다
    const hint = f.kind === "d3format" ? [meta.hint, S.formatHint].filter(Boolean).join(" · ") : meta.hint;
    return wrap(text(name, { mono: f.kind === "d3format" }), { hint });
  }

  function jsonArea(name, required) {
    const el = h("textarea", { class: "input mono", rows: raw ? 10 : 3, "data-path": name, spellcheck: "false" });
    const broken = brokenOf(pending);
    if (typeof broken[name] === "string") { el.value = broken[name]; el.setAttribute("aria-invalid", "true"); }   // 읽히지 않던 초안을 그대로 되살린다
    else el.value = pending[name] === undefined ? "" : JSON.stringify(pending[name], null, 2);
    el.addEventListener("input", () => {
      const src = el.value.trim();
      if (!src) { delete broken[name]; el.removeAttribute("aria-invalid"); if (required) pending[name] = {}; else delete pending[name]; changed(false); return; }
      try {
        const v = JSON.parse(src);
        if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("object");
        pending[name] = v; delete broken[name]; el.removeAttribute("aria-invalid");
      } catch (e) { broken[name] = el.value; el.setAttribute("aria-invalid", "true"); }
      changed(false);
    });
    return el;
  }

  function build() {
    const out = [];
    if (isNew && keyBox) {
      const key = h("input", { type: "text", class: "input mono", autocomplete: "off", value: keyBox.value, "data-path": "key" });
      key.addEventListener("input", () => { keyBox.value = key.value; });
      out.push(field(S.chartKey, key, { hint: S.chartKeyHint, required: true, requiredText: S.required }));
    }
    out.push(field(S.chartTitle, text("title", { keep: true }), { required: true, requiredText: S.required }));

    if (raw) {
      const vt = h("input", { type: "text", class: "input mono", autocomplete: "off", value: pending.viz_type ?? "", "data-path": "viz_type", disabled: !isNew });
      vt.addEventListener("input", () => { pending.viz_type = vt.value; changed(false); });
      out.push(field(S.chartRawType, vt, { hint: isNew ? S.chartRawTypeHint : S.chartRawTypeFixed, required: isNew, requiredText: S.required }));
    } else {
      const opts = types.map((t) => ({ value: t.viz, label: t.label ? `${t.label} · ${t.viz}` : t.viz }));
      if (!typeOf()) opts.push({ value: pending.viz, label: S.notInList(pending.viz) });
      out.push(field(S.chartType, select(opts, pending.viz, { "data-path": "viz", onchange: (v) => {
        pending.viz = v; retype(pending, typeOf());
        for (const f of typeOf() ? typeOf().fields : []) if (f.required && LIST_KINDS.includes(f.kind) && !Array.isArray(pending[f.name])) pending[f.name] = [];
        changed(true, "viz");
      } }), { hint: isNew ? null : S.chartTypeHint }));
    }

    const ds = datasets.slice();
    if (!blank(pending.dataset) && !ds.includes(pending.dataset)) ds.push({ value: pending.dataset, label: S.notInList(pending.dataset) });
    out.push(field(S.chartDataset, select(ds, pending.dataset, { "data-path": "dataset", placeholder: S.chooseDataset, onchange: (v) => { pending.dataset = v; changed(true, "dataset"); } }),
      { required: true, requiredText: S.required }));

    if (raw) {
      out.push(field(S.chartParams, jsonArea("params", true), { hint: S.chartParamsRawHint, required: true, requiredText: S.required }));
    } else {
      const type = typeOf();
      if (type) out.push(h("div", { class: "schema-form chart-fields" }, type.fields.map(fieldControl)));
      else out.push(h("p", { class: "hint", text: S.chartUnknownType }));
    }
    out.push(field(S.chartWhere, text("where", { mono: true, area: true }), { hint: S.chartWhereHint }));
    out.push(field(S.chartDescription, text("description")));
    if (!raw) out.push(field(S.chartParams, jsonArea("params", false), { hint: S.chartParamsHint }));
    return out;
  }

  rerender();
  return { rerender, invalidJson: () => !!box.querySelector('textarea[aria-invalid="true"]') };
}
