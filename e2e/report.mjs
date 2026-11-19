// 변경 전(base)·변경 후(head) 캡처를 비교하는 PR 댓글(markdown)을 만든다.
//
//   node report.mjs <캡처 폴더> <이미지 URL 접두사> <출력 파일>
//
// <캡처 폴더>/base, <캡처 폴더>/head 아래에 capture.mjs가 만든 png와 manifest.json이 있어야 한다.
// 한쪽 캡처가 실패해 폴더가 없으면 그쪽은 "캡처 실패"로 표시한다.
// 이미지 URL은 <이미지 URL 접두사>/<base|head>/<파일>?raw=true 로 만든다.
import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";

export const MARKER = "<!-- ui-screenshots -->";

const [shotsDir, urlPrefix, outFile] = process.argv.slice(2);
if (!shotsDir || !urlPrefix || !outFile) {
  console.error("사용법: node report.mjs <캡처 폴더> <이미지 URL 접두사> <출력 파일>");
  process.exit(2);
}

const VIEWPORT_LABEL = { desktop: "데스크톱", mobile: "모바일" };
const THEME_LABEL = { light: "라이트", dark: "다크" };

async function loadSide(side) {
  try {
    const dir = path.join(shotsDir, side);
    return { dir, manifest: JSON.parse(await readFile(path.join(dir, "manifest.json"), "utf8")) };
  } catch {
    return null;
  }
}

async function hashOf(dir, file) {
  try {
    return createHash("sha256").update(await readFile(path.join(dir, file))).digest("hex");
  } catch {
    return null;
  }
}

const img = (side, file, width) =>
  `<img src="${urlPrefix}/${side}/${encodeURIComponent(file)}?raw=true" width="${width}">`;

const base = await loadSide("base");
const head = await loadSide("head");
const env = process.env;

const lines = [MARKER, "## 🖼️ 화면 캡처 (자동)", ""];
const sha = (env.HEAD_SHA || "").slice(0, 7);
const runUrl = env.RUN_URL ? ` · [실행 기록](${env.RUN_URL})` : "";
lines.push(`변경 전: \`${env.BASE_REF || "base"}\` · 변경 후: \`${sha || "head"}\`${runUrl}`, "");

if (!head) {
  lines.push("> ⚠️ 변경 후 화면을 캡처하지 못했습니다. 실행 기록의 로그를 확인해 주세요.");
} else {
  if (!base) lines.push("> ⚠️ 변경 전 화면은 캡처하지 못했습니다. 변경 후 화면만 표시합니다.", "");

  const errors = head.manifest.consoleErrors || [];
  lines.push(
    errors.length
      ? `**콘솔 오류 ${errors.length}건** (웹앱에서 발생, Superset iframe 제외)`
      : "**콘솔 오류 없음** (웹앱 기준, Superset iframe 제외)",
  );
  if (errors.length) {
    const uniq = [...new Set(errors.map((e) => e.text))].slice(0, 10);
    lines.push("", "```", ...uniq, "```");
  }
  lines.push("");

  const baseFiles = new Set((base?.manifest.shots || []).map((s) => s.file));
  let changedTotal = 0;
  const sections = [];

  for (const tab of head.manifest.tabs) {
    const shots = head.manifest.shots.filter((s) => s.tab === tab.key);
    const rows = [];
    let changed = 0;
    for (const s of shots) {
      const width = s.viewport === "mobile" ? 220 : 420;
      const label = `${VIEWPORT_LABEL[s.viewport] || s.viewport} · ${THEME_LABEL[s.theme] || s.theme}`;
      let before = "—";
      let state = "🆕 새 화면";
      if (base && baseFiles.has(s.file)) {
        const same = (await hashOf(base.dir, s.file)) === (await hashOf(head.dir, s.file));
        state = same ? "같음" : "🔸 달라짐";
        before = img("base", s.file, width);
      } else if (!base) {
        state = "";
      }
      if (state && state !== "같음") changed++;
      rows.push(`| ${label}${state ? `<br>${state}` : ""} | ${before} | ${img("head", s.file, width)} |`);
    }
    changedTotal += changed;
    const summary = base ? `${tab.label} — 달라진 화면 ${changed}/${shots.length}` : tab.label;
    sections.push(
      `<details${changed || !base ? " open" : ""}>`,
      `<summary><b>${summary}</b></summary>`,
      "",
      "| 화면 | 변경 전 | 변경 후 |",
      "| --- | --- | --- |",
      ...rows,
      "",
      "</details>",
      "",
    );
  }

  if (base) {
    lines.push(
      changedTotal
        ? `달라진 화면이 **${changedTotal}개** 있습니다. 달라진 탭은 펼쳐 두었습니다.`
        : "변경 전과 픽셀 단위로 같은 화면만 있습니다. (UI 변경 없음으로 보입니다)",
      "",
    );
  }
  const removed = [...baseFiles].filter((f) => !head.manifest.shots.some((s) => s.file === f));
  if (removed.length) lines.push(`없어진 화면: ${removed.map((f) => `\`${f}\``).join(", ")}`, "");
  lines.push(...sections);
  lines.push(
    "<sub>`e2e/capture.mjs`가 Playwright로 모든 탭을 데스크톱(1440×900)·모바일(390×844) × 라이트·다크로 캡처합니다. " +
      "'달라짐'은 파일이 바이트 단위로 다르다는 뜻이라, 차트 애니메이션 등으로 실제 변화 없이 표시될 수도 있습니다.</sub>",
  );
}

await writeFile(outFile, lines.join("\n") + "\n");
console.log(`댓글 작성: ${outFile}`);
