// JSON Schema(위젯 매니페스트의 options)에서 폼을 만든다.
// 지원: 문자열·숫자·불리언·enum·배열·중첩 객체·맵(additionalProperties), `x-column`(데이터셋 뷰의 열 선택),
// `dataset`(데이터셋 선택), icon·tone(registry 목록). 그 밖의 모양(oneOf 등)은 JSON 입력 상자로 받는다.
// 유형·옵션 이름은 스키마에서만 온다(이 파일에 위젯 유형 이름은 없다).
import S from "./strings.js";
import { h, uid, field, select, iconButton, button } from "./dom.js";

// 계약 스키마(contracts/dashboard.schema.json)의 공용 정의. registry가 `defs`를 주면 그쪽이 우선이다(api.js A9).
const BUILTIN_DEFS = {
  key: { type: "string" },
  column: { type: "string" },
  date: { type: "string" },
  color: { type: "string" },
  tone: {},
  icon: {},
  status_map: {
    type: "object",
    additionalProperties: {
      type: "object", required: ["label"],
      properties: {
        label: { type: "string", minLength: 1 },
        icon: { type: "string", minLength: 1, maxLength: 8 },
        tone: { $ref: "#/$defs/tone" },
      },
    },
  },
};
const REF_KINDS = { tone: "tone", icon: "icon", date: "date" };

/** `$ref`를 풀어 한 겹으로 합친다(노드에 직접 적은 키가 우선). `x-kind`에 정의 이름에서 온 종류를 적는다. */
export function resolve(schema, defs) {
  if (!schema || typeof schema !== "object") return {};
  let out = schema;
  for (let i = 0; i < 8 && typeof out.$ref === "string"; i++) {
    const name = out.$ref.split("/").pop();
    const target = (defs && defs[name]) || BUILTIN_DEFS[name] || {};
    const { $ref, ...own } = out;
    out = { ...target, ...own };
    if (REF_KINDS[name] && !out["x-kind"]) out["x-kind"] = REF_KINDS[name];
  }
  return out;
}

const isMap = (s) => s.type === "object" && !s.properties && s.additionalProperties && typeof s.additionalProperties === "object";
const isObject = (s) => s.type === "object" && !!s.properties;
const isDataset = (name, depth) => name === "dataset" && depth === 0;

function kindOf(s, name, depth) {
  if (s["x-column"]) return "column";
  if (isDataset(name, depth)) return "dataset";
  if (s["x-kind"] === "tone" || s["x-kind"] === "icon") return s["x-kind"];
  if (Array.isArray(s.enum)) return "enum";
  if (s.type === "boolean") return "boolean";
  if (s.type === "integer" || s.type === "number") return "number";
  if (s.type === "string") return s["x-kind"] === "date" ? "date" : "string";
  if (s.type === "array" && s.items) return "array";
  if (isObject(s)) return "object";
  if (isMap(s)) return "map";
  return "json";
}

/** 필수 항목만 채운 빈 값 */
export function defaultsFor(schema, ctx, name = "", depth = 0) {
  const s = resolve(schema, ctx.defs);
  switch (kindOf(s, name, depth)) {
    case "dataset": return ctx.datasets[0] || "";
    case "enum": return s.enum[0];
    case "tone": return (ctx.tones || [])[0] || "";
    case "icon": return (ctx.icons || [])[0] || "";
    case "boolean": return s.default === true;
    case "number": return typeof s.default === "number" ? s.default : undefined;
    case "array": return s.minItems > 0 ? [defaultsFor(s.items, ctx, "", depth + 1)] : [];
    case "object": {
      const out = {};
      for (const key of s.required || []) if (s.properties[key]) out[key] = defaultsFor(s.properties[key], ctx, key, depth + 1);
      return out;
    }
    case "map": return {};
    case "json": return undefined;
    default: return "";
  }
}

const blank = (v) => v === undefined || v === null || v === "";
const empty = (v) => blank(v) || (Array.isArray(v) && !v.length) || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length);

