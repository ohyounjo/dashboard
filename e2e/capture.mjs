// 웹앱의 모든 탭을 화면 크기(데스크톱·모바일) × 테마(라이트·다크)별로 캡처한다.
//
//   node capture.mjs [출력 폴더]
//
// 환경 변수
//   BASE_URL   웹앱 주소 (기본 http://localhost:8080)
//   SETTLE_MS  네트워크가 잠잠해진 뒤 추가로 기다리는 시간 (기본 1500)
//   E2E_EMAIL, E2E_PASSWORD  로그인할 계정(make admin으로 만든 것). 웹앱이 로그인을 요구하면 이 계정으로 로그인해
//              탭을 찍고, 로그인 전 화면(AUTH_PAGES)도 찍는다. 로그인 기능이 없는 버전(/api/auth/login이 404)은 그냥 찍는다.
//
// 출력
//   <출력 폴더>/<탭>-<화면>-<테마>.png
//   <출력 폴더>/manifest.json  탭 목록, 캡처 파일, 콘솔 오류
import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const BASE_URL = (process.env.BASE_URL || "http://localhost:8080").replace(/\/$/, "");
const SETTLE_MS = Number(process.env.SETTLE_MS || 1500);
const OUT = path.resolve(process.argv[2] || "screenshots");

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900, isMobile: false },
  { name: "mobile", width: 390, height: 844, isMobile: true },
];
const THEMES = ["light", "dark"];

const SESSION_COOKIE = "dashkit_session";
// 로그인 전 화면. 탭과 같은 표로 비교되도록 manifest의 tabs에 넣는다(키는 탭 키와 겹치지 않게 _로 시작)
const AUTH_PAGES = [
  { key: "_login", label: "로그인 화면", path: "/login.html" },
];
// 로그인한 채로 찍는 운용 화면(캡처용 계정은 admin이다)
const ADMIN_PAGES = [
  { key: "_admin-groups", label: "운용: 그룹", path: "/admin/#groups" },
];

// 로그인해서 세션 쿠키 값을 돌려준다. 로그인 기능이 없는 버전이면 null.
async function login() {
  const email = process.env.E2E_EMAIL || "";
  const password = process.env.E2E_PASSWORD || "";
  const probe = await fetch(`${BASE_URL}/api/app`);
  if (probe.ok) return null;                       // 로그인이 필요 없는 버전
  if (probe.status !== 401) throw new Error(`/api/app ${probe.status}`);
  if (!email || !password) throw new Error("웹앱이 로그인을 요구함: E2E_EMAIL, E2E_PASSWORD가 필요");
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(`로그인 실패: HTTP ${res.status}`);
  for (const c of res.headers.getSetCookie()) {
    const [pair] = c.split(";");
    const i = pair.indexOf("=");
    if (pair.slice(0, i).trim() === SESSION_COOKIE) return pair.slice(i + 1).trim();
  }
  throw new Error("로그인 응답에 세션 쿠키가 없음");
}

async function fetchApp(cookie) {
  const res = await fetch(`${BASE_URL}/api/app`, { headers: cookie ? { Cookie: `${SESSION_COOKIE}=${cookie}` } : {} });
  if (!res.ok) throw new Error(`/api/app ${res.status}`);
  return res.json();
}

// 지금 보이는 탭의 내용이 다 그려질 때까지 기다린다.
// Superset은 요청을 계속 보내므로 networkidle은 쓰지 않는다.
async function waitSettled(page) {
  await page.waitForLoadState("load", { timeout: 30_000 }).catch(() => {});
  // 웹앱의 "불러오는 중" 표시가 사라질 때까지
  await page
    .waitForFunction(() => !document.querySelector(".view.active .sk-note"), null, { timeout: 30_000 })
    .catch(() => {});
  // 보이는 탭의 Superset iframe마다: Superset 주소로 바뀌고, 차트가 1개 이상 있고,
  // 로딩 표시가 없는 상태가 1초 동안 유지될 때까지 기다린다.
  // 다른 탭의 iframe은 숨겨진 채 남아 있으므로 page.frames()가 아니라 보이는 탭의 iframe만 본다.
  const warnings = [];
  const iframes = await page.locator(".view.active iframe").elementHandles();
  for (const [i, handle] of iframes.entries()) {
    const ok = await waitEmbedReady(handle);
    if (!ok) warnings.push(`Superset iframe ${i + 1}이 ${EMBED_TIMEOUT_MS / 1000}초 안에 다 그려지지 않음`);
  }
  await page.evaluate(() => document.fonts.ready).catch(() => {});
  await page.waitForTimeout(SETTLE_MS);
  return warnings;
}

