// 데이터 쪽 화면(데이터, 데이터셋, 설정 이력)이 함께 쓰는 조각: 오류 글, 안내 상자, 버튼, 작업 진행 표시와 작업 돌리기.
// 문구는 strings.js의 S.admin.data에만 둔다.
import { h } from "../common.js";
import * as I from "../shared/ingest.js";
import { T, ApiFail, api, waitJob, errorText } from "./kit.js";

const D = T.data;

/** 관리 서비스 오류 → 글(데이터 쪽 코드 먼저, 없으면 공통 코드, 없으면 서버 글) */
export function dataFailText(err) {
  if (err instanceof ApiFail) {
    if (err.detail && typeof D.errors[err.detail] === "string") return D.errors[err.detail];
    if (err.detail && typeof T.errors[err.detail] === "string") return T.errors[err.detail];
    if (err.body && typeof err.body.message === "string" && err.body.message) return err.body.message;
  }
  return errorText(err);
}

export const pill = (text, tone) => h("span", { class: "pill tone", "data-tone": tone, text });

/** 안내 상자: 제목 한 줄 + (줄 목록) + (버튼들). tone: good | warn | bad | primary | neutral */
export function box(tone, title, { lines, more, actions, body } = {}) {
  const shown = (lines || []).slice(0, 50);
  return h("div", { class: "a-msg d-box", "data-tone": tone, role: tone === "bad" ? "alert" : "status" },
    h("strong", { text: title }),
    body || null,
    shown.length ? h("ul", { class: "d-lines" }, shown.map((l) => h("li", { text: l }))) : null,
    more ? h("p", { class: "d-more", text: more }) : null,
    actions && actions.filter(Boolean).length ? h("div", { class: "a-actions" }, actions.filter(Boolean)) : null);
}

export function btn(label, onclick, cls = "") {
  return h("button", { type: "button", class: `btn ${cls}`.trim(), onclick }, label);
}

/** 진행 중인 작업 한 줄(GET /jobs/{id}의 progress: {step, table?, rows?, bytes?, total_bytes?}). */
export function jobText(job) {
  const p = (job && job.progress) || {};
  const parts = [D.jobStep[p.step] || D.jobRunning];
  if (p.table) parts.push(p.table);
  if (p.rows != null) parts.push(D.rowsRead(p.rows));
  if (p.bytes != null && p.total_bytes) parts.push(D.bytesRead(D.size(p.bytes), D.size(p.total_bytes)));
  return parts.join(" · ");
}

/** 작업 진행 표시: 글 한 줄 + (전체 크기를 알면) 진행률 막대 + (파일 작업이면) 화면을 떠나도 계속된다는 안내 */
export function jobBox(job, { keepsRunning = false } = {}) {
  const ratio = I.progressRatio(job && job.progress);
  return h("div", { class: "d-progress", role: "status" },
    h("span", { text: jobText(job) }),
    ratio === null ? null : h("progress", { max: "1000", value: String(Math.round(ratio * 1000)), "aria-label": D.progressLabel(Math.round(ratio * 100)) }),
    keepsRunning ? h("small", { class: "a-key", text: D.keepsRunning }) : null);
}

/** 202 작업을 돌려주는 요청을 보내고 끝날 때까지 본다(구조 변경·새 테이블·합치기·설정 되돌리기).
 *  send(): 요청 Promise. out: 진행 표시를 그릴 곳. 돌려주는 것: 작업 결과(화면을 떠났으면 null). 실패는 ApiFail(작업의 error) */
export async function runJob(send, out, alive) {
  out.replaceChildren(h("div", { class: "sk-note", role: "status", text: D.jobRunning }));
  const r = await send();
  if (!r || !r.job) return r;
  if (alive()) out.replaceChildren(jobBox(r.job, { keepsRunning: true }));
  return waitJob(r.job.id, { alive, onTick: (job) => { if (alive()) out.replaceChildren(jobBox(job, { keepsRunning: true })); } });
}

/** 지금 설정 판(revision). 구조 변경 요청이 함께 보낸다. table을 주면 GET /tables/{t}, 아니면 GET /history(admin) */
/** 설정 판(revision). 폼을 **열 때** 읽어 두고 보낼 때 그 값을 쓴다(보내기 직전에 읽으면 충돌 검사를 지나친다) */
export async function currentRevision(table) {
  const r = table ? await api("GET", `/api/manage/tables/${encodeURIComponent(table)}`) : await api("GET", "/api/manage/history");
  return r.revision;
}
