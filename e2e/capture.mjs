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
  { key: "_signup", label: "가입 화면", path: "/signup.html" },
  { key: "_forgot", label: "비밀번호 찾기 화면", path: "/forgot.html" },
];
// 로그인한 채로 찍는 운용 화면·내 정보(캡처용 계정은 admin이다)
const ADMIN_PAGES = [
  { key: "_admin-users", label: "운용: 사용자", path: "/admin/#users" },
  { key: "_admin-groups", label: "운용: 그룹", path: "/admin/#groups" },
  { key: "_admin-stats", label: "운용: 통계", path: "/admin/#stats" },
  { key: "_admin-audit", label: "운용: 감사 로그", path: "/admin/#audit" },
  { key: "_admin-outbox", label: "운용: 테스트 메일함", path: "/admin/#outbox" },
  // 데이터 메뉴: 관리 서비스가 떠 있는 버전에만 있다(메뉴가 없으면 건너뛴다). 상세는 목록의 첫 테이블을 눌러 들어가고
  // 탭은 차례(0: 구조, 1: 배치)로 고른다. 프로젝트마다 테이블 이름이 다르므로 이름을 쓰지 않는다
  { key: "_admin-data", label: "운용: 데이터 목록", path: "/admin/#tables", menu: "tables" },
  { key: "_admin-data-columns", label: "운용: 테이블 구조", path: "/admin/#tables", menu: "tables", detailTab: 0 },
  { key: "_admin-data-batches", label: "운용: 테이블 배치", path: "/admin/#tables", menu: "tables", detailTab: 1 },
  // 여러 파일 올리기(빈 화면): 그 버튼이 없는 버전(2a 이전)이면 건너뛴다
  { key: "_admin-data-bulk", label: "운용: 여러 파일 올리기", path: "/admin/#tables", menu: "tables", open: "[data-action=bulk]" },
  { key: "_account", label: "내 정보", path: "/account.html" },
];

// 운용 화면의 데이터 상세로 들어간다: 목록의 첫 행 링크 → 탭 고르기 → 탭 내용이 다 그려질 때까지
async function openDetail(page, tabIndex) {
  const link = page.locator("#view .a-table tbody tr a.a-link").first();
  await link.waitFor({ timeout: 30_000 });
  await link.click();
  await page.waitForSelector("#view [role=tablist] [role=tab]", { timeout: 30_000 });
  await page.locator("#view [role=tablist] [role=tab]").nth(tabIndex).click();
  await page.waitForFunction(() => {
    const panel = document.querySelector("#view [role=tabpanel]");
    return panel && !panel.querySelector(".sk-note") && panel.querySelector(".a-table, .a-empty");
  }, null, { timeout: 30_000 }).catch(() => {});
}

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
          if (pg.menu) {
            // 그 메뉴가 없는 버전(관리 서비스가 없는 옛 버전 등)이면 이 장면은 찍지 않는다
            await page.waitForSelector("#nav a", { timeout: 30_000 }).catch(() => {});
            const hasMenu = await page.locator(`#nav a[data-key="${pg.menu}"]`).count().catch(() => 0);
            if (!hasMenu) { console.log(`- ${pg.key}: 메뉴 없음, 건너뜀`); continue; }
            await page.waitForSelector("#view .a-table tbody tr, #view .a-empty, #view .a-msg", { timeout: 30_000 }).catch(() => {});
            if (pg.detailTab !== undefined) await openDetail(page, pg.detailTab);
            if (pg.open) {
              const opener = page.locator(`#view ${pg.open}`);
              if (!(await opener.count())) { console.log(`- ${pg.key}: 화면 없음, 건너뜀`); continue; }
              await opener.click();
              await page.waitForSelector("#view .d-drop", { timeout: 30_000 }).catch(() => {});
            }
          }
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
    project: app.project, title: app.app?.title ?? "",
    // 찍지 않은(건너뛴) 운용 장면은 탭 목록에서도 뺀다
    tabs: [...tabs, ...[...pages, ...adminPages].filter((p) => shots.some((s) => s.tab === p.key)).map((p) => ({ key: p.key, label: p.label }))],
    shots, consoleErrors,
  };
  await writeFile(path.join(OUT, "manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`캡처 ${shots.length}장, 콘솔 오류 ${consoleErrors.length}건 → ${OUT}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
