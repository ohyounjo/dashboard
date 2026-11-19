// Studio API 호출은 모두 이 파일에 있다(계약: contracts/studio.md).
// 화면 모듈은 fetch를 직접 쓰지 않는다. 실제 API와 어긋나면 여기만 고친다.
//
// 계약에 기대는 점(바뀌면 여기를 고친다):
//  - 오류 응답은 JSON `{detail, message, errors?}`다. JSON이 아니면(프록시 오류 등) HTTP 상태만 보여 준다.
//  - `PATCH …/config`에는 GET이 준 `revision`을 함께 보낸다. 그 사이 파일이 바뀌었으면 409 `stale_config`다.
//  - `tab.move`의 `index`, `section.move`의 `to_index`는 옮긴 뒤의 자리다. `section.update`는 최상위 키만 바꾸는 얕은 병합이다.
//  - apply 스트림: `event: log`의 data는 로그 한 줄(글자 그대로), `event: done`의 data는 JSON `{ok, step, errors, project}`.
//    스트림이 done 없이 끊기면 `GET /studio/api/apply`로 결과를 다시 확인한다(작업은 서버에서 끝까지 간다).
//  - `…/data/{table}/check`는 검증에 실패해도 200 `{valid: false, errors, diff: null}`이다.
//  - registry의 위젯 `options`에는 `$ref: "#/$defs/…"`가 남아 있고 정의는 registry의 `defs`에 있다(schema-form.js가 푼다).
// 계약에 없어 화면이 가정한 것:
//  - 추론 결과의 `problems` 한 줄은 `<원본 파일 이름>: …` 꼴이다(view-wizard.js가 그 테이블의 문제가 풀렸는지 볼 때 쓴다).
//  - `web_url`이 빈 문자열이면 미리보기를 띄우지 않는다.

const BASE = "/studio/api";

export class ApiError extends Error {
  /** @param {{status?: number, detail?: string, message?: string, errors?: string[], network?: boolean}} info */
  constructor(info) {
    super(info.message || info.detail || "error");
    this.status = info.status || 0;
    this.detail = info.detail || null;
    this.errors = Array.isArray(info.errors) ? info.errors.map(String) : [];
    this.network = !!info.network;
  }
}

async function failure(res) {
  let body = null;
  try { body = await res.json(); } catch (e) { /* JSON이 아닌 오류 응답 */ }
  const detail = body && typeof body.detail === "string" ? body.detail : null;
  const message = body && typeof body.message === "string" ? body.message : null;
  return new ApiError({ status: res.status, detail, message, errors: body && body.errors });
}

async function send(method, path, { json, form } = {}) {
  const init = { method, cache: "no-store", headers: { Accept: "application/json" } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    // multipart 경계는 브라우저가 붙인다
    init.body = form;
  }
  let res;
  try { res = await fetch(BASE + path, init); } catch (e) { throw new ApiError({ network: true }); }
  if (!res.ok) throw await failure(res);
  return res;
}

async function call(method, path, body) {
  const res = await send(method, path, body);
  try { return await res.json(); } catch (e) { throw new ApiError({ status: res.status, detail: "bad_response" }); }
}

const enc = encodeURIComponent;
const project = (p) => `/projects/${enc(p)}`;

function oneFile(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  return form;
}

/** `event:`/`data:` 블록 하나를 {event, data}로 */
function parseEvent(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  return { event, data: data.join("\n") };
}

export const api = {
  projects: () => call("GET", "/projects"),
  registry: () => call("GET", "/registry"),
  changes: () => call("GET", "/changes"),

  /** CSV 여러 개 → {inference, upload_id} */
  infer(files) {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return call("POST", "/projects/infer", { form });
  },
  /** {name, upload_id, inference, resuggest?} → {name} */
  createProject: (body) => call("POST", "/projects", { json: body }),

  config: (p) => call("GET", `${project(p)}/config`),
  /** ops: 변경 연산 목록 → {changed, dashboard, schema, revision}. revision이 있으면 함께 보낸다(409 stale_config) */
  patchConfig: (p, ops, revision) => call("PATCH", `${project(p)}/config`, { json: revision ? { ops, revision } : { ops } }),
  /** 적용 상태: {running, project, last: {ok, step, errors, project, log} | null} */
  applyStatus: () => call("GET", "/apply"),
  plan: (p) => call("GET", `${project(p)}/plan`),

  /** CSV 하나 검증(파일을 쓰지 않는다) → {valid, errors, diff, diff_error?} */
  checkData: (p, table, file) => call("POST", `${project(p)}/data/${enc(table)}/check`, { form: oneFile(file) }),
  putData: (p, table, file) => call("PUT", `${project(p)}/data/${enc(table)}`, { form: oneFile(file) }),

  /**
   * check → load → sync. 로그를 한 줄씩 onLog로 넘기고 마지막 done({ok, step, errors})을 돌려준다.
   * 실행 중이면 ApiError(status 409, detail apply_running).
   */
  async apply(p, onLog) {
    const res = await send("POST", `${project(p)}/apply`, { json: {} });
    if (!res.body) throw new ApiError({ status: res.status, detail: "bad_response" });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", done = null;
    const flush = (block) => {
      if (!block.trim()) return;
      const ev = parseEvent(block);
      if (ev.event === "log") onLog(ev.data);
      else if (ev.event === "done") {
        try { done = JSON.parse(ev.data); } catch (e) { done = { ok: false, step: null, errors: [ev.data] }; }
      }
    };
    try {
      for (;;) {
        const { value, done: end } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) { flush(buf.slice(0, cut)); buf = buf.slice(cut + 2); }
        if (end) break;
      }
    } catch (e) {
      throw new ApiError({ network: true });
    }
    flush(buf);
    if (!done) throw new ApiError({ detail: "stream_ended" });
    return { ok: !!done.ok, step: done.step || null, errors: Array.isArray(done.errors) ? done.errors.map(String) : [] };
  },
};
