// DOM 도우미. 설정·데이터 값은 항상 텍스트 노드로 넣는다(innerHTML 없음).
import { icon } from "./icons.js";

/** 요소 생성. 문자열 자식은 텍스트 노드로 넣는다. on* 속성은 addEventListener로 건다(CSP: 인라인 핸들러 없음). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** el의 자식을 바꾼다(null·false는 건너뛴다) */
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false).map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}

export const $ = (id) => document.getElementById(id);

let seq = 0;
/** 레이블과 컨트롤을 잇는 고유 id */
export const uid = (prefix = "f") => `${prefix}-${++seq}`;

/** 아이콘 + 글자 버튼 */
export function button(label, { iconName, cls = "", onclick, title, ...attrs } = {}) {
  return h("button", { type: "button", class: `btn ${cls}`.trim(), onclick, title, ...attrs },
    iconName ? icon(iconName) : null, label ? h("span", { text: label }) : null);
}

/** 아이콘만 있는 버튼(aria-label 필수) */
export function iconButton(iconName, label, { cls = "", onclick, ...attrs } = {}) {
  return h("button", { type: "button", class: `btn icon-only ${cls}`.trim(), "aria-label": label, title: label, onclick, ...attrs }, icon(iconName));
}

/** 상태 배지: 색조 + 아이콘 + 글자(색만으로 구분하지 않는다) */
export function badge(text, tone = "neutral", iconName = null) {
  return h("span", { class: "badge", "data-tone": tone }, iconName ? icon(iconName) : null, h("span", { text }));
}

/** 알림 상자. errors는 한 줄씩 목록으로 보여 준다. */
export function alertBox({ tone = "bad", title, body, errors, actions, role }) {
  const iconName = tone === "good" ? "check" : tone === "primary" ? "info" : "warning";
  return h("div", { class: "alert", "data-tone": tone, role: role || (tone === "bad" ? "alert" : "status") },
    h("div", { class: "alert-ico" }, icon(iconName)),
    h("div", { class: "alert-body" },
      title ? h("strong", { text: title }) : null,
      body ? h("p", { text: body }) : null,
      errors && errors.length ? h("ul", { class: "errors mono" }, errors.map((e) => h("li", { text: e }))) : null,
      actions ? h("div", { class: "alert-actions" }, actions) : null));
}

/** 빈 상태·오류 상태(가운데 정렬) */
export function stateBox({ tone = "neutral", iconName = "info", title, body, actions, role = "status" }) {
  return h("div", { class: "state", role, "data-tone": tone },
    h("div", { class: "state-ico" }, icon(iconName)),
    title ? h("h3", { text: title }) : null,
    body ? h("p", { text: body }) : null,
    actions ? h("div", { class: "state-actions" }, actions) : null);
}

/** 로딩 자리 표시 */
export function skeleton(note) {
  return h("div", { class: "sk", role: "status", "aria-busy": "true" },
    h("div", { class: "sk-note", text: note }),
    h("div", { class: "sk-block line" }), h("div", { class: "sk-block bar" }), h("div", { class: "sk-block bar" }), h("div", { class: "sk-block bar" }));
}

/** 레이블이 있는 폼 줄. control은 id가 있는 요소. */
export function field(label, control, { hint, required, requiredText, cls = "" } = {}) {
  if (!control.id) control.id = uid();
  const hintEl = hint ? h("span", { class: "hint", id: `${control.id}-hint`, text: hint }) : null;
  if (hintEl) control.setAttribute("aria-describedby", hintEl.id);
  if (required) control.setAttribute("aria-required", "true");
  return h("div", { class: `field ${cls}`.trim() },
    h("label", { for: control.id }, label, required ? h("span", { class: "req", text: ` ${requiredText}` }) : null),
    control, hintEl);
}

/** select 요소. options: [{value, label}] 또는 문자열 배열. */
export function select(options, value, { onchange, placeholder, ...attrs } = {}) {
  const el = h("select", { class: "input", ...attrs });
  if (placeholder != null) el.append(h("option", { value: "", text: placeholder }));
  for (const o of options) {
    const opt = typeof o === "string" ? { value: o, label: o } : o;
    el.append(h("option", { value: opt.value, text: opt.label }));
  }
  el.value = value == null ? "" : String(value);
  if (onchange) el.addEventListener("change", () => onchange(el.value));
  return el;
}

/** 파일 끌어다 놓기 영역으로 만든다. onFiles(File[])를 부른다. */
export function fileDrop(el, onFiles) {
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  el.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; el.classList.add("is-over"); });
  el.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  el.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) el.classList.remove("is-over"); });
  el.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; el.classList.remove("is-over");
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) onFiles(files);
  });
}

/** 숨긴 file input + 버튼. 버튼을 돌려준다(input은 버튼 옆에 붙는다). */
export function filePicker(label, { multiple = false, onFiles, cls = "" } = {}) {
  const input = h("input", { type: "file", accept: ".csv,text/csv", class: "sr-only", multiple, tabindex: "-1", "aria-label": label });
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    input.value = "";
    if (files.length) onFiles(files);
  });
  const btn = button(label, { iconName: "upload", cls, onclick: () => input.click() });
  return h("span", { class: "file-pick" }, btn, input);
}

export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
