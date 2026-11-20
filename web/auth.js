// 로그인 전 화면(login.html 등). body의 data-page로 화면을 고른다. 문구는 strings.js(S.auth)에만 둔다.
import S from "./strings.js";
import { icon } from "./icons.js";
import { applySavedTheme, postJson, readDetail, safeNext } from "./common.js";

const A = S.auth;
const $ = (id) => document.getElementById(id);

function showMessage(text, tone = "bad") {
  const box = $("msg");
  if (!text) { box.hidden = true; box.textContent = ""; return; }
  box.textContent = text;
  box.setAttribute("data-tone", tone);
  box.setAttribute("role", tone === "bad" || tone === "warn" ? "alert" : "status");
  box.hidden = false;
}

function errorText(status, detail) {
  if (detail && typeof A.errors[detail] === "string") return A.errors[detail];
  return A.errors.server(status);
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

/* ---------------- 로그인 ---------------- */

function loginPage() {
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next") || "/");
  document.title = A.loginTitle;
  $("title").textContent = A.loginTitle;
  $("intro").textContent = A.loginIntro;
  $("emailLabel").textContent = A.email;
  $("passwordLabel").textContent = A.password;
  const submit = $("submit");
  setBusy(submit, false, A.loginSubmit);
  const reason = params.get("reason");
  if (reason === "expired") showMessage(S.sessionExpired, "warn");
  else if (reason === "logout") showMessage(A.loggedOut, "neutral");

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
    let res;
    try {
      res = await postJson("/api/auth/login", { email: email.value.trim(), password: password.value });
    } catch (err) {
      setBusy(submit, false, A.loginSubmit);
      showMessage(A.errors.network);
      return;
    }
    if (res.ok) {
      location.replace(next);
      return;
    }
    const detail = await readDetail(res);
    setBusy(submit, false, A.loginSubmit);
    showMessage(errorText(res.status, detail), detail === "account_locked" || detail === "email_not_verified" ? "warn" : "bad");
    if (detail === "invalid_credentials") {
      password.value = "";
      password.setAttribute("aria-invalid", "true");
      password.focus();
    }
  });
  email.focus();
}

/* ---------------- 시작 ---------------- */

applySavedTheme();
document.documentElement.lang = S.lang;
$("mark").replaceChildren(icon("lock"));
const pages = { login: loginPage };
(pages[document.body.dataset.page] || loginPage)();
