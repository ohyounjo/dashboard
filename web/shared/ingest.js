// 데이터 적재 화면의 순수 함수(파일 ↔ 테이블 짝짓기, 진행률). DOM을 쓰지 않는다(node --test로 시험한다).
// 문구는 이 파일에 두지 않는다. 이 폴더 밖의 모듈을 import하지 않는다.

const loose = (s) => String(s).toLowerCase().replace(/[\s_-]+/g, "");

/** 파일 이름에서 확장자(.csv)와 끝의 " (1)"(같은 이름을 내려받을 때 브라우저가 붙인다)를 뺀다. */
export function fileStem(name) {
  return String(name).replace(/\.csv$/i, "").replace(/\s*\(\d+\)$/, "").trim();
}

/** 파일 이름 → 테이블 짝짓기. tables: 고를 수 있는 테이블 이름(누적 테이블).
 *  서버는 map이 없으면 확장자를 뺀 이름이 테이블 이름과 같아야 한다. 화면은 그 밖에 대소문자·공백·_·- 차이와
 *  끝의 " (1)"도 받아 미리 짝짓고 map으로 보낸다. 두 테이블에 맞으면 짝짓지 않는다.
 *  돌려주는 것: [{name, table: 이름|null, how: "exact"|"loose"|null}] (names 순서) */
export function pairFiles(names, tables) {
  const list = (tables || []).map(String);
  return (names || []).map((name) => {
    const raw = String(name).replace(/\.csv$/i, "");
    if (list.includes(raw)) return { name, table: raw, how: "exact" };
    const stem = fileStem(name);
    const hits = list.filter((t) => loose(t) === loose(stem));
    return hits.length === 1 ? { name, table: hits[0], how: "loose" } : { name, table: null, how: null };
  });
}

/** 파일 목록에 새 파일들을 더한다. 같은 이름이 있으면 새 것으로 바꾼다(서버는 같은 이름 둘을 받지 않는다).
 *  current·added는 {name}을 가진 것들. 돌려주는 것: {files, replaced: [바꾼 이름]} (원래 자리를 지킨다) */
export function mergeFiles(current, added) {
  const files = [...(current || [])];
  const replaced = [];
  for (const f of added || []) {
    const at = files.findIndex((x) => x.name === f.name);
    if (at >= 0) { files[at] = f; if (!replaced.includes(f.name)) replaced.push(f.name); } else files.push(f);
  }
  return { files, replaced };
}

/** 작업 진행률(contracts/manage.md "작업"의 progress: {step, bytes, total_bytes, …}) → 0~1. 전체 크기를 모르면 null. */
export function progressRatio(progress) {
  const p = progress || {};
  const done = Number(p.bytes), total = Number(p.total_bytes);
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
  return Math.min(1, Math.max(0, done / total));
}

/** 여러 파일 적재의 unknown_columns가 가리키는 파일 이름들. 오류에 file이 있으면 그 파일,
 *  없으면 그 테이블에 짝지은 파일 모두(같은 테이블에 파일이 여럿이면 어느 것인지 모르므로 모두에 적용한다). */
export function filesForError(pairs, { file, table } = {}) {
  if (file && (pairs || []).some((p) => p.name === file)) return [file];
  return (pairs || []).filter((p) => p.table === table).map((p) => p.name);
}

/** 머리글 맞춤 중 aliases로 저장할 것: 버리지 않았고(열이 있음) 이름이 빈 머리글이 아닌 것. */
export function aliasesToSave(columns) {
  const out = {};
  for (const [header, col] of Object.entries(columns || {})) {
    if (col && String(header).trim() && header !== col) out[header] = col;
  }
  return out;
}
