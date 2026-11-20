// 대시보드 배치·필터 화면이 같이 쓰는 것: 대시보드 고르기, 저장 중 잠금.
import S from "./strings.js";
import { h, select } from "./dom.js";

// 마지막으로 고른 대시보드(프로젝트별). 배치 ↔ 필터 화면을 오가도 같은 대시보드를 보여 준다.
const picked = { project: null, key: null };

const boardsOf = (ctx) => (ctx.config.dashboard && ctx.config.dashboard.dashboards && typeof ctx.config.dashboard.dashboards === "object" ? ctx.config.dashboard.dashboards : {});

/** 지금 보여 줄 대시보드 키(없으면 null). want가 있으면 그것으로 바꾼다. */
export function pickDashboard(ctx, want) {
  const keys = Object.keys(boardsOf(ctx));
  if (want && keys.includes(want)) { picked.project = ctx.project; picked.key = want; }
  if (picked.project !== ctx.project || !keys.includes(picked.key)) { picked.project = ctx.project; picked.key = keys[0] || null; }
  return picked.key;
}

/** 대시보드 선택 상자. onPick(key)가 false를 돌려주면 선택을 되돌린다(저장하지 않은 폼). */
export function dashboardSelect(ctx, current, onPick) {
  const all = boardsOf(ctx);
  const opts = Object.keys(all).map((k) => ({ value: k, label: all[k] && all[k].title ? `${all[k].title} · ${k}` : k }));
  const el = select(opts, current, { "data-action": "pick-dashboard", "aria-label": S.dashboardPick });
  el.addEventListener("change", () => { if (onPick(el.value) === false) el.value = current; });
  return h("div", { class: "dash-pick" }, h("span", { class: "field-label", text: S.dashboardPick }), el);
}

/**
 * 변경 연산을 보내는 동안 컨테이너를 잠근다(입력·끌기 차단).
 * 컨테이너는 다른 화면도 쓰므로, 화면을 떠났더라도 요청이 끝나면 반드시 잠금을 푼다.
 * send()는 { ok: true } 또는 { ok: false, info }를 돌려주고, 그 사이 화면을 떠났으면 null을 돌려준다.
 */
export function saver(container, ctx, isAlive) {
  let busy = false;
  const unlock = () => { container.classList.remove("is-busy"); container.inert = false; };
  return {
    get busy() { return busy; },
    async send(ops) {
      if (busy) return null;
      busy = true;
      container.classList.add("is-busy");
      container.inert = true;
      try {
        await ctx.patch(ops);
        busy = false; unlock();
        return isAlive() ? { ok: true } : null;
      } catch (e) {
        busy = false; unlock();
        return isAlive() ? { ok: false, info: ctx.describeError(e) } : null;
      }
    },
  };
}
