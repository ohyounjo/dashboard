// 대시보드 배치(layout)를 다루는 순수 함수. DOM을 쓰지 않는다.
// layout의 꼴은 dashboard.yaml 그대로다(contracts/chart-shorthand.md "배치"):
//   layout = [항목…], 항목 = 행(배열) 또는 { tabs: [{ label, rows: [행…] }] }
//   행의 칸 = "차트키" | { chart, width?, height? } | { header } | { markdown, width?, height? }
// 여기 함수들은 넘겨받은 layout을 그 자리에서 고친다(호출하는 쪽이 사본을 넘긴다).
// 건드리지 않은 칸은 원래 꼴 그대로 둔다(문자열 칸을 객체로 바꾸지 않는다). 서버가 바뀌지 않은 행의 글자를 보존한다.
//
// 자리 표기:
//   묶음(c): null = layout 최상위, [b, t] = b번째 항목(탭 묶음)의 t번째 탭
//   칸 자리: { c, r, i } = 묶음 c의 r번째 행의 i번째 칸

export const GRID = 12;

export const isBlock = (entry) => !!entry && !Array.isArray(entry) && typeof entry === "object" && Array.isArray(entry.tabs);
export const isHeader = (item) => !!item && typeof item === "object" && "header" in item;
export const isText = (item) => !!item && typeof item === "object" && "markdown" in item;
export const isHeaderRow = (row) => Array.isArray(row) && row.some(isHeader);
export const chartOf = (item) => (typeof item === "string" ? item : item && typeof item === "object" && "chart" in item ? item.chart : null);
export const widthOf = (item) => (item && typeof item === "object" && Number.isInteger(item.width) ? item.width : null);
export const sameC = (a, b) => (a === null || b === null ? a === b : a[0] === b[0] && a[1] === b[1]);

/** 묶음 c의 행 목록(배열 그 자체). 없으면 null. */
export function rowsOf(layout, c) {
  if (c === null) return layout;
  const block = layout[c[0]];
  const tab = isBlock(block) ? block.tabs[c[1]] : null;
  return tab && Array.isArray(tab.rows) ? tab.rows : null;
}

/** 행 목록(배열 참조)이 어느 묶음인지 */
function containerOf(layout, rows) {
  if (rows === layout) return null;
  for (let b = 0; b < layout.length; b++) {
    if (!isBlock(layout[b])) continue;
    const t = layout[b].tabs.findIndex((tab) => tab.rows === rows);
    if (t >= 0) return [b, t];
  }
  return undefined;
}

export function itemAt(layout, addr) {
  const rows = rowsOf(layout, addr.c);
  const row = rows && rows[addr.r];
  return Array.isArray(row) ? row[addr.i] : undefined;
}

/** 차트 키가 놓인 자리(탭 속 포함). 없으면 null. */
export function findChart(layout, key) {
  const scan = (rows, c) => {
    for (let r = 0; r < rows.length; r++) {
      if (!Array.isArray(rows[r])) continue;
      const i = rows[r].findIndex((item) => chartOf(item) === key);
      if (i >= 0) return { c, r, i };
    }
    return null;
  };
  const top = scan(layout, null);
  if (top) return top;
  for (let b = 0; b < layout.length; b++) {
    if (!isBlock(layout[b])) continue;
    for (let t = 0; t < layout[b].tabs.length; t++) {
      const hit = scan(layout[b].tabs[t].rows || [], [b, t]);
      if (hit) return hit;
    }
  }
  return null;
}

/**
 * 행의 칸마다 화면에 그릴 칸 수. 계약의 규칙과 같다: 너비가 없는 칸은 남은 칸(12 − 지정 너비 합)을
 * 균등 분할하고 나머지는 앞 칸부터 1칸씩 더 준다. 제목은 한 줄 전체다.
 * extra: 자리만 차지하는 "너비 없는 칸"을 더 넣어 계산한다(추가하려는 초안 카드).
 */
export function rowWidths(row, extra = 0) {
  if (isHeaderRow(row)) return row.map(() => GRID);
  const fixed = row.reduce((sum, item) => sum + (widthOf(item) || 0), 0);
  const free = row.filter((item) => !widthOf(item)).length + extra;
  const room = Math.max(0, GRID - fixed);
  const share = free ? Math.floor(room / free) : 0;
  let rest = free ? room - share * free : 0;
  const out = row.map((item) => {
    const w = widthOf(item);
    if (w) return w;
    const give = share + (rest > 0 ? 1 : 0);
    if (rest > 0) rest--;
    return Math.max(1, give);
  });
  for (let n = 0; n < extra; n++) { out.push(Math.max(1, share + (rest > 0 ? 1 : 0))); if (rest > 0) rest--; }
  return out;
}

/** 이 행에 칸을 하나 더 넣을 수 있는지(설정 검사의 "행 너비가 12칸을 넘음"과 같은 식) */
export function fits(row, item, skipIndex = -1) {
  if (row.length - (skipIndex >= 0 ? 1 : 0) >= GRID) return false;
  let fixed = widthOf(item) || 0;
  let free = widthOf(item) ? 0 : 1;
  row.forEach((other, n) => {
    if (n === skipIndex) return;
    const w = widthOf(other);
    if (w) fixed += w; else free++;
  });
  return fixed <= GRID && GRID - fixed >= free;
}

