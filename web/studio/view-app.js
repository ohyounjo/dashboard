// 앱 화면: 제목, 로고 글자, 기준일, 색 → app.update 연산 하나.
import S from "./strings.js";
import { h, fill, uid, field, button, alertBox, clone, same } from "./dom.js";

const HEX = /^#[0-9A-Fa-f]{6}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DEFAULT_COLORS = { light: "#1428A0", dark: "#7B8CFF" };   // tokens.css의 --primary 기본값

export function mount(container, ctx) {
  let original, pending, dirty = false, busy = false, error = null;

  function load() {
    const app = (ctx.config.dashboard && ctx.config.dashboard.app) || {};
    const primary = app.theme && app.theme.primary;
    original = {
      title: app.title || "",
      logo_text: app.logo_text || "",
      as_of: app.as_of || "today",
      theme: primary ? { primary: { light: primary.light, dark: primary.dark } } : null,
    };
    pending = clone(original); dirty = false; error = null;
  }

  let alive = true;   // 화면을 떠나면 false(떠난 뒤 끝난 저장이 컨테이너를 다시 그리지 않게)

  async function save() {
    if (busy) return;
    const values = {};
    if (pending.title !== original.title) values.title = pending.title;
    if (pending.logo_text !== original.logo_text) values.logo_text = pending.logo_text || null;
    if (pending.as_of !== original.as_of) values.as_of = pending.as_of;
    if (!same(pending.theme, original.theme)) values.theme = pending.theme;
    const problems = [];
    if (!pending.title.trim()) problems.push(S.appTitleEmpty);
    if (pending.as_of !== "today" && !DATE.test(pending.as_of)) problems.push(S.appDateInvalid);
    if (pending.theme && !(HEX.test(pending.theme.primary.light) && HEX.test(pending.theme.primary.dark))) problems.push(S.appColorInvalid);
    if (problems.length) { error = { title: S.errInvalid, body: null, errors: problems }; ctx.announce(S.errInvalid); render(); return; }
    if (!Object.keys(values).length) return;
    busy = true; error = null; render();
    try {
      await ctx.patch([{ op: "app.update", values }]);
      if (!alive) return;
      load(); ctx.announce(S.saved);
    } catch (e) {
      if (!alive) return;
      const info = ctx.describeError(e);
      if (info.detail === "stale_config") load();   // 설정을 새로 읽었다 → 초안을 버린다
      error = info; ctx.announce(error.title);
    }
    busy = false; render();
  }

  function colorField(label, mode) {
    const id = uid("color");
    const value = pending.theme.primary[mode];
    const picker = h("input", { type: "color", id, value: HEX.test(value) ? value : DEFAULT_COLORS[mode], class: "color" });
    const text = h("input", { type: "text", class: "input mono", value, maxlength: 7, autocomplete: "off", "aria-label": S.appColorHex(label), "data-color": mode });
    picker.addEventListener("input", () => { pending.theme.primary[mode] = picker.value.toUpperCase(); text.value = pending.theme.primary[mode]; touch(); });
    text.addEventListener("input", () => { pending.theme.primary[mode] = text.value.trim(); if (HEX.test(text.value.trim())) picker.value = text.value.trim(); touch(); });
    return h("div", { class: "field" }, h("label", { for: id, text: label }), h("div", { class: "color-row" }, picker, text));
  }

  let actions = null;
  function touch() { dirty = !same(pending, original); paintActions(); }
  function paintActions() {
    if (!actions) return;
    actions.save.disabled = busy || !dirty;
    actions.revert.disabled = busy || !dirty;
    actions.note.textContent = busy ? S.saving : dirty ? S.unsaved : "";
  }

  function render() {
    if (!alive) return;   // 화면을 떠난 뒤 끝난 요청은 그리지 않는다
    const title = h("input", { type: "text", class: "input", value: pending.title, autocomplete: "off", "data-app": "title" });
    title.addEventListener("input", () => { pending.title = title.value; touch(); });
    const logo = h("input", { type: "text", class: "input short", value: pending.logo_text, maxlength: 3, autocomplete: "off", "data-app": "logo_text" });
    logo.addEventListener("input", () => { pending.logo_text = logo.value; touch(); });

    const fixed = pending.as_of !== "today";
    const name = uid("asof");
    const idToday = uid("r"), idFixed = uid("r");
    const date = h("input", { type: "date", class: "input short", value: fixed ? pending.as_of : "", disabled: !fixed, "aria-label": S.appAsOfDate, "data-app": "as_of" });
    const rToday = h("input", { type: "radio", name, id: idToday, checked: !fixed });
    const rFixed = h("input", { type: "radio", name, id: idFixed, checked: fixed });
    rToday.addEventListener("change", () => { pending.as_of = "today"; date.disabled = true; touch(); });
    rFixed.addEventListener("change", () => { pending.as_of = date.value || ""; date.disabled = false; date.focus(); touch(); });
    date.addEventListener("input", () => { pending.as_of = date.value; touch(); });

    const useId = uid("use");
    const use = h("input", { type: "checkbox", id: useId, checked: !!pending.theme, "data-app": "theme" });
    use.addEventListener("change", () => {
      pending.theme = use.checked ? (original.theme ? clone(original.theme) : { primary: { ...DEFAULT_COLORS } }) : null;
      dirty = !same(pending, original); render();
    });

    const save_ = button(S.save, { iconName: "check", cls: "primary", "data-action": "save", onclick: save });
    const revert = button(S.revert, { onclick: () => { load(); render(); } });
    const note = h("span", { class: "dirty-note", role: "status" });
    actions = { save: save_, revert, note };
    paintActions();

    const form = h("form", { class: "panel form app-form", novalidate: true },
      field(S.appTitle, title, { required: true, requiredText: S.required, hint: S.appTitleHint }),
      field(S.appLogo, logo, { hint: S.appLogoHint }),
      h("fieldset", { class: "group" }, h("legend", {}, h("span", { class: "legend-name", text: S.appAsOf })),
        h("p", { class: "hint", text: S.appAsOfHint }),
        h("div", { class: "field check" }, rToday, h("label", { for: idToday, text: S.appAsOfToday })),
        h("div", { class: "field check" }, rFixed, h("label", { for: idFixed, text: S.appAsOfFixed }), date)),
      h("fieldset", { class: "group" }, h("legend", {}, h("span", { class: "legend-name", text: S.appTheme }),
        h("span", { class: "legend-use" }, use, h("label", { for: useId, text: S.appThemeUse }))),
        h("p", { class: "hint", text: S.appThemeHint }),
        pending.theme ? h("div", { class: "color-grid" }, colorField(S.appColorLight, "light"), colorField(S.appColorDark, "dark")) : null),
      error ? alertBox({ title: error.title, body: error.body, errors: error.errors }) : null,
      h("div", { class: "form-actions" }, save_, revert, note));
    form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
    // 저장 중에는 입력을 막는다. 응답이 오면 초안을 서버 값으로 다시 읽으므로, 그 사이의 입력은 저장되지 않은 채 사라진다
    form.inert = busy;
    if (busy) form.setAttribute("aria-busy", "true");

    fill(container, h("div", { class: "view-head" }, h("h2", { text: S.screenApp }), h("p", { class: "intro", text: S.appLead })), form);
  }

  load();
  render();
  return { canLeave: () => !dirty || window.confirm(S.discardConfirm), destroy: () => { alive = false; } };
}
