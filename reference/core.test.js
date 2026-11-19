// 실행: node --test reference/core.test.js
// 기존 Artifact 버전(reference/index.html)의 검증이다. 새 템플릿의 테스트는 tests/에 따로 만든다.
// 여기의 기대값(열린 이슈 16, Critical 미해결 3, 가중 진척률 55, D-29, 최근 문서 5건)은 새 테스트의 기준값이다.
// 외부 의존성 없이 Node 내장 node:test, node:assert, node:vm만 사용한다.
// index.html의 <script id="core"> 블록(데이터와 순수 함수)을 꺼내 검증한다. UI는 사용자 확인 범위라 제외한다.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const block = id => {
  const m = html.match(new RegExp(`<script id="${id}">([\\s\\S]*?)</script>`));
  assert.ok(m, `<script id="${id}"> 블록이 있어야 함`);
  return m[1];
};
const RawCore = vm.runInNewContext(block("core") + "\n;Core");
// vm 컨텍스트의 객체는 프로토타입이 달라 deepStrictEqual이 실패하므로, 반환값을 현재 컨텍스트 값으로 복사한다.
const plain = v => (v && typeof v === "object") ? JSON.parse(JSON.stringify(v)) : v;
const Core = Object.fromEntries(Object.entries(RawCore).map(([k, v]) =>
  [k, typeof v === "function" ? (...a) => plain(v(...a)) : plain(v)]));
const D = Core.DATA;

test("문법: core와 ui 스크립트가 파싱된다", () => {
  assert.doesNotThrow(() => new vm.Script(block("core")));
  assert.doesNotThrow(() => new vm.Script(block("ui")));
});

test("데이터 무결성: validateData 오류 0건", () => {
  assert.deepEqual(Core.validateData(D), []);
});

test("데이터 규모가 계획서와 일치", () => {
  assert.equal(D.members.length, 6);
  assert.equal(D.milestones.length, 5);
  assert.equal(D.tasks.length, 16);
  assert.equal(D.issues.length, 24);
  assert.equal(D.docs.length, 8);
  assert.equal(D.kpiHistory.length, 6);
});

test("validateData가 깨진 데이터를 잡아낸다", () => {
  const bad = JSON.parse(JSON.stringify(D));
  bad.issues[0].milestoneId = "ms99";
  bad.issues[1].assignee = "m99";
  bad.milestones[2].start = "2026-12-30";
  bad.docs[0].version = "2.1";
  bad.issues[2].created = "2026-11-30";
  bad.tasks.push({ ...bad.tasks[0] });
  const errs = Core.validateData(bad);
  for (const s of ["ISS-101: 마일스톤 없음", "ISS-102: 담당자 없음", "ms3: start > end", "D1: 버전 형식 오류", "ISS-103: created > updated", "tasks: 중복 ID"]) {
    assert.ok(errs.includes(s), `오류 목록에 "${s}" 포함`);
  }
});

test("KPI 계산", () => {
  assert.equal(Core.countOpen(D.issues), 16);
  assert.equal(Core.countCriticalOpen(D.issues), 3);
  assert.equal(Core.overallProgress(D.milestones), 55);
  const nx = Core.nextMilestone(D.milestones, "2026-11-19");
  assert.equal(nx.milestone.id, "ms3"); // ms2는 종료일이 지나 제외
  assert.equal(nx.days, 29);
  assert.equal(Core.nextMilestone(D.milestones, "2027-03-01"), null);
});

test("KPI 샘플 입력", () => {
  const ms = [{ start: "2026-01-01", end: "2026-01-10", progress: 100 }, { start: "2026-01-11", end: "2026-01-30", progress: 40 }];
  assert.equal(Core.overallProgress(ms), 60); // (10*100 + 20*40) / 30
  assert.equal(Core.overallProgress([]), 0);
});

test("증감 표시: 방향과 좋고 나쁨", () => {
  assert.deepEqual(Core.delta(16, 14, false), { diff: 2, tone: "bad", icon: "📈", text: "▲ +2" });
  assert.deepEqual(Core.delta(55, 51, true), { diff: 4, tone: "good", icon: "📈", text: "▲ +4" });
  assert.deepEqual(Core.delta(2, 3, false), { diff: -1, tone: "good", icon: "📉", text: "▼ -1" });
  assert.equal(Core.delta(5, 5, true).tone, "flat");
});

