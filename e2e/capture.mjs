// 웹앱의 모든 탭을 화면 크기(데스크톱·모바일) × 테마(라이트·다크)별로 캡처한다.
//
//   node capture.mjs [출력 폴더]
//
// 환경 변수
//   BASE_URL   웹앱 주소 (기본 http://localhost:8080)
//   SETTLE_MS  네트워크가 잠잠해진 뒤 추가로 기다리는 시간 (기본 1500)
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

async function fetchApp() {
  const res = await fetch(`${BASE_URL}/api/app`);
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
  // 보이는 탭에 Superset iframe이 있으면 차트가 나타나고 로딩 표시가 사라질 때까지
  const hasEmbed = await page.locator(".view.active iframe").count();
  if (hasEmbed) {
    await page.waitForTimeout(500);
    for (const frame of page.frames()) {
      if (frame === page.mainFrame() || !frame.url().includes("/embedded/")) continue;
      await frame
        .waitForFunction(
          () =>
            document.querySelector(".dashboard-component-chart-holder, .chart-container") &&
            !document.querySelector(".loading, [data-test='loading-indicator'], .ant-skeleton"),
          null,
          { timeout: 45_000, polling: 500 },
        )
        .catch(() => console.warn("  ⚠ Superset 로딩 대기 시간 초과"));
    }
  }
  await page.evaluate(() => document.fonts.ready).catch(() => {});
  await page.waitForTimeout(SETTLE_MS);
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const app = await fetchApp();
  const tabs = app.tabs.map((t) => ({ key: t.key, label: t.label }));
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
          if (src && !src.startsWith(BASE_URL)) return;
          consoleErrors.push({ viewport: vp.name, theme, text: msg.text(), source: src });
        });
        page.on("pageerror", (err) => consoleErrors.push({ viewport: vp.name, theme, text: String(err) }));

        for (const tab of tabs) {
          await page.goto(`${BASE_URL}/#${encodeURIComponent(tab.key)}`, { waitUntil: "domcontentloaded" });
          await waitSettled(page);
          const file = `${tab.key}-${vp.name}-${theme}.png`;
          await page.screenshot({ path: path.join(OUT, file), fullPage: true });
          shots.push({ tab: tab.key, label: tab.label, viewport: vp.name, theme, file });
          console.log(`✔ ${file}`);
        }
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }

  const manifest = { project: app.project, title: app.app?.title ?? "", tabs, shots, consoleErrors };
  await writeFile(path.join(OUT, "manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`캡처 ${shots.length}장, 콘솔 오류 ${consoleErrors.length}건 → ${OUT}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
