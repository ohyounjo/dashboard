// 로그인 전 화면(login·signup·forgot·reset·verify.html). body의 data-page로 화면을 고른다. 문구는 strings.js(S.auth)에만 둔다.
// 메일 링크의 토큰은 URL 조각(#token=…)으로 온다. 읽은 뒤 주소창에서 지운다(화면 공유·기록에 남지 않게).
import S from "./strings.js";
import { icon } from "./icons.js";
import { applySavedTheme, h, postJson, safeNext } from "./common.js";

const A = S.auth;
const $ = (id) => document.getElementById(id);
let config = { email_domains: [], mail_mode: "test", password_min: 10 };

function showMessage(text, tone = "bad") {
  const box = $("msg");
  if (!text) { box.hidden = true; box.replaceChildren(); return; }
  box.replaceChildren(text);
  box.setAttribute("data-tone", tone);
  box.setAttribute("role", tone === "bad" || tone === "warn" ? "alert" : "status");
  box.hidden = false;
}

async function readBody(res) {
  try { const j = await res.json(); return j && typeof j === "object" ? j : {}; } catch (e) { return {}; }
}

function errorText(status, body) {
  const detail = body && body.detail;
  if (detail === "weak_password" && body.reason && A.weak[body.reason]) return A.weak[body.reason](config.password_min);
  if (detail && typeof A.errors[detail] === "string") return A.errors[detail];
  return A.errors.server(status);
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

/** POST 후 (상태, 본문). 연결 실패면 null. */
async function send(path, body) {
  try {
    const res = await postJson(path, body);
    return { status: res.status, ok: res.ok, body: res.status === 204 ? {} : await readBody(res) };
  } catch (e) {
    return null;
  }
}

function field(id, label, type, attrs = {}, hint) {
  const input = h("input", { id, name: id, type, required: true, ...attrs });
  return { input, el: h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("span", { class: "hint", text: hint }) : null) };
}

function links(...items) {
  $("links").replaceChildren(...items.filter(Boolean).map(([href, text]) => h("a", { href, text })));
}

function setHeader(title, intro) {
  document.title = title;
  $("title").textContent = title;
  $("intro").textContent = intro || "";
  $("intro").hidden = !intro;
}

/** 다 끝난 화면: 폼을 지우고 안내만 남긴다. */
function done(text, tone = "good") {
  $("form").replaceChildren();
  showMessage(text, tone);
}

/** 메일 링크의 토큰(#token=…). 읽은 뒤 주소창에서 지운다. */
function takeToken() {
  const m = location.hash.match(/(?:^#|&)token=([A-Za-z0-9_-]+)/);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  return m ? m[1] : null;
}

async function loadConfig(showBanner) {
  try {
    const res = await fetch("/api/auth/config", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.ok) config = { ...config, ...(await res.json()) };
  } catch (e) { /* 기본값으로 */ }
  if (showBanner && config.mail_mode === "test") {
    $("banner").textContent = A.mailTestBanner;
    $("banner").hidden = false;
  }
}

/* ---------------- 로그인 ---------------- */

function loginPage() {
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next") || "/");
  setHeader(A.loginTitle, A.loginIntro);
  $("emailLabel").textContent = A.email;
  $("passwordLabel").textContent = A.password;
  const submit = $("submit");
  setBusy(submit, false, A.loginSubmit);
  links(["/signup.html", A.toSignup], ["/forgot.html", A.toForgot]);
  const reason = params.get("reason");
  if (reason === "expired") showMessage(S.sessionExpired, "warn");
  else if (reason === "logout") showMessage(A.loggedOut, "neutral");
  loadConfig(true);

  const email = $("email"), password = $("password");
  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    showMessage("");
    email.removeAttribute("aria-invalid");
    password.removeAttribute("aria-invalid");
    if (!email.value.trim() || !password.value) {
      if (!email.value.trim()) email.setAttribute("aria-invalid", "true");
      if (!password.value) password.setAttribute("aria-invalid", "true");
      showMessage(A.required);
      (email.value.trim() ? password : email).focus();
      return;
    }
    setBusy(submit, true, A.loginSubmitting);
    const r = await send("/api/auth/login", { email: email.value.trim(), password: password.value });
    if (r && r.ok) { location.replace(r.body.must_change_password ? "/account.html" : next); return; }
    setBusy(submit, false, A.loginSubmit);
    if (!r) { showMessage(A.errors.network); return; }
    const detail = r.body.detail;
    if (detail === "email_not_verified") {
      // 인증 메일 다시 보내기(응답은 항상 202)
      const again = h("button", { type: "button", class: "btn link" }, A.resend);
      again.addEventListener("click", async () => {
        again.disabled = true;
        const rr = await send("/api/auth/verify/resend", { email: email.value.trim() });
        showMessage(rr && rr.ok ? A.resent : errorText(rr ? rr.status : 0, rr ? rr.body : {}), rr && rr.ok ? "good" : "bad");
      });
      showMessage(h("span", {}, A.errors.email_not_verified, " ", again), "warn");
      return;
    }
    showMessage(r ? errorText(r.status, r.body) : A.errors.network, detail === "account_locked" ? "warn" : "bad");
    if (detail === "invalid_credentials") {
      password.value = "";
      password.setAttribute("aria-invalid", "true");
      password.focus();
    }
  });
  email.focus();
}

/* ---------------- 가입 ---------------- */

