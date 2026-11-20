// 내 정보(account.html). 모든 등급이 쓴다. 비밀번호 변경이 필요한 계정(임시 비밀번호)은 여기로만 올 수 있다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { applySavedTheme, h, loginUrl, postJson } from "./common.js";

const T = S.account;
const A = S.auth;
const $ = (id) => document.getElementById(id);

function showMessage(text, tone = "bad") {
  const box = $("msg");
  if (!text) { box.hidden = true; box.replaceChildren(); return; }
  box.replaceChildren(text);
  box.setAttribute("data-tone", tone);
  box.setAttribute("role", tone === "bad" || tone === "warn" ? "alert" : "status");
  box.hidden = false;
}

const fmt = (iso) => (iso ? S.admin.fmtDateTime(iso) : "—");

function row(label, value) {
  return [h("dt", { text: label }), h("dd", {}, value)];
}

function field(id, label, attrs, hint) {
  const input = h("input", { id, type: "password", required: true, maxlength: 128, ...attrs });
  return { input, el: h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("span", { class: "hint", text: hint }) : null) };
}

async function boot() {
  applySavedTheme();
  document.documentElement.lang = S.lang;
  document.title = T.title;
  $("title").textContent = T.title;
  $("back").replaceChildren(icon("home"), T.back);
  let me;
  try {
    const res = await fetch("/api/me", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.status === 401) { location.replace(loginUrl("/account.html")); return; }
    if (!res.ok) throw new Error(String(res.status));
    me = await res.json();
  } catch (e) {
    showMessage(A.errors.network);
    return;
  }
  $("info").replaceChildren(...[
    row(T.email, me.email), row(T.role, S.roleLabel[me.role] || me.role), row(T.createdAt, fmt(me.created_at)),
    row(T.lastLogin, fmt(me.last_login_at)), row(T.groups, me.groups.map((g) => g.name).join(", ") || T.noGroups),
  ].flat());
  $("pwTitle").textContent = T.passwordTitle;
  $("pwIntro").textContent = T.passwordIntro;
  if (me.must_change_password) {
    showMessage(T.mustChange, "warn");
    $("back").hidden = true;
  }

  const current = field("current", T.current, { autocomplete: "current-password", maxlength: 1024 });
  const pw = field("password", A.newPassword, { autocomplete: "new-password" }, A.passwordHint(10));
  const pw2 = field("password2", A.passwordConfirm, { autocomplete: "new-password" });
  const submit = h("button", { class: "btn primary block", type: "submit" }, T.submit);
  const form = $("form");
  form.replaceChildren(current.el, pw.el, pw2.el, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const empty = [current, pw, pw2].find((f) => !f.input.value);
    if (empty) { empty.input.focus(); showMessage(A.required); return; }
    if (pw.input.value !== pw2.input.value) { pw2.input.focus(); showMessage(A.mismatch); return; }
    submit.disabled = true;
    let res;
    try { res = await postJson("/api/me/password", { current: current.input.value, new: pw.input.value }); } catch (err) { res = null; }
    submit.disabled = false;
    if (res && res.status === 204) {
      form.replaceChildren(h("a", { class: "btn primary block", href: "/" }, T.continue));
      $("back").hidden = false;
      showMessage(T.changed, "good");
      return;
    }
    if (res && res.status === 401) {
      const body = await res.json().catch(() => ({}));
      if (body.detail === "unauthenticated") { location.replace(loginUrl("/account.html", "expired")); return; }
      showMessage(T.errors.invalid_credentials); current.input.focus(); return;
    }
    const body = res ? await res.json().catch(() => ({})) : {};
    if (body.detail === "weak_password") {
      showMessage(body.reason === "same_as_current" ? T.sameAsCurrent : (A.weak[body.reason] ? A.weak[body.reason](10) : A.errors.weak_password));
      pw.input.focus();
      return;
    }
    showMessage(res ? (A.errors[body.detail] || A.errors.server(res.status)) : A.errors.network);
  });
}

boot();
