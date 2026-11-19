// 칸반 위젯(보기 전용, 드래그 없음). 계약: contracts/widgets.md
const TONES = ["primary", "good", "warn", "bad", "neutral"];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export default function render(root, rows, options, app) {
  const S = app.strings;
  const groupBy = options.group_by;
  const columns = options.columns || [];
  const card = options.card || {};
  const chips = card.chips || [];
  const filter = options.filter || null;

  if (rows.length) {
    for (const col of [groupBy, card.title, ...chips, filter && filter.column]) {
      if (col && !(col in rows[0])) throw new Error(S.widgetColumnMissing(col));
    }
  }

  // 어느 칸에도 없는 group_by 값은 표시하지 않고 한 번만 경고
  const known = new Set(columns.map((c) => String(c.value)));
  const unknown = [...new Set(rows.map((r) => String(r[groupBy])).filter((v) => !known.has(v)))];
  if (unknown.length) console.warn("kanban: values without a column are hidden", unknown);

  const toolbar = el("div", "toolbar");
  const count = el("span", "count num");
  let select = null;
  if (filter) {
    const label = el("label", null, filter.label + " ");
    select = el("select", "sel");
    select.append(new Option(S.all, ""));
    const values = [...new Set(rows.map((r) => r[filter.column]).filter((v) => v != null).map(String))]
      .sort((a, b) => a.localeCompare(b, S.locale, { numeric: true }));
    values.forEach((v) => select.append(new Option(v, v)));
    label.append(select);
    toolbar.append(label);
  } else {
    toolbar.append(el("span"));
  }
  const right = el("span", "count");
  right.append(count, document.createTextNode(" · " + S.kanbanReadOnly));
  toolbar.append(right);

  const board = el("div", "kanban");
  root.append(toolbar, board);
  if (app.truncated) root.append(el("p", "w-note", S.truncated(rows.length)));

  function draw() {
    const f = select ? select.value : "";
    const visible = rows.filter((r) => !f || (r[filter.column] != null && String(r[filter.column]) === f));
    let shown = 0;
    board.replaceChildren();
    for (const col of columns) {
      const cards = visible.filter((r) => String(r[groupBy]) === String(col.value));
      shown += cards.length;
      const box = el("section", "col");
      if (col.tone && TONES.includes(col.tone)) box.dataset.tone = col.tone;
      box.setAttribute("aria-label", S.columnCountLabel(col.label, cards.length));
      const h3 = el("h3");
      h3.append(el("span", "name", col.label), el("span", "num", String(cards.length)));
      box.append(h3);
      if (!cards.length) box.append(el("div", "col-empty", S.kanbanEmpty));
      for (const r of cards) {
        const a = el("article", "card");
        a.setAttribute("draggable", "false");
        const t = r[card.title] == null ? "" : String(r[card.title]);
        a.append(el("span", "t", (col.icon ? col.icon + " " : "") + t));
        const meta = el("span", "m");
        chips.forEach((c, i) => {
          const v = r[c];
          if (v == null || v === "") return;
          const chip = el("span", i === 0 ? "pill soft" : null, String(v));
          chip.title = String(v);
          meta.append(chip);
        });
        if (meta.childNodes.length) a.append(meta);
        box.append(a);
      }
      board.append(box);
    }
    count.textContent = S.cardCount(shown);
  }

  if (select) select.addEventListener("change", draw);
  draw();
  return { destroy() { if (select) select.removeEventListener("change", draw); } };
}
