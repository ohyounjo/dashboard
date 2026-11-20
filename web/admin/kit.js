// 운용 화면의 메뉴들이 함께 쓰는 도우미(네트워크, 안내 문구, 공통 조각, 정렬·페이지 나눔 표).
// 메뉴 모듈(menus/<이름>.js)과 셸(admin.js)이 import한다. 문구는 strings.js(S.admin)에만 둔다.
import S from "../strings.js";
import { h, loginUrl } from "../common.js";

export const T = S.admin;
export const $ = (id) => document.getElementById(id);
export const view = () => $("view");

/** 셸이 채우는 값. me: 로그인한 운용자 {email, role}, mailMode: 메일 모드(/api/auth/config),
 *  manage: 관리 서비스의 GET /api/manage/config({project, role, limits}). 관리 서비스가 떠 있지 않으면 null(데이터 메뉴를 숨긴다) */
export const session = { me: null, mailMode: "test", manage: null };

/* ---------------- 네트워크 ---------------- */

/** 실패한 요청. body는 오류 응답 전체({detail, message, errors, …}. 없으면 null) */
export class ApiFail extends Error {
  constructor(status, detail, body = null) { super(detail || String(status)); this.status = status; this.detail = detail; this.body = body; }
}

let leaving = false;
export function leave(url) {
  if (leaving) return;
  leaving = true;
  location.replace(url);
}

/** JSON API. 401이면 로그인 화면, 403 forbidden이면 웹앱으로 보낸다. 실패는 ApiFail. */
export async function api(method, path, body) {
  let res;
  try {
    res = method === "GET"
      ? await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } })
      : await fetch(path, {
        method, cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
  } catch (e) { throw new ApiFail(0, "network"); }
  return answer(method, res);
}

async function answer(method, res) {
  if (res.status === 204) return null;
  if (res.ok) return res.json();
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  const detail = body && typeof body.detail === "string" ? body.detail : null;
  if (res.status === 401) leave(loginUrl(location.pathname + location.hash, "expired"));
  else if (res.status === 403 && detail === "password_change_required") leave("/account.html");
  else if (res.status === 403 && detail === "forbidden" && method === "GET") leave("/");
  throw new ApiFail(res.status, detail, body && typeof body === "object" ? body : null);
}

/** multipart 본문(파일 올리기). Content-Type은 브라우저가 경계값과 함께 정한다. 응답 처리는 api()와 같다. */
export async function apiForm(path, form) {
  let res;
  try {
    res = await fetch(path, { method: "POST", cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" }, body: form });
  } catch (e) { throw new ApiFail(0, "network"); }
  return answer("POST", res);
}

/** 관리 서비스의 작업(GET /api/manage/jobs/{id})을 1초 간격으로 본다. 끝나면 결과, 실패하면 ApiFail(작업의 error).
 *  alive()가 false가 되면(화면을 떠났다) 멈추고 null을 돌려준다. 작업은 서버에서 끝까지 간다. onTick(job)은 볼 때마다 부른다. */
export async function waitJob(id, { alive = () => true, onTick } = {}) {
  for (;;) {
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
    if (!alive()) return null;
    const job = await api("GET", `/api/manage/jobs/${encodeURIComponent(id)}`);
    if (!alive()) return null;
    if (onTick) onTick(job);
    if (job.status === "succeeded") return job.result || {};
    if (job.status !== "running") {
      const err = job.error || {};
      throw new ApiFail(err.status || 500, err.detail || null, err);
    }
  }
}

export function errorText(err) {
  if (err instanceof ApiFail) {
    if (err.detail && typeof T.errors[err.detail] === "string") return T.errors[err.detail];
    if (err.status === 0) return T.errors.network;
    return T.errors.server(err.status);
  }
  return String(err && err.message || err);
}

export function message(text, tone = "bad") {
  return h("div", { class: "a-msg", "data-tone": tone, role: tone === "bad" ? "alert" : "status" }, text);
}

/** 영역 하나의 안내 문구 자리. set(text, tone)으로 바꾸고 set(null)로 지운다. */
export function notice() {
  const box = h("div");
  return { el: box, set(text, tone) { box.replaceChildren(text ? message(text, tone) : ""); } };
}

/* ---------------- 공통 조각 ---------------- */

/** 주소의 # 뒤가 `<kind>/<id>`(상세 화면)이면 id(풀 수 없으면 ""), 아니면 null. */
export function detailId(hash, kind) {
  const m = hash.match(/^([a-z]+)\/(.+)$/);
  if (!m || m[1] !== kind) return null;
  try { return decodeURIComponent(m[2]); } catch (e) { return ""; }
}

export const fmtDay = (iso) => (iso ? T.fmtDateTime(iso).slice(0, 10) : "—");

export function head(title, intro, ...actions) {
  return h("div", { class: "a-head" },
    h("div", {}, h("h2", { text: title }), intro ? h("p", { text: intro }) : null),
    actions.length ? h("div", { class: "a-actions" }, ...actions) : null);
}

export function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("span", { class: "hint", text: hint }) : null);
}