/** 보낼 값으로 다듬는다: 빈 선택 옵션은 뺀다(필수 옵션은 비어 있어도 남겨 서버가 이유를 말하게 한다). */
export function clean(value, schema, ctx) {
  const s = resolve(schema, ctx.defs);
  if (Array.isArray(value)) return value.map((v) => clean(v, s.items || {}, ctx)).filter((v) => !blank(v));
  if (value && typeof value === "object") {
    const out = {};
    const required = new Set(s.required || []);
    for (const [k, v] of Object.entries(value)) {
      const sub = (s.properties && s.properties[k]) || (isMap(s) ? s.additionalProperties : {});
      const c = clean(v, sub, ctx);
      if (isMap(s)) out[k] = c;                                           // 맵의 항목은 그대로
      else if (required.has(k)) { if (c !== undefined && c !== null) out[k] = c; }   // 필수: 빈 문자열도 남긴다
      else if (!empty(c)) out[k] = c;                                     // 선택: 비어 있으면 뺀다
    }
    return out;
  }
  return value;
}

/**
 * 폼을 container 안에 그린다.
 * @param {HTMLElement} container
 * @param {{schema: object, value: object, ctx: object, onChange: () => void, skip?: string[]}} opts
 *   ctx: {defs, datasets: string[], columns: (dataset) => [{name,type}]|null, icons: string[], tones: string[]}
 *   value는 그 자리에서 고친다(초안 객체).
 */