/** i번째 칸에 줄 수 있는 가장 큰 너비: 다른 칸의 지정 너비는 건드리지 않고, 너비 없는 칸에는 1칸씩 남긴다 */
export function maxWidth(row, i) {
  let fixed = 0, free = 0;
  row.forEach((item, n) => {
    if (n === i) return;
    const w = widthOf(item);
    if (w) fixed += w; else free++;
  });
  return Math.max(1, GRID - fixed - free);
}

/** 칸의 너비를 바꾼다(null이면 너비를 지운다 = 남은 칸을 나눠 받는다). 바꾼 칸만 꼴이 달라진다. */
export function setSize(layout, addr, name, value) {
  const row = rowsOf(layout, addr.c)[addr.r];
  const item = row[addr.i];
  if (isHeader(item)) return;
  const next = typeof item === "string" ? { chart: item } : { ...item };
  if (value == null) delete next[name]; else next[name] = value;
  // 차트만 남은 객체는 차트 키 문자열로 되돌린다(가장 짧은 꼴)
  row[addr.i] = "chart" in next && Object.keys(next).length === 1 ? next.chart : next;
}

/**
 * 칸을 옮긴다.
 * to: { c, r, i } = 그 행의 i번째 틈(옮기기 전 기준) | { c, at } = 묶음 c의 at번째 자리에 새 행
 * 돌려주는 값: { addr } 또는 { error: "header" | "last" | "full" | "same" }
 */
export function moveItem(layout, from, to) {
  const srcRows = rowsOf(layout, from.c);
  const srcRow = srcRows && srcRows[from.r];
  if (!Array.isArray(srcRow) || srcRow[from.i] === undefined) return { error: "same" };
  const item = srcRow[from.i];
  const dstRows = rowsOf(layout, to.c);
  if (!dstRows) return { error: "same" };
  const fresh = "at" in to;
  const sameRow = !fresh && dstRows === srcRows && to.r === from.r;

  if (fresh) {
    // 혼자 있는 칸을 제자리(바로 위·아래 틈)에 새 행으로 옮기는 것은 변화가 없다
    if (srcRow.length === 1 && dstRows === srcRows && (to.at === from.r || to.at === from.r + 1)) return { error: "same" };
  } else {
    const dstRow = dstRows[to.r];
    if (!Array.isArray(dstRow)) return { error: "same" };
    if (sameRow) {
      if (to.i === from.i || to.i === from.i + 1) return { error: "same" };
    } else {
      if (isHeader(item) || isHeaderRow(dstRow)) return { error: "header" };   // 제목은 한 행에 혼자 둔다
      if (!fits(dstRow, item)) return { error: "full" };
    }
  }
  // 탭에는 행이 하나는 있어야 한다: 탭의 마지막 칸은 밖으로 못 나간다
  if (from.c !== null && dstRows !== srcRows && srcRows.length === 1 && srcRow.length === 1) return { error: "last" };

  let dstRow, index;
  if (fresh) { dstRow = []; dstRows.splice(to.at, 0, dstRow); index = 0; }
  else { dstRow = dstRows[to.r]; index = sameRow && to.i > from.i ? to.i - 1 : to.i; }
  srcRow.splice(from.i, 1);
  dstRow.splice(index, 0, item);
  if (!srcRow.length) srcRows.splice(srcRows.indexOf(srcRow), 1);   // 비게 된 행은 없앤다
  return { addr: { c: containerOf(layout, dstRows), r: dstRows.indexOf(dstRow), i: index } };
}

/** 칸을 지운다(비게 된 행도). 탭의 마지막 칸이면 { error: "last" }. */
export function removeItem(layout, addr) {
  const rows = rowsOf(layout, addr.c);
  const row = rows[addr.r];
  if (addr.c !== null && rows.length === 1 && row.length === 1) return { error: "last" };
  row.splice(addr.i, 1);
  if (!row.length) rows.splice(addr.r, 1);
  return {};
}

/** 칸을 넣는다. place: { c, r, i } | { c, at }(새 행) */
export function insertItem(layout, place, item) {
  const rows = rowsOf(layout, place.c);
  if ("at" in place) { rows.splice(place.at, 0, [item]); return { c: place.c, r: place.at, i: 0 }; }
  rows[place.r].splice(place.i, 0, item);
  return { c: place.c, r: place.r, i: place.i };
}

/** 최상위 항목(행 또는 탭 묶음)을 옮긴다. at은 옮기기 전 기준의 틈(0 ~ 길이). 옮긴 뒤의 자리를 돌려준다(변화가 없으면 -1). */
export function moveEntry(layout, from, at) {
  if (at === from || at === from + 1) return -1;
  const [entry] = layout.splice(from, 1);
  const to = at > from ? at - 1 : at;
  layout.splice(to, 0, entry);
  return to;
}

/** 탭을 지우고 그 안의 행을 탭 묶음 바로 아래(최상위)로 꺼낸다. 마지막 탭이면 묶음도 없앤다. */
export function removeTab(layout, b, t) {
  const block = layout[b];
  const [tab] = block.tabs.splice(t, 1);
  const rows = Array.isArray(tab.rows) ? tab.rows : [];
  if (block.tabs.length) layout.splice(b + 1, 0, ...rows);
  else layout.splice(b, 1, ...rows);
}

/** layout에 놓인 차트 키 전부(탭 속 포함) */
export function placedCharts(layout) {
  const out = [];
  const scan = (rows) => rows.forEach((row) => { if (Array.isArray(row)) row.forEach((item) => { const k = chartOf(item); if (k) out.push(k); }); });
  scan(layout);
  layout.forEach((entry) => { if (isBlock(entry)) entry.tabs.forEach((tab) => scan(tab.rows || [])); });
  return out;
}
