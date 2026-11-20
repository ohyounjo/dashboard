// 단색 선 아이콘(24x24, currentColor). 탭 아이콘 14개는 contracts/icons.md와 같다.
// 이름은 모양을 뜻할 뿐 특정 도메인을 뜻하지 않는다.

const r = (x, y, w, h, rx = 1.5) => ["rect", { x, y, width: w, height: h, rx }];
const p = (d) => ["path", { d }];
const c = (cx, cy, rr) => ["circle", { cx, cy, r: rr }];

const TAB_ICONS = {
  grid: [r(3, 3, 7, 9), r(14, 3, 7, 5), r(14, 12, 7, 9), r(3, 16, 7, 5)],
  calendar: [r(3, 5, 18, 16, 2), p("M3 10h18M8 3v4M16 3v4M7 14h5M7 17h8")],
  alert: [p("M10.3 4.2 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"), p("M12 9.5v4.5M12 17.5v.5")],
  doc: [p("M6 3h8l4 4v14H6z"), p("M14 3v4h4M9 12h6M9 16h6")],
  chart: [p("M4 20h16"), r(6, 11, 3, 6, 1), r(11, 6, 3, 11, 1), r(16, 13, 3, 4, 1)],
  table: [r(3, 4, 18, 16, 2), p("M3 9.5h18M3 14.5h18M9.5 9.5V20")],
  users: [c(9, 8, 3.2), p("M3.5 19.5a5.5 5.5 0 0 1 11 0"), c(17, 9, 2.4), p("M16 14.2a4.5 4.5 0 0 1 5 4.3")],
  box: [p("M12 3 3.5 7.5v9L12 21l8.5-4.5v-9z"), p("M3.5 7.5 12 12l8.5-4.5M12 12v9")],
  list: [p("M9 6h11M9 12h11M9 18h11"), p("M4.5 6h.5M4.5 12h.5M4.5 18h.5")],
  flag: [p("M5 21V4"), p("M5 4.5h11l-2 4 2 4H5")],
  target: [c(12, 12, 9), c(12, 12, 5), c(12, 12, 1.2)],
  cart: [p("M3 4h2.5l2.2 11h10.6l2-8H6.6"), c(9, 19.5, 1.3), c(17, 19.5, 1.3)],
  megaphone: [p("M4 10v4a1 1 0 0 0 1 1h2l8 4.5V4.5L7 9H5a1 1 0 0 0-1 1z"), p("M7 15l1.2 4.5M19 9.5a3.5 3.5 0 0 1 0 5")],
  home: [p("M3.5 11 12 4l8.5 7"), p("M5.5 9.5V20h13V9.5"), p("M10 20v-5.5h4V20")],
};

// 셸 전용 UI 아이콘
const UI_ICONS = {
  moon: [p("M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z")],
  sun: [c(12, 12, 4), p("M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4")],
  lock: [r(5, 10.5, 14, 10, 2), p("M8 10.5V7.5a4 4 0 0 1 8 0v3")],
  refresh: [p("M20 11a8 8 0 1 0-2.3 5.7"), p("M20 4.5V11h-6.5")],
  upload: [p("M12 15V4M7.5 8.5 12 4l4.5 4.5"), p("M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4")],
  key: [c(8, 15, 4), p("M11 12l8.5-8.5M16 7l2.5 2.5M14 9l2 2")],
  plug: [p("M9 3v5M15 3v5"), p("M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"), p("M12 16.5V21")],
  database: [["ellipse", { cx: 12, cy: 6, rx: 7.5, ry: 3 }], p("M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"), p("M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3")],
  warning: TAB_ICONS.alert,
  user: [c(12, 8, 4), p("M4.5 20.5a7.5 7.5 0 0 1 15 0")],
  logout: [p("M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"), p("M10 16.5 5.5 12 10 7.5M5.5 12H15")],
};

const NS = "http://www.w3.org/2000/svg";

/** 이름으로 SVG 요소를 만든다. 모르는 이름이면 grid. */
export function icon(name) {
  const parts = TAB_ICONS[name] || UI_ICONS[name] || TAB_ICONS.grid;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("icon");
  for (const [tag, attrs] of parts) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.appendChild(el);
  }
  return svg;
}

export const TAB_ICON_NAMES = Object.keys(TAB_ICONS);