test("이슈 필터: 조합, 검색, 빈 조건", () => {
  assert.equal(Core.filterIssues(D.issues, {}).length, 24);
  assert.equal(Core.filterIssues(D.issues, { severity: ["critical"] }).length, 4);
  assert.equal(Core.filterIssues(D.issues, { severity: ["critical"], status: ["open", "in-progress"] }).length, 3);
  const r = Core.filterIssues(D.issues, { assignee: ["m3"], status: ["open"] });
  assert.deepEqual(r.map(i => i.id).sort(), ["ISS-103", "ISS-115", "ISS-123"]);
  assert.deepEqual(Core.filterIssues(D.issues, { q: "루틴" }).map(i => i.id), ["ISS-103", "ISS-110", "ISS-115", "ISS-120"]);
  assert.equal(Core.filterIssues(D.issues, { q: "iss-124" }).length, 1);
  assert.equal(Core.filterIssues(D.issues, { q: "없는 단어" }).length, 0);
});

test("이슈 정렬: 심각도 순서와 방향", () => {
  const asc = Core.sortIssues(D.issues, "severity", "asc", D.members);
  assert.equal(asc[0].severity, "critical");
  assert.equal(asc.at(-1).severity, "trivial");
  const desc = Core.sortIssues(D.issues, "updated", "desc", D.members);
  assert.equal(desc[0].updated, "2026-11-19");
  assert.equal(desc.at(-1).updated, "2026-09-03");
  const orig = D.issues.map(i => i.id).join();
  Core.sortIssues(D.issues, "title", "asc", D.members);
  assert.equal(D.issues.map(i => i.id).join(), orig, "원본 배열을 바꾸지 않음");
});

test("문서 최근 7일 판정", () => {
  const recent = D.docs.filter(d => Core.isRecent(d.updated, D.project.today)).map(d => d.id);
  assert.deepEqual(recent, ["D2", "D3", "D5", "D6", "D7"]);
  assert.equal(Core.isRecent("2026-11-20", "2026-11-19"), false);
});

test("타임라인 위치 계산", () => {
  assert.equal(Core.timelinePct("2026-08-03", "2026-08-03", "2027-02-26"), 0);
  assert.equal(Core.timelinePct("2027-02-26", "2026-08-03", "2027-02-26"), 100);
  assert.equal(Core.timelinePct("2030-01-01", "2026-08-03", "2027-02-26"), 100);
  const p = Core.timelinePct("2026-11-19", "2026-08-03", "2027-02-26");
  assert.ok(p > 50 && p < 60, `오늘 위치 ${p}`);
});

test("규약: 외부 스크립트를 쓰지 않고, 폰트만 Google Fonts에서 불러온다", () => {
  assert.equal((html.match(/<script[^>]+src=/g) || []).length, 0);
  const hrefs = [...html.matchAll(/href="(https?:[^"]+)"/g)].map(m => m[1]);
  assert.ok(hrefs.every(h => /^https:\/\/fonts\.(googleapis|gstatic)\.com/.test(h)), hrefs.join());
});

test("규약: 페이지 안에 데이터 수정 경로가 없다(보기 전용)", () => {
  const ui = block("ui");
  assert.ok(!/fetch\(|XMLHttpRequest|window\.claude/.test(ui), "외부 통신/런타임 기능 미사용");
  assert.ok(!/draggable="true"|dragstart|contenteditable/.test(html), "드래그·편집 불가");
  assert.ok(!/D\.(issues|tasks|docs|milestones)\.(push|splice|pop|shift)|\b[timd]\.(status|column|progress|severity|assignee)\s*=(?!=)/.test(ui), "UI가 DATA를 변경하지 않음");
});

test("규약: 다크모드 토큰 3단 구조와 reduced-motion 처리", () => {
  assert.ok(html.includes("--primary: #1428A0"));
  assert.ok(html.includes(':root:not([data-theme="light"])'));
  assert.ok(html.includes(':root[data-theme="dark"]'));
  assert.ok(html.includes("prefers-reduced-motion: reduce"));
});
