// 데이터 화면: 테이블 카드에 CSV를 놓으면 검증만 먼저 하고(check), "교체"를 누르면 파일을 바꾼다(PUT).
// DB 적재는 하지 않는다. 반영은 위쪽 "적용" 버튼이다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { h, fill, button, badge, alertBox, stateBox, fileDrop, filePicker } from "./dom.js";

const MAX_COLUMNS = 14;   // 카드에 보여 줄 열 이름 수

export function mount(container, ctx) {
  const cards = new Map();   // 테이블 → {step: idle|checking|checked|replacing|replaced|error, file, result, error}

  function tables() {
    const t = ctx.config.tables;
    if (t && typeof t === "object") return Object.entries(t).map(([name, info]) => ({ name, rows: info && info.rows, columns: (info && info.columns) || [] }));
    const schema = (ctx.config.schema && ctx.config.schema.tables) || {};
    return Object.entries(schema).map(([name, spec]) => ({ name, rows: null, columns: Object.keys((spec && spec.columns) || {}) }));
  }
  const state = (name) => { if (!cards.has(name)) cards.set(name, { step: "idle" }); return cards.get(name); };

  let alive = true;   // 화면을 떠나면 false(떠난 뒤 끝난 검증·교체가 화면을 건드리지 않게)

  async function check(name, files) {
    const st = state(name);
    if (st.step === "checking" || st.step === "replacing") return;
    if (files.length !== 1 || !/\.csv$/i.test(files[0].name)) {
      Object.assign(st, { step: "error", file: null, result: null, error: { title: S.dataOneCsvTitle, body: S.dataOneCsvBody, errors: [] } });
      paint(name); return;
    }
    Object.assign(st, { step: "checking", file: files[0], result: null, error: null });
    paint(name);
    try {
      st.result = await ctx.api.checkData(ctx.project, name, st.file);
      if (!alive) return;
      st.step = "checked";
      ctx.announce(st.result.valid ? S.dataCheckOk(name) : S.dataCheckBad(name, (st.result.errors || []).length));
    } catch (e) {
      st.step = "error"; st.error = ctx.describeError(e); ctx.announce(st.error.title);
    }
    paint(name);
  }

  async function replace(name) {
    const st = state(name);
    if (st.step !== "checked" || !st.result || !st.result.valid) return;
    st.step = "replacing"; paint(name);
    try {
      await ctx.api.putData(ctx.project, name, st.file);
      st.step = "replaced";
      ctx.refreshChanges();
      if (!alive) return;
      ctx.announce(S.dataReplaced(name));
      await ctx.reloadConfig({ quiet: true });
    } catch (e) {
      st.step = "error"; st.error = ctx.describeError(e); ctx.announce(st.error.title);
    }
    paint(name);
  }

  function diffLine(diff) {
    if (!diff) return null;
    if (diff.added == null || diff.removed == null) {
      return h("p", { class: "diff" }, badge(S.diffUnknown, "neutral", "info"), " ", h("span", { text: diff.reason || S.diffUnrecorded }), " ", h("span", { class: "num", text: S.rowCount(diff.rows) }));
    }
    return h("div", { class: "diff" },
      h("span", { class: "diff-n", "data-tone": "good" }, h("b", { class: "num", text: `+${diff.added}` }), ` ${S.diffAdded}`),
      h("span", { class: "diff-n", "data-tone": "bad" }, h("b", { class: "num", text: `−${diff.removed}` }), ` ${S.diffRemoved}`),
      h("span", { class: "diff-n", "data-tone": "warn" }, h("b", { class: "num", text: `~${diff.changed ?? 0}` }), ` ${S.diffChanged}`),
      diff.unchanged != null ? h("span", { class: "diff-n", "data-tone": "neutral" }, h("b", { class: "num", text: String(diff.unchanged) }), ` ${S.diffUnchanged}`) : null,
      diff.rows != null ? h("span", { class: "muted num", text: S.diffTotal(diff.rows) }) : null,
      diff.comparable === false ? h("p", { class: "hint", text: diff.reason ? S.diffNotComparable(diff.reason) : S.diffNotComparable("") }) : null);
  }

  function resultBox(name) {
    const st = state(name);
    const reset = () => { cards.set(name, { step: "idle" }); paint(name); };
    if (st.step === "idle") return null;
    if (st.step === "checking") return h("p", { class: "busy-line", role: "status", text: S.dataChecking(st.file.name) });
    if (st.step === "error") return alertBox({ title: st.error.title, body: st.error.body, errors: st.error.errors, actions: button(S.close, { cls: "small", onclick: reset }) });
    if (st.step === "replaced") {
      return alertBox({ tone: "good", title: S.dataReplacedTitle(st.file.name), body: S.dataReplacedBody,
        actions: [button(S.apply, { iconName: "play", cls: "primary small", onclick: () => ctx.apply() }), button(S.close, { cls: "small", onclick: reset })] });
    }
    const r = st.result;
    const errors = r.errors || [];
    return h("div", { class: "check-result" },
      h("p", { class: "check-file" }, icon("file"), h("span", { class: "mono", text: st.file.name }), " ",
        r.valid ? badge(S.dataValid, "good", "check") : badge(S.dataInvalid(errors.length), "bad", "warning")),
      errors.length ? h("ul", { class: "errors mono", "aria-label": S.dataErrors }, errors.map((e) => h("li", { text: e }))) : null,
      r.valid ? diffLine(r.diff) : null,
      r.valid && !r.diff && r.diff_error ? h("p", { class: "hint", text: S.diffError(r.diff_error) }) : null,
      h("div", { class: "form-actions" },
        button(st.step === "replacing" ? S.dataReplacing : S.dataReplace, { iconName: "check", cls: "primary", "data-action": "replace", disabled: !r.valid || st.step === "replacing", onclick: () => replace(name) }),
        button(S.cancel, { disabled: st.step === "replacing", onclick: reset })),
      r.valid ? h("p", { class: "hint", text: S.dataReplaceHint }) : h("p", { class: "hint", text: S.dataFixHint }));
  }

  const els = new Map();   // 테이블 → 결과 영역

  function paint(name) {
    const el = els.get(name);
    if (el) fill(el, resultBox(name));
  }

  function card(t) {
    const names = t.columns.map((c) => (typeof c === "string" ? c : c.name));
    const result = h("div", { class: "card-result" });
    els.set(t.name, result);
    const el = h("li", { class: "panel data-card dropzone", "data-table": t.name },
      h("div", { class: "data-card-head" },
        h("h3", {}, icon("database"), h("span", { class: "mono", text: t.name })),
        t.rows != null ? h("span", { class: "muted num", text: S.rowCount(t.rows) }) : null),
      h("p", { class: "cols" }, names.slice(0, MAX_COLUMNS).map((n) => h("span", { class: "col-chip mono", text: n })),
        names.length > MAX_COLUMNS ? h("span", { class: "muted", text: S.moreColumns(names.length - MAX_COLUMNS) }) : null),
      h("div", { class: "data-card-drop" },
        h("span", { class: "muted", text: S.dataDropHint }),
        filePicker(S.dataPick(t.name), { cls: "small", onFiles: (files) => check(t.name, files) })),
      result);
    fileDrop(el, (files) => check(t.name, files));
    fill(result, resultBox(t.name));
    return el;
  }

  function render() {
    if (!alive) return;   // 화면을 떠난 뒤 끝난 요청은 그리지 않는다
    els.clear();
    const list = tables();
    fill(container,
      h("div", { class: "view-head" }, h("h2", { text: S.screenData }), h("p", { class: "intro", text: S.dataLead })),
      list.length ? h("ul", { class: "data-grid" }, list.map(card)) : stateBox({ iconName: "database", title: S.dataEmptyTitle, body: S.dataEmptyBody }));
  }

  render();
  return { refresh: render, destroy: () => { alive = false; } };
}