async function signupPage() {
  setHeader(A.signupTitle, A.signupIntro);
  links(["/login.html", A.toLogin]);
  await loadConfig(true);
  const email = field("email", A.email, "email", { autocomplete: "username", inputmode: "email", autocapitalize: "off", spellcheck: "false" },
    A.emailHint(config.email_domains));
  const pw = field("password", A.password, "password", { autocomplete: "new-password", maxlength: 128 }, A.passwordHint(config.password_min));
  const pw2 = field("password2", A.passwordConfirm, "password", { autocomplete: "new-password", maxlength: 128 });
  const submit = h("button", { class: "btn primary block", type: "submit" }, A.signupSubmit);
  const form = $("form");
  form.replaceChildren(email.el, pw.el, pw2.el, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showMessage("");
    [email, pw, pw2].forEach((f) => f.input.removeAttribute("aria-invalid"));
    const empty = [email, pw, pw2].find((f) => !f.input.value.trim());
    if (empty) { empty.input.setAttribute("aria-invalid", "true"); empty.input.focus(); showMessage(A.required); return; }
    if (pw.input.value !== pw2.input.value) { pw2.input.setAttribute("aria-invalid", "true"); pw2.input.focus(); showMessage(A.mismatch); return; }
    setBusy(submit, true, A.signupSubmit);
    const r = await send("/api/auth/signup", { email: email.input.value.trim(), password: pw.input.value });
    setBusy(submit, false, A.signupSubmit);
    if (r && r.status === 202) { done(A.signupDone); return; }
    if (!r) { showMessage(A.errors.network); return; }
    showMessage(errorText(r.status, r.body));
    const bad = r.body.detail === "weak_password" ? pw : (r.body.detail || "").startsWith("email") || r.body.detail === "invalid_email" ? email : null;
    if (bad) { bad.input.setAttribute("aria-invalid", "true"); bad.input.focus(); }
  });
  email.input.focus();
}

/* ---------------- 비밀번호 찾기 ---------------- */

function forgotPage() {
  setHeader(A.forgotTitle, A.forgotIntro);
  links(["/login.html", A.toLogin]);
  loadConfig(true);
  const email = field("email", A.email, "email", { autocomplete: "username", inputmode: "email", autocapitalize: "off", spellcheck: "false" });
  const submit = h("button", { class: "btn primary block", type: "submit" }, A.forgotSubmit);
  const form = $("form");
  form.replaceChildren(email.el, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!email.input.value.trim()) { email.input.setAttribute("aria-invalid", "true"); email.input.focus(); return; }
    setBusy(submit, true, A.forgotSubmit);
    const r = await send("/api/auth/password/forgot", { email: email.input.value.trim() });
    setBusy(submit, false, A.forgotSubmit);
    if (r && r.status === 202) { done(A.forgotDone); return; }
    showMessage(r ? errorText(r.status, r.body) : A.errors.network);
  });
  email.input.focus();
}

/* ---------------- 새 비밀번호(재설정·초대 수락) ---------------- */

async function resetPage() {
  const token = takeToken();
  setHeader(A.resetTitle, A.resetIntro);
  links(["/login.html", A.toLogin], ["/forgot.html", A.toForgot]);
  if (!token) { done(A.noToken, "bad"); return; }
  await loadConfig(false);
  const pw = field("password", A.newPassword, "password", { autocomplete: "new-password", maxlength: 128 }, A.passwordHint(config.password_min));
  const pw2 = field("password2", A.passwordConfirm, "password", { autocomplete: "new-password", maxlength: 128 });
  const submit = h("button", { class: "btn primary block", type: "submit" }, A.resetSubmit);
  const form = $("form");
  form.replaceChildren(pw.el, pw2.el, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showMessage("");
    if (!pw.input.value) { pw.input.focus(); showMessage(A.required); return; }
    if (pw.input.value !== pw2.input.value) { pw2.input.setAttribute("aria-invalid", "true"); pw2.input.focus(); showMessage(A.mismatch); return; }
    setBusy(submit, true, A.resetSubmit);
    const r = await send("/api/auth/password/reset", { token, password: pw.input.value });
    setBusy(submit, false, A.resetSubmit);
    if (r && r.status === 204) { done(A.resetDone); links(["/login.html", A.toLogin]); return; }
    if (r && r.body.detail === "token_expired") { done(A.errors.token_expired, "bad"); return; }
    showMessage(r ? errorText(r.status, r.body) : A.errors.network);
    if (r && r.body.detail === "weak_password") pw.input.focus();
  });
  pw.input.focus();
}

/* ---------------- 메일 인증 ---------------- */

async function verifyPage() {
  const token = takeToken();
  setHeader(A.verifyTitle, null);
  links(["/login.html", A.toLogin]);
  if (!token) { done(A.noToken, "bad"); return; }
  showMessage(A.verifying, "neutral");
  const r = await send("/api/auth/verify", { token });
  if (r && r.status === 204) done(A.verifyDone);
  else done(r ? errorText(r.status, r.body) : A.errors.network, "bad");
}

/* ---------------- 시작 ---------------- */

applySavedTheme();
document.documentElement.lang = S.lang;
$("mark").replaceChildren(icon("lock"));
const pages = { login: loginPage, signup: signupPage, forgot: forgotPage, reset: resetPage, verify: verifyPage };
(pages[document.body.dataset.page] || loginPage)();
