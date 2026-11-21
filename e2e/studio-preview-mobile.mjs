// 격리된 테스트 스택에서: STUDIO_BROWSER_URL=http://localhost:<포트> node --test e2e/studio-preview-mobile.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";

const base = process.env.STUDIO_BROWSER_URL;
if (!base) throw new Error("STUDIO_BROWSER_URL에 격리된 Studio 주소를 지정하세요");

for (const colorScheme of ["light", "dark"]) {
  test(`390px ${colorScheme}: 미리보기 펼치기·접기·선택 유지`, async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 390, height: 800 }, colorScheme });
    try {
      const page = await context.newPage();
      await page.goto(base);
      const toggle = page.locator(".preview-toggle");
      const preview = page.locator("#preview");
      await toggle.waitFor({ state: "visible" });
      await page.waitForFunction(() => !document.querySelector(".preview-toggle")?.disabled);
      assert.equal(await preview.isHidden(), true);
      await toggle.click();
      await preview.locator("iframe").waitFor({ state: "visible" });
      assert.equal(await toggle.getAttribute("aria-pressed"), "true");
      assert.ok((await preview.boundingBox()).height >= 420);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.reload();
      await preview.locator("iframe").waitFor({ state: "visible" });
      await toggle.click();
      assert.equal(await preview.isHidden(), true);
      await page.reload();
      await toggle.waitFor({ state: "visible" });
      await page.waitForFunction(() => !document.querySelector(".preview-toggle")?.disabled && document.querySelector("#preview")?.hidden);
      assert.equal(await preview.isHidden(), true);
    } finally {
      await context.close();
      await browser.close();
    }
  });
}
