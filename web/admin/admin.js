// 운용 화면(contracts/auth.md "운용 화면"). 주소의 # 뒤로 메뉴를 고른다. 문구는 strings.js(S.admin)에만 둔다.
// 권한은 API가 지킨다. user 등급이 이 주소를 열면 API가 403이고 화면은 웹앱으로 돌려보낸다.
//
// 메뉴는 menus/<이름>.js 하나씩이다. 각 모듈은 { key, label, match, when?, render(hash) }를 내보낸다.
//   key    메뉴 링크의 주소(#key)와 data-key
//   label  메뉴 이름(document.title에도 쓴다)
//   match  # 뒤(앞의 #는 뺀다)가 이 메뉴의 화면인지 고르는 정규식(상세 화면 주소도 포함)
//   when   있으면 true일 때만 메뉴에 보인다(셸이 /api/auth/config와 /api/manage/config를 읽은 뒤에 부른다)
//   render 주소가 바뀔 때마다 부른다. #view를 채운다
// 메뉴를 더하려면 파일 하나를 만들고 아래 MENUS에 한 줄을 더한다. 맞는 메뉴가 없는 주소는 첫 메뉴가 받는다.
import S from "../strings.js";
import { icon } from "../icons.js";
import { applySavedTheme, h, loginUrl, postJson, readDetail } from "../common.js";
import { T, $, view, session, ApiFail, leave, errorText, message } from "./kit.js";
import users from "./menus/users.js";
import groups from "./menus/groups.js";
import stats from "./menus/stats.js";
import tables from "./menus/tables.js";
import datasets from "./menus/datasets.js";
import history from "./menus/history.js";
import audit from "./menus/audit.js";
import outbox from "./menus/outbox.js";

const MENUS = [users, groups, tables, datasets, history, stats, audit, outbox];
const menus = () => MENUS.filter((m) => !m.when || m.when());

function route() {
  const hash = location.hash.replace(/^#/, "") || "users";
  const menu = menus().find((m) => m.match.test(hash)) || MENUS[0];
  $("nav").querySelectorAll("a").forEach((a) => {
    if (a.dataset.key === menu.key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = `${menu.label} · ${T.title}`;
  menu.render(hash);
  $("main").focus({ preventScroll: true });
}

async function boot() {
  applySavedTheme();
  document.documentElement.lang = S.lang;
  $("skip").textContent = S.skipToContent;
  $("skip").addEventListener("click", (e) => { e.preventDefault(); $("main").focus(); });
  $("title").textContent = T.title;
  $("back").replaceChildren(icon("home"), T.back);
  $("nav").setAttribute("aria-label", T.title);
  const readJson = async (path) => {
    try {
      const res = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
      return res.ok ? await res.json() : null;
    } catch (e) { return null; }
  };
  // 관리 서비스가 떠 있지 않으면(nginx 502, 404, 연결 실패) null이고 데이터 메뉴를 숨긴다
  const [authConfig, manageConfig] = await Promise.all([readJson("/api/auth/config"), readJson("/api/manage/config")]);
  if (authConfig && authConfig.mail_mode) session.mailMode = authConfig.mail_mode;
  session.manage = manageConfig && typeof manageConfig === "object" && manageConfig.limits ? manageConfig : null;
  if (session.mailMode === "test") { $("banner").textContent = S.auth.mailTestBanner; $("banner").hidden = false; }
  $("nav").replaceChildren(...menus().map((m) => h("a", { href: `#${m.key}`, "data-key": m.key, text: m.label })));
  view().replaceChildren(h("div", { class: "sk-note", text: T.loading }));
  try {
    const res = await fetch("/api/app", { cache: "no-store", headers: { Accept: "application/json" } });
    if (res.status === 401) { leave(loginUrl(location.pathname + location.hash)); return; }
    if (!res.ok) throw new ApiFail(res.status, await readDetail(res));
    session.me = (await res.json()).user;
  } catch (err) {
    if (err instanceof ApiFail && err.detail === "password_change_required") { leave("/account.html"); return; }
    view().replaceChildren(message(errorText(err)));
    return;
  }
  const me = session.me;
  if (!me || (me.role !== "admin" && me.role !== "operator")) { leave("/"); return; }
  $("who").textContent = `${me.email} · ${S.roleLabel[me.role] || me.role}`;
  const logout = $("logout");
  logout.textContent = S.logout;
  logout.hidden = false;
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    try { await postJson("/api/auth/logout", {}); } catch (e) { /* 그래도 로그인 화면으로 */ }
    leave(loginUrl("/", "logout"));
  });
  window.addEventListener("hashchange", route);
  route();
}

boot();
