// chart-gallery를 적용한 격리 스택에서 실행: BASE_URL=... E2E_EMAIL=... E2E_PASSWORD=... node e2e/embed-height.mjs
// 실제 Superset guest iframe과 6.1.0 CSS 적용을 검증한다. 비밀값은 환경 변수로만 받는다.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const base = (process.env.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
const { E2E_EMAIL: email, E2E_PASSWORD: password } = process.env;
if (!email || !password) throw new Error('E2E_EMAIL과 E2E_PASSWORD가 필요합니다');
const browser = await chromium.launch();
try {
  for (const { width, fixed } of [{ width: 390 }, { width: 1280 }, { width: 390, fixed: 500 }]) {
    const page = await browser.newPage({ viewport: { width, height: 850 } });
    try {
      if (fixed) await page.route('**/api/app', async route => {
        const response = await route.fetch();
        const data = await response.json();
        for (const tab of data.tabs) for (const section of tab.sections) {
          if (section.type === 'superset') section.height = fixed;
        }
        await route.fulfill({ response, json: data });
      });
      await page.goto(`${base}/login.html`);
      await page.locator('#email').fill(email);
      await page.locator('#password').fill(password);
      await page.locator('button[type=submit]').click();
      await page.getByText('배치·필터 묶음', { exact: true }).click();
      const frame = page.frameLocator('iframe').last();
      const iframe = page.locator('iframe').last();
      const expand = frame.locator('[data-test="filter-bar__collapsable"]');
      await frame.locator('[data-test="filter-bar"]').waitFor({ state: 'attached', timeout: 30_000 });
      if (await expand.isVisible()) await expand.click();
      const input = frame.locator('[data-test="range-filter-from-input"]');
      await input.waitFor({ state: 'visible', timeout: 30_000 });
      await page.waitForTimeout(4000);
      const heights = [];
      for (let i = 0; i < 6; i++) {
        heights.push(await iframe.evaluate(el => el.clientHeight));
        await page.waitForTimeout(2100);
      }
      assert.ok(Math.max(...heights) - Math.min(...heights) <= 2, `iframe 높이가 계속 변함: ${heights}`);
      if (fixed) assert.equal(heights[0], fixed);
      await input.fill('10');
      await input.press('Tab');
      await frame.getByText('Apply filters', { exact: true }).click();
      await frame.getByText('2,821', { exact: true }).waitFor({ timeout: 30_000 });
      await frame.getByText('과일·판매 경로별', { exact: true }).click();
      await frame.getByText('판매 경로별 무게', { exact: true }).waitFor();
      await page.waitForTimeout(4000);
      const tall = await iframe.evaluate(el => el.clientHeight);
      await frame.getByText('농원별', { exact: true }).click();
      await page.waitForTimeout(5000);
      const short = await iframe.evaluate(el => el.clientHeight);
      if (fixed) assert.equal(short, fixed);
      else assert.ok(short < tall - 20, `탭 복귀 후 높이가 줄지 않음: ${tall} → ${short}`);
      if (!fixed) {
        await frame.locator('[data-test="filter-bar__collapse-button"]').click();
        await frame.getByText('과일·판매 경로별', { exact: true }).click();
        await page.waitForTimeout(4000);
        const collapsedTall = await iframe.evaluate(el => el.clientHeight);
        await frame.getByText('농원별', { exact: true }).click();
        await page.waitForTimeout(5000);
        const collapsedShort = await iframe.evaluate(el => el.clientHeight);
        assert.ok(collapsedShort < collapsedTall - 20, `필터 접기 후 높이가 줄지 않음: ${collapsedTall} → ${collapsedShort}`);
      }
      console.log(`${width}px${fixed ? `, 고정 ${fixed}px` : ''}: 높이 ${heights.join(', ')}, 탭 ${tall}→${short}; 필터 적용 통과`);
    } finally { await page.close(); }
  }
} finally { await browser.close(); }