export function checkbox(label, checked, hint) {
  const input = h("input", { type: "checkbox" });
  input.checked = !!checked;
  return { input, el: h("label", { class: "check" }, input, h("span", {}, label, hint ? h("small", { text: hint }) : null)) };
}

/** 정렬·페이지 나눔 표. columns: [{key, label, sort?, num?, wrap?, render(item)}]
 *  select: {id(item), label(item), onChange(Set)}를 주면 맨 앞에 고르기 체크박스 열(고른 것은 페이지를 넘겨도 남는다).
 *  rowHref(item)를 주면 행의 빈 곳을 눌러도 그 주소로 간다(키보드는 행 안의 링크를 쓴다). */
export function pagedTable({ columns, load, empty, defaultSort, select, rowHref }) {
  const state = { page: 1, size: 50, sort: defaultSort, selected: new Set() };
  const wrap = h("div");
  async function draw() {
    let data;
    try {
      data = await load(state);
    } catch (err) { wrap.replaceChildren(message(errorText(err))); return; }
    if (!data.items.length && state.page === 1) { wrap.replaceChildren(h("div", { class: "a-table-wrap" }, h("div", { class: "a-empty", text: empty }))); return; }
    const sortKey = state.sort.replace(/^-/, ""), desc = state.sort.startsWith("-");
    const changed = () => { if (select) select.onChange(state.selected); };
    let allBox = null;
    const rowBoxes = [];
    if (select) {
      allBox = h("input", { type: "checkbox", "aria-label": T.selectAll });
      allBox.checked = data.items.length > 0 && data.items.every((i) => state.selected.has(select.id(i)));
      allBox.addEventListener("change", () => {
        data.items.forEach((i) => (allBox.checked ? state.selected.add(select.id(i)) : state.selected.delete(select.id(i))));
        rowBoxes.forEach((b) => { b.checked = allBox.checked; });
        changed();
      });
    }
    const thead = h("tr", {}, select ? h("th", { scope: "col", class: "pick" }, allBox) : null, columns.map((c) => {
      const th = h("th", { scope: "col", class: c.num ? "num" : null });
      if (!c.sort) { th.textContent = c.label; return th; }
      if (sortKey === c.key) th.setAttribute("aria-sort", desc ? "descending" : "ascending");
      th.append(h("button", { type: "button", onclick: () => {
        state.sort = sortKey === c.key && !desc ? "-" + c.key : c.key;
        state.page = 1;
        draw();
      } }, c.label, sortKey === c.key ? (desc ? " ↓" : " ↑") : ""));
      return th;
    }));
    const rows = data.items.map((item) => {
      let pick = null;
      if (select) {
        const box = h("input", { type: "checkbox", "aria-label": select.label(item) });
        box.checked = state.selected.has(select.id(item));
        box.addEventListener("change", () => {
          if (box.checked) state.selected.add(select.id(item)); else state.selected.delete(select.id(item));
          allBox.checked = data.items.every((i) => state.selected.has(select.id(i)));
          changed();
        });
        rowBoxes.push(box);
        pick = h("td", { class: "pick" }, box);
      }
      const tr = h("tr", { class: rowHref ? "a-row-link" : null }, pick, columns.map((c) =>
        h("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : ""].join(" ").trim() || null }, c.render(item))));
      if (rowHref) {
        tr.addEventListener("click", (e) => {
          if (e.target.closest("a, button, input, select, label")) return;
          location.hash = rowHref(item);
        });
      }
      return tr;
    });
    const from = (state.page - 1) * state.size + 1, to = from + data.items.length - 1;
    const pager = h("div", { class: "a-pager" },
      h("span", { text: T.pageInfo(from, to, data.total) }),
      h("button", { type: "button", class: "btn small", disabled: state.page <= 1, onclick: () => { state.page -= 1; draw(); } }, T.prev),
      h("button", { type: "button", class: "btn small", disabled: to >= data.total, onclick: () => { state.page += 1; draw(); } }, T.next));
    wrap.replaceChildren(h("div", { class: "a-table-wrap" }, h("table", { class: "a-table" }, h("thead", {}, thead), h("tbody", {}, rows))),
      data.total > state.size ? pager : "");
  }
  return { el: wrap, draw, state };
}

export const qs = (state) => `?page=${state.page}&size=${state.size}&sort=${encodeURIComponent(state.sort)}`;