const EMBED_TIMEOUT_MS = 60_000;

async function waitEmbedReady(handle) {
  const deadline = Date.now() + EMBED_TIMEOUT_MS;
  let readySince = 0;
  while (Date.now() < deadline) {
    const frame = await handle.contentFrame().catch(() => null);
    const ready =
      frame &&
      frame.url().includes("/embedded/") &&
      (await frame
        .evaluate(
          () =>
            document.querySelectorAll(".dashboard-component-chart-holder").length > 0 &&
            !document.querySelector(".loading, [data-test='loading-indicator'], .ant-skeleton"),
        )
        .catch(() => false));
    if (ready) {
      readySince ||= Date.now();
      if (Date.now() - readySince >= 1000) return true;
    } else {
      readySince = 0;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const cookie = await login();
  const app = await fetchApp(cookie);
  const tabs = app.tabs.map((t) => ({ key: t.key, label: t.label }));
  const pages = cookie ? AUTH_PAGES : [];
  const adminPages = cookie ? ADMIN_PAGES : [];
  const shots = [];
  const consoleErrors = [];

  const browser = await chromium.launch();
  try {
    for (const vp of VIEWPORTS) {
      for (const theme of THEMES) {
        const context = await browser.newContext({
          viewport: { width: vp.width, height: vp.height },
          deviceScaleFactor: 1,
          isMobile: vp.isMobile,
          hasTouch: vp.isMobile,
          colorScheme: theme,
          locale: "ko-KR",
          timezoneId: "Asia/Seoul",
        });
        const page = await context.newPage();
        // 웹앱에서 난 오류만 모은다. Superset iframe 안의 오류(서비스 워커 404 등)는 Superset 쪽 문제라 뺀다.
        page.on("console", (msg) => {
          if (msg.type() !== "error") return;
          const src = msg.location()?.url || "";
          if (!src.startsWith(BASE_URL)) return;
          consoleErrors.push({ viewport: vp.name, theme, text: msg.text(), source: src });
        });
        page.on("pageerror", (err) => consoleErrors.push({ viewport: vp.name, theme, text: String(err) }));

        // 로그인 전 화면은 쿠키 없이 찍는다
        for (const pg of pages) {
          await page.goto(`${BASE_URL}${pg.path}`, { waitUntil: "domcontentloaded" });
          const warnings = await waitSettled(page);
          const file = `${pg.key}-${vp.name}-${theme}.png`;
          await page.screenshot({ path: path.join(OUT, file), fullPage: true });
          shots.push({ tab: pg.key, label: pg.label, viewport: vp.name, theme, file, warnings });
          console.log(`✔ ${file}`);
        }
        if (cookie) await context.addCookies([{ name: SESSION_COOKIE, value: cookie, url: BASE_URL }]);

        for (const tab of tabs) {
          await page.goto(`${BASE_URL}/#${encodeURIComponent(tab.key)}`, { waitUntil: "domcontentloaded" });
          const warnings = await waitSettled(page);
          const file = `${tab.key}-${vp.name}-${theme}.png`;
          await page.screenshot({ path: path.join(OUT, file), fullPage: true });
          shots.push({ tab: tab.key, label: tab.label, viewport: vp.name, theme, file, warnings });
          console.log(`${warnings.length ? "⚠" : "✔"} ${file}${warnings.length ? " — " + warnings.join(", ") : ""}`);
        }
        for (const pg of adminPages) {
          await page.goto(`${BASE_URL}${pg.path}`, { waitUntil: "domcontentloaded" });
          await page.waitForFunction(() => !document.querySelector("#view .sk-note"), null, { timeout: 30_000 }).catch(() => {});
          const warnings = await waitSettled(page);
          const file = `${pg.key}-${vp.name}-${theme}.png`;
          await page.screenshot({ path: path.join(OUT, file), fullPage: true });
          shots.push({ tab: pg.key, label: pg.label, viewport: vp.name, theme, file, warnings });
          console.log(`✔ ${file}`);
        }
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }

  const manifest = {
    project: app.project, title: app.app?.title ?? "", tabs: [...tabs, ...[...pages, ...adminPages].map((p) => ({ key: p.key, label: p.label }))],
    shots, consoleErrors,
  };
  await writeFile(path.join(OUT, "manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`캡처 ${shots.length}장, 콘솔 오류 ${consoleErrors.length}건 → ${OUT}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
