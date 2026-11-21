// Superset 섹션을 가짜 SDK와 DOM으로 실행해 리사이즈가 임베드를 다시 만들지 않는지 확인한다.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../web/app.js', import.meta.url), 'utf8');
const section = source.slice(source.indexOf('function supersetSection('), source.indexOf('/* ---------------- 위젯 섹션'));

class Element {
  constructor() { this.children = []; this.attrs = {}; this.clientWidth = 356; this._classes = new Set(); this.style = {}; }
  set className(value) { this._classes = new Set(value.split(' ').filter(Boolean)); }
  get className() { return [...this._classes].join(' '); }
  get classList() { return { toggle: (name, value) => value ? this._classes.add(name) : this._classes.delete(name), contains: (name) => this._classes.has(name) }; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  querySelector(name) { return name === 'iframe' ? this.children.find(c => c.tag === 'iframe') : null; }
}

async function setup(height) {
  let callback, calls = 0, sizeCalls = 0;
  const container = new Element(), iframe = new Element(); iframe.tag = 'iframe';
  const instance = { getScrollSize: async () => { sizeCalls++; return { height: 1240 }; }, setThemeMode() {}, unmount() {} };
  const sdk = { embedDashboard: async ({ mountPoint }) => { calls++; mountPoint.append(iframe); return instance; } };
  const ctx = { tabLabel: '이슈', supersetUrl: 'http://superset', isActive: () => true, onResize(fn) { this.resize = fn; }, onShow(fn) { this.show = fn; } };
  const scope = {
    EMBED_MIN_WIDTH: 900, CONNECT_TIMEOUT_MS: 30000, SIZE_POLL_MS: 2000,
    S: { embedScrollHint: '좌우로 스크롤', embedScrollLabel: name => `${name} 스크롤` },
    h: (tag, attrs = {}) => { const el = new Element(); el.tag = tag; el.className = attrs.class || ''; return el; },
    ResizeObserver: class { constructor(fn) { callback = fn; } observe() {} },
    window: { supersetEmbeddedSdk: sdk, matchMedia: () => ({ matches: false }) },
    dashboardSkeleton: () => new Element(), embeds: new Set(), setEmbedTheme() {}, withTimeout: promise => promise,
    setInterval: () => 1, clearInterval() {}, Math,
  };
  vm.runInNewContext(section + '\nthis.render = supersetSection;', scope);
  scope.render(container, { dashboard: 'issues', embed_id: 'id', height }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  const wrap = container.children[0], mount = wrap.children[1];
  return { wrap, mount, iframe, ctx, resize: width => { wrap.clientWidth = width; callback(); }, calls: () => calls, sizeCalls: () => sizeCalls };
}

test('섹션 폭에 따라 접근 가능한 스크롤 영역을 전환하고 iframe을 유지한다', async () => {
  const t = await setup();
  assert.equal(t.wrap.classList.contains('is-narrow'), true);
  assert.equal(t.mount.attrs.role, 'region');
  assert.equal(t.mount.attrs.tabindex, '0');
  assert.equal(t.mount.attrs['aria-label'], '이슈 스크롤');
  assert.equal(t.iframe.style.height, '1240px');
  t.resize(1000);
  assert.equal(t.wrap.classList.contains('is-narrow'), false);
  assert.equal(t.mount.attrs.tabindex, undefined);
  t.resize(356);
  assert.equal(t.mount.attrs.tabindex, '0');
  assert.equal(t.calls(), 1);
  assert.equal(t.mount.querySelector('iframe'), t.iframe);
});

test('고정 높이를 유지하며 숨겨진 섹션의 0px 측정은 상태를 바꾸지 않는다', async () => {
  const t = await setup(500);
  assert.equal(t.iframe.style.height, '500px');
  t.resize(0);
  assert.equal(t.wrap.classList.contains('is-narrow'), true);
  t.ctx.resize();
  assert.equal(t.sizeCalls(), 1);
  assert.equal(t.calls(), 1);
});
