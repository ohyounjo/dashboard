// 단색 선 아이콘(24x24, currentColor). Studio와 운용 화면이 함께 쓰는 아이콘(web/shared/dom.js가 쓴다).
// 아래 '탭 아이콘' 구간(const r ~ TAB_ICONS)은 web/icons.js의 같은 구간을 그대로 복사한 것이다.
// studio 컨테이너는 web/studio/와 web/shared/만 서빙하므로 원본을 import할 수 없다. 원본이 바뀌면 다시 복사한다
// (tests/test_studio_ui_static.py가 일치를 검사한다).

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

// Studio 화면 전용 아이콘
const UI_ICONS = {
  moon: [p("M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z")],
  sun: [c(12, 12, 4), p("M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4")],
  refresh: [p("M20 11a8 8 0 1 0-2.3 5.7"), p("M20 4.5V11h-6.5")],
  upload: [p("M12 15V4M7.5 8.5 12 4l4.5 4.5"), p("M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4")],
  database: [["ellipse", { cx: 12, cy: 6, rx: 7.5, ry: 3 }], p("M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"), p("M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3")],
  warning: TAB_ICONS.alert,
  check: [p("M5 12.5l4.5 4.5L19 7.5")],
  x: [p("M6 6l12 12M18 6 6 18")],
  plus: [p("M12 5v14M5 12h14")],
  up: [p("M12 19V5M6 11l6-6 6 6")],
  down: [p("M12 5v14M6 13l6 6 6-6")],
  trash: [p("M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13"), p("M10 11v6M14 11v6")],
  grip: [c(9, 6, 1), c(15, 6, 1), c(9, 12, 1), c(15, 12, 1), c(9, 18, 1), c(15, 18, 1)],
  play: [p("M7 4.5v15l12-7.5z")],
  diff: [p("M7 4v8M3 8h8M13 17h8"), p("M4 20 20 4")],
  file: [p("M6 3h8l4 4v14H6z"), p("M14 3v4h4")],
  panel: [r(3, 4, 18, 16, 2), p("M14.5 4v16")],
  external: [p("M14 4h6v6M20 4l-9 9"), p("M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5")],
  menu: [p("M4 6.5h16M4 12h16M4 17.5h16")],
  sliders: [p("M4 7h9M17 7h3M4 17h3M11 17h9"), c(15, 7, 2), c(9, 17, 2)],
  layers: [p("M12 3 3 8l9 5 9-5z"), p("M3 12.5l9 5 9-5M3 17l9 5 9-5")],
  info: [c(12, 12, 9), p("M12 11v6M12 7.5v.5")],
  filter: [p("M4 5h16l-6 7.5V19l-4-2v-4.5z")],
  pencil: [p("M4 20l1-4L16.5 4.5l3 3L8 19z"), p("M14.5 6.5l3 3")],
  left: [p("M19 12H5M11 6l-6 6 6 6")],
  right: [p("M5 12h14M13 6l6 6-6 6")],
  heading: [p("M5 5v14M13 5v14M5 12h8"), p("M17 11l2.5-2V19")],
  tabset: [p("M3 9h18v10H3z"), p("M3 9V5h6v4M9 5h6v4")],
  code: [p("M8.5 7 3.5 12l5 5M15.5 7l5 5-5 5")],
  // 차트 유형 그림(배치 화면의 카드와 팔레트)
  vizLine: [p("M4 20h16"), p("M4 15l5-6 4 3 7-8")],
  vizArea: [p("M4 20V14l5-5 4 3 7-7v15z")],
  vizPie: [c(12, 12, 9), p("M12 3v9l7.6 4.6")],
  vizNumber: [p("M9.5 4 7.5 20M16.5 4l-2 16M4.5 9.5h16M3.5 14.5h16")],
  vizDots: [p("M4 4v16h16"), c(9, 14, 1.4), c(13, 9, 1.4), c(17, 12, 1.4), c(16.5, 6, 1.4)],
  vizCells: [r(3, 3, 5, 5, 1), r(9.5, 3, 5, 5, 1), r(16, 3, 5, 5, 1), r(3, 9.5, 5, 5, 1), r(9.5, 9.5, 5, 5, 1), r(16, 9.5, 5, 5, 1), r(3, 16, 5, 5, 1), r(9.5, 16, 5, 5, 1), r(16, 16, 5, 5, 1)],
  vizFunnel: [p("M3.5 5h17l-3 5h-11zM7.5 12.5h9l-2.2 4H9.7zM10.5 19h3")],
  vizGauge: [p("M4 17a8 8 0 0 1 16 0"), p("M12 17l4.5-6")],
  vizRadar: [p("M12 3l8.5 6.2-3.2 10H6.7L3.5 9.2z"), p("M12 8l3.8 2.8-1.4 4.5H9.6l-1.4-4.5z")],
  vizBlocks: [r(3, 4, 10, 16), r(13, 4, 8, 9), r(13, 13, 8, 7)],
  vizFlow: [p("M4 6c8 0 8 12 16 12M4 12c8 0 8-6 16-6M4 18c6 0 8-3 16-3")],
  vizBox: [p("M12 3v4M12 17v4M9.5 3h5M9.5 21h5M7 12h10"), r(7, 7, 10, 10)],
  vizWords: [p("M4 17 7.5 7l3.5 10M5.3 13.5h4.4M14 17v-6M14 13.5c.6-1.6 3.6-1.6 3.6.5V17M20 9v8")],
};

// 축약 차트 유형 → 그림. registry에는 그림이 없어서 여기서 정한다. 표에 없는 유형(새로 추가한 유형)은 기본 그림(chart)이다.
const VIZ_ICONS = {
  line: "vizLine", mixed: "vizLine", area: "vizArea", bar: "chart", histogram: "chart", waterfall: "chart",
  big_number: "vizNumber", big_number_total: "vizNumber", gauge: "vizGauge",
  pie: "vizPie", sunburst: "vizPie", funnel: "vizFunnel", radar: "vizRadar", treemap: "vizBlocks",
  scatter: "vizDots", bubble: "vizDots", heatmap: "vizCells", box_plot: "vizBox",
  table: "table", pivot_table: "table", sankey: "vizFlow", word_cloud: "vizWords",
};
/** 차트 유형의 그림 이름(icon()에 넘긴다). viz가 없으면(원본 방식 차트) code. */
export const vizIconName = (viz) => (viz ? VIZ_ICONS[viz] || "chart" : "code");

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
