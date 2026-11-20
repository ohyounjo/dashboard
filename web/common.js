// 웹앱 셸(app.js)과 로그인 전 화면(auth.js)이 함께 쓰는 도우미. 문구는 strings.js에 둔다.

const THEME_KEY = "dashboard-theme";             // localStorage 키(app.js와 같은 값)

/** 요소 생성. 문자열 자식은 텍스트 노드로 넣는다(innerHTML 없음). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 웹앱에서 고른 테마(라이트·다크)를 로그인 전 화면에도 적용한다. */
export function applySavedTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "dark" || saved === "light") document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* 저장소 사용 불가 */ }
}

/** 같은 출처의 경로만 돌아갈 곳으로 받는다(다른 사이트로 보내는 열린 리디렉션 방지).
 *  문자열 접두사가 아니라 브라우저와 같은 URL 파서로 읽은 결과의 출처를 본다(탭·줄바꿈·역슬래시를 섞은 `/\t/evil` 같은 우회).
 *  돌려주는 값은 검증한 URL의 경로·쿼리·해시뿐이다. origin은 테스트용(기본: 지금 출처). */
export function safeNext(raw, origin = location.origin) {
  if (typeof raw !== "string" || !raw) return "/";
  let url;
  try { url = new URL(raw, origin); } catch (e) { return "/"; }
  if (url.origin !== origin || (url.protocol !== "http:" && url.protocol !== "https:")) return "/";
  const out = url.pathname + url.search + url.hash;
  if (!out.startsWith("/") || out.startsWith("//")) return "/";
  if (/^\/(login|signup|forgot|reset|verify)\.html$/.test(url.pathname)) return "/";
  return out;
}

/** 로그인 화면 주소. next는 로그인한 뒤 돌아올 경로. */
export function loginUrl(next, reason) {
  const q = new URLSearchParams();
  const n = safeNext(next, location.origin);
  if (n !== "/") q.set("next", n);
  if (reason) q.set("reason", reason);
  const qs = q.toString();
  return "/login.html" + (qs ? "?" + qs : "");
}

/** 상태를 바꾸는 요청(JSON). backend는 application/json만 받는다(요청 보호). */
export function postJson(path, body) {
  return fetch(path, {
    method: "POST", cache: "no-store", credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/** 오류 응답의 detail 코드(없으면 null). */
export async function readDetail(res) {
  try { const j = await res.json(); return j && typeof j.detail === "string" ? j.detail : null; } catch (e) { return null; }
}