export function renderForm(container, { schema, value, ctx, onChange, skip = [] }) {
  const top = resolve(schema, ctx.defs);

  function rerender(focusPath) {
    const active = focusPath || (document.activeElement && container.contains(document.activeElement) ? document.activeElement.getAttribute("data-path") : null);
    container.replaceChildren(...properties(top, value, [], 0));
    if (active) {
      const el = container.querySelector(`[data-path="${CSS.escape(active)}"]`);
      if (el) el.focus();
    }
  }
  const changed = (structural, focusPath) => { onChange(); if (structural) rerender(focusPath); };

  function properties(s, obj, path, depth) {
    const required = new Set(s.required || []);
    return Object.entries(s.properties || {})
      .filter(([name]) => !(depth === 0 && skip.includes(name)))
      .map(([name, sub]) => node({
        schema: resolve(sub, ctx.defs), name, label: name, path: [...path, name], depth,
        required: required.has(name),
        get: () => obj[name],
        set: (v) => { if (v === undefined) delete obj[name]; else obj[name] = v; },
      }));
  }

  /** 값 하나의 컨트롤(레이블 포함) */
  function node(n) {
    const s = n.schema;
    const kind = kindOf(s, n.name, n.depth);
    const pathId = n.path.join(".");
    const meta = { hint: s.description, required: n.required, requiredText: S.required };
    const attrs = { "data-path": pathId, id: uid("opt") };

    if (kind === "object") return objectNode(n, pathId);
    if (kind === "map") return mapNode(n, pathId);
    if (kind === "array") return arrayNode(n, pathId);

    if (kind === "boolean") {
      const box = h("input", { type: "checkbox", ...attrs, checked: n.get() === true });
      box.addEventListener("change", () => { n.set(box.checked); changed(false); });
      return h("div", { class: "field check" }, box, h("label", { for: box.id, text: n.label }),
        s.description ? h("span", { class: "hint", text: s.description }) : null);
    }

    let el;
    if (kind === "column") {
      const cols = ctx.columns(ctx.dataset());
      const cur = n.get();
      if (cols && cols.length) {
        const opts = cols.map((c) => ({ value: c.name, label: c.type ? `${c.name} (${c.type})` : c.name }));
        if (!blank(cur) && !cols.some((c) => c.name === cur)) opts.push({ value: cur, label: S.notInList(cur) });
        el = select(opts, cur, { ...attrs, placeholder: n.required ? S.chooseColumn : S.none, onchange: (v) => { n.set(v || undefined); changed(false); } });
      } else {
        el = textInput(n, attrs);
        meta.hint = [s.description, S.noColumnList].filter(Boolean).join(" · ");
      }
    } else if (kind === "dataset") {
      const opts = ctx.datasets.slice();
      const cur = n.get();
      if (!blank(cur) && !opts.includes(cur)) opts.push({ value: cur, label: S.notInList(cur) });
      el = select(opts, cur, { ...attrs, placeholder: S.chooseDataset, onchange: (v) => { n.set(v || undefined); changed(true, pathId); } });
    } else if (kind === "enum" || kind === "tone" || kind === "icon") {
      const values = Array.isArray(s.enum) ? s.enum : (kind === "tone" ? ctx.tones : ctx.icons) || [];
      const opts = values.map((v) => ({ value: String(v), label: kind === "tone" ? S.toneLabel(v) : (s["x-labels"] && s["x-labels"][v]) || String(v) }));
      el = select(opts, n.get(), { ...attrs, placeholder: n.required ? S.choose : S.none, onchange: (v) => {
        const hit = values.find((x) => String(x) === v);
        n.set(hit === undefined ? undefined : hit); changed(false);
      } });
    } else if (kind === "number") {
      el = h("input", { type: "number", class: "input", ...attrs, min: s.minimum, max: s.maximum, step: s.type === "integer" ? 1 : "any", value: n.get() ?? "" });
      el.addEventListener("input", () => { n.set(el.value === "" || Number.isNaN(Number(el.value)) ? undefined : Number(el.value)); changed(false); });
      if (s.minimum != null || s.maximum != null) meta.hint = [s.description, S.range(s.minimum, s.maximum)].filter(Boolean).join(" · ");
    } else if (kind === "json") {
      el = h("textarea", { class: "input mono", rows: 3, ...attrs, spellcheck: "false" });
      el.value = n.get() === undefined ? "" : JSON.stringify(n.get(), null, 1);
      el.addEventListener("input", () => {
        if (!el.value.trim()) { n.set(undefined); el.removeAttribute("aria-invalid"); changed(false); return; }
        try { n.set(JSON.parse(el.value)); el.removeAttribute("aria-invalid"); changed(false); }
        catch (e) { el.setAttribute("aria-invalid", "true"); }
      });
      meta.hint = [s.description, S.jsonHint].filter(Boolean).join(" · ");
    } else {
      el = textInput(n, { ...attrs, type: kind === "date" ? "date" : "text" });
    }
    return field(n.label, el, { ...meta, cls: n.compact ? "compact" : "" });
  }

  function textInput(n, attrs) {
    const s = n.schema;
    const el = h("input", { type: "text", class: "input", autocomplete: "off", ...attrs, maxlength: s.maxLength, value: n.get() ?? "" });
    el.addEventListener("input", () => { n.set(el.value === "" && !n.required ? undefined : el.value); changed(false); });
    return el;
  }

  /** 중첩 객체: 선택 옵션이면 "사용" 체크로 켜고 끈다 */
  function objectNode(n, pathId) {
    const s = n.schema;
    const on = n.get() && typeof n.get() === "object";
    const legend = h("legend", {}, h("span", { class: "legend-name", text: n.label }),
      n.required ? h("span", { class: "req", text: ` ${S.required}` }) : null);
    const set = h("fieldset", { class: "group" }, legend);
    if (!n.required) {
      const box = h("input", { type: "checkbox", id: uid("use"), "data-path": pathId, checked: on });
      box.addEventListener("change", () => { n.set(box.checked ? defaultsFor(s, ctx, n.name, n.depth) : undefined); changed(true, pathId); });
      legend.append(h("span", { class: "legend-use" }, box, h("label", { for: box.id, text: S.useOption })));
    }
    if (s.description) set.append(h("p", { class: "hint", text: s.description }));
    if (n.required && !on) n.set(defaultsFor(s, ctx, n.name, n.depth));
    if (n.required || on) set.append(...properties(s, n.get(), n.path, n.depth + 1));
    return set;
  }

  /** 배열: 항목 추가·삭제·위/아래 이동 */
  function arrayNode(n, pathId) {
    const s = n.schema;
    const itemSchema = resolve(s.items, ctx.defs);
    if (!Array.isArray(n.get())) { if (n.required) n.set([]); }
    const arr = () => (Array.isArray(n.get()) ? n.get() : []);
    const set = h("fieldset", { class: "group" },
      h("legend", {}, h("span", { class: "legend-name", text: n.label }), n.required ? h("span", { class: "req", text: ` ${S.required}` }) : null));
    if (s.description) set.append(h("p", { class: "hint", text: s.description }));
    const list = h("ol", { class: "arr" });
    arr().forEach((item, i) => {
      const ipath = [...n.path, String(i)];
      const move = (to) => { const a = arr(); const [x] = a.splice(i, 1); a.splice(to, 0, x); changed(true, `${pathId}.${to}.__up`); };
      const body = isObject(itemSchema)
        ? h("div", { class: "arr-fields" }, properties(itemSchema, item, ipath, n.depth + 1).map((el) => { el.classList.add("compact"); return el; }))
        : node({ schema: itemSchema, name: "", label: S.itemLabel(i + 1), path: ipath, depth: n.depth + 1, required: true, compact: true,
          get: () => arr()[i], set: (v) => { arr()[i] = v === undefined ? "" : v; } });
      list.append(h("li", { class: `arr-item${isObject(itemSchema) ? "" : " is-scalar"}` },
        h("span", { class: "arr-no num", text: String(i + 1), "aria-hidden": "true" }),
        h("div", { class: "arr-body" }, body),
        h("div", { class: "arr-actions" },
          iconButton("up", S.itemUp(n.label, i + 1), { "data-path": `${pathId}.${i}.__up`, disabled: i === 0, onclick: () => move(i - 1) }),
          iconButton("down", S.itemDown(n.label, i + 1), { disabled: i === arr().length - 1, onclick: () => move(i + 1) }),
          iconButton("trash", S.itemRemove(n.label, i + 1), { onclick: () => { arr().splice(i, 1); changed(true, `${pathId}.__add`); } }))));
    });
    set.append(list);
    if (!arr().length) set.append(h("p", { class: "hint", text: S.arrayEmpty }));
    set.append(button(S.itemAdd, { iconName: "plus", cls: "small", "data-path": `${pathId}.__add`, onclick: () => {
      if (!Array.isArray(n.get())) n.set([]);
      n.get().push(defaultsFor(itemSchema, ctx, "", n.depth + 1));
      changed(true, `${pathId}.__add`);
    } }));
    return set;
  }

  /** 맵(키 → 객체): 키 이름을 고치고 항목을 추가·삭제 */
  function mapNode(n, pathId) {
    const s = n.schema;
    const itemSchema = resolve(s.additionalProperties, ctx.defs);
    const map = () => (n.get() && typeof n.get() === "object" ? n.get() : {});
    const set = h("fieldset", { class: "group" },
      h("legend", {}, h("span", { class: "legend-name", text: n.label }), n.required ? h("span", { class: "req", text: ` ${S.required}` }) : null));
    if (s.description) set.append(h("p", { class: "hint", text: s.description }));
    const list = h("ol", { class: "arr" });
    Object.keys(map()).forEach((key, i) => {
      const keyInput = h("input", { type: "text", class: "input mono", id: uid("key"), autocomplete: "off", value: key, "data-path": `${pathId}.#${i}` });
      keyInput.addEventListener("change", () => {
        const next = keyInput.value;
        if (next === key) return;
        if (next in map()) { keyInput.value = key; return; }
        const rebuilt = {};
        for (const [k, v] of Object.entries(map())) rebuilt[k === key ? next : k] = v;
        n.set(rebuilt); changed(true, `${pathId}.#${i}`);
      });
      const fields = isObject(itemSchema)
        ? properties(itemSchema, map()[key], [...n.path, key], n.depth + 1).map((el) => { el.classList.add("compact"); return el; })
        : [node({ schema: itemSchema, name: "", label: S.mapValue, path: [...n.path, key], depth: n.depth + 1, required: true, compact: true,
          get: () => map()[key], set: (v) => { map()[key] = v; } })];
      list.append(h("li", { class: "arr-item" },
        h("span", { class: "arr-no num", text: String(i + 1), "aria-hidden": "true" }),
        h("div", { class: "arr-body" }, h("div", { class: "arr-fields" }, field(S.mapKey, keyInput, { cls: "compact" }), fields)),
        h("div", { class: "arr-actions" },
          iconButton("trash", S.itemRemove(n.label, key || i + 1), { onclick: () => {
            const rest = { ...map() }; delete rest[key];
            n.set(Object.keys(rest).length || n.required ? rest : undefined); changed(true, `${pathId}.__add`);
          } }))));
    });
    set.append(list);
    if (!Object.keys(map()).length) set.append(h("p", { class: "hint", text: S.arrayEmpty }));
    set.append(button(S.itemAdd, { iconName: "plus", cls: "small", "data-path": `${pathId}.__add`, disabled: "" in map(), onclick: () => {
      n.set({ ...map(), "": defaultsFor(itemSchema, ctx, "", n.depth + 1) });
      changed(true, `${pathId}.#${Object.keys(map()).length - 1}`);
    } }));
    return set;
  }

  rerender();
  return { rerender };
}
