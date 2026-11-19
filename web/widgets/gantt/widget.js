// 간트 위젯. 계약: contracts/widgets.md
const DATE = /^(\d{4})-(\d{2})-(\d{2})/;
const TONES = ["primary", "good", "warn", "bad", "neutral"];

/** "YYYY-MM-DD..." → UTC 일 번호. 날짜가 아니면 NaN */
function toDay(v) {
  if (typeof v !== "string") return NaN;
  const m = DATE.exec(v);
  if (!m) return NaN;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const t = Date.UTC(y, mo - 1, d);
  return new Date(t).getUTCDate() === d ? t / 864e5 : NaN;
}
const iso = (day) => new Date(day * 864e5).toISOString().slice(0, 10);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function compare(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  const da = toDay(a), db = toDay(b);
  if (!Number.isNaN(da) && !Number.isNaN(db)) return da - db;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

export default function render(root, rows, options, app) {
  const S = app.strings;
  const f = options.fields || {};
  const statuses = options.statuses || {};

  // 필수 열 확인(행이 있을 때만 알 수 있다)
  if (rows.length) {
    for (const col of [f.label, f.start, f.end, f.progress, f.status, options.sort]) {
      if (col && !(col in rows[0])) throw new Error(S.widgetColumnMissing(col));
    }
  }

  // 막대로 바꿀 수 있는 행만 남긴다
  const items = [];
  rows.forEach((row, i) => {
    const s = toDay(row[f.start]), e = toDay(row[f.end]);
    if (Number.isNaN(s) || Number.isNaN(e) || s > e) {
      console.warn("gantt: skip row", i, row[f.start], row[f.end]);
      return;
    }
    items.push({ row, i, s, e });
  });
  const sortCol = options.sort || f.start;
  items.sort((a, b) => compare(a.row[sortCol], b.row[sortCol]) || a.i - b.i);

  if (!items.length) {
    root.append(el("div", "w-empty", S.ganttEmpty));
    return { destroy() {} };
  }

  // 축 범위
  let r0 = Math.min(...items.map((x) => x.s)), r1 = Math.max(...items.map((x) => x.e));
  if (options.range) {
    const a = toDay(options.range.start), b = toDay(options.range.end);
    if (!Number.isNaN(a) && !Number.isNaN(b) && a < b) { r0 = a; r1 = b; }
  }
  const span = Math.max(1, r1 - r0);
  const pct = (d) => Math.max(0, Math.min(100, ((d - r0) / span) * 100));

  const meta = el("div", "w-meta");
  meta.append(el("span", "num", S.dateRange(iso(r0), iso(r1))));
  root.append(meta);

  const scroller = el("div", "gantt");
  const grid = el("div", "gantt-inner");
  scroller.append(grid);
  root.append(scroller);

  // 월 눈금
  grid.append(el("div"));
  const head = el("div", "g-head");
  head.setAttribute("aria-hidden", "true");
  const first = new Date(r0 * 864e5);
  let y = first.getUTCFullYear(), m = first.getUTCMonth() + 1;
  if (first.getUTCDate() !== 1) { m++; if (m > 12) { m = 1; y++; } }
  for (;;) {
    const d = Date.UTC(y, m - 1, 1) / 864e5;
    if (d > r1) break;
    const tick = el("span", null, S.monthLabel(m));
    tick.style.left = pct(d) + "%";
    head.append(tick);
    m++; if (m > 12) { m = 1; y++; }
  }
  grid.append(head);

  const asOfDay = toDay(app.asOf);
  const showToday = !Number.isNaN(asOfDay) && asOfDay >= r0 && asOfDay <= r1;
  const todayPct = showToday ? pct(asOfDay) : 0;
  const fills = [];

  items.forEach((it, idx) => {
    const row = it.row;
    const sv = f.status ? row[f.status] : null;
    const st = sv != null ? statuses[String(sv)] : null;
    const tone = st && TONES.includes(st.tone) ? st.tone : "neutral";
    const statusLabel = st ? st.label : sv != null ? String(sv) : null;
    const rawP = f.progress ? row[f.progress] : null;
    const prog = typeof rawP === "number" && Number.isFinite(rawP) ? Math.max(0, Math.min(100, rawP)) : null;
    const label = row[f.label] == null ? "" : String(row[f.label]);

    // 라벨: [아이콘] 라벨 / MM.DD–MM.DD · 상태 진척%
    const lab = el("div", "g-label");
    const title = el("b", null, (st && st.icon ? st.icon + " " : "") + label);
    title.title = label;
    const tail = [statusLabel, prog != null ? S.progress(Math.round(prog)) : null].filter((x) => x != null).join(" ");
    const sub = el("span", null, [S.shortRange(iso(it.s), iso(it.e)), tail].filter(Boolean).join(" · "));
    lab.append(title, sub);

    const track = el("div", "g-track");
    track.setAttribute("aria-hidden", "true");
    const bar = el("div", "g-bar");
    bar.dataset.tone = tone;
    const l = pct(it.s), r = pct(it.e);
    bar.style.left = l + "%";
    bar.style.width = Math.max(0.5, r - l) + "%";
    bar.title = prog != null ? `${label} ${S.progress(Math.round(prog))}` : label;
    if (prog != null) {
      const fill = el("div", "g-fill");
      fill.style.transform = `scaleX(${prog / 100})`;
      bar.append(fill);
      fills.push([fill, prog / 100]);
    }
    track.append(bar);
    if (showToday) {
      const t = el("div", "g-today");
      t.style.left = todayPct + "%";
      if (idx === 0) {
        if (todayPct > 85) t.classList.add("flip");
        t.append(el("span", null, S.today(app.asOf)));
      }
      track.append(t);
    }
    grid.append(lab, track);
  });

  // 범례: 상태 표 + 오늘 선
  const legendItems = Object.values(statuses).filter((s) => s && s.label).map((s) => (s.icon ? s.icon + " " : "") + s.label);
  if (legendItems.length || showToday) {
    const legend = el("div", "g-legend");
    legendItems.forEach((t) => legend.append(el("span", null, t)));
    if (showToday) legend.append(el("span", "today", S.todayLegend));
    root.append(legend);
  }
  if (app.truncated) root.append(el("p", "w-note", S.truncated(rows.length)));

  // 모션: 진척 채움이 0에서 값까지(600ms). transform만 쓰고 reduced-motion이면 끈다.
  const anims = [];
  if (!app.reducedMotion && typeof Element.prototype.animate === "function") {
    for (const [fill, k] of fills) {
      anims.push(fill.animate([{ transform: "scaleX(0)" }, { transform: `scaleX(${k})` }],
        { duration: 600, easing: "cubic-bezier(.2,.7,.2,1)" }));
    }
  }
  return { destroy() { anims.forEach((a) => a.cancel()); } };
}
