// web/shared/ingest.js의 순수 함수(node --test): 여러 파일 올리기의 파일 ↔ 테이블 짝짓기, 진행률, 머리글 맞춤 저장.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  fileStem, pairFiles, mergeFiles, progressRatio, filesForError, aliasesToSave,
  LARGE_BATCH_ROWS, previewInsertRows, isLargeBatchPreview,
} from "../../web/shared/ingest.js";

const TABLES = ["entries", "events", "line_items", "Marks"];

test("파일 이름의 확장자와 끝의 (1)을 뺀다", () => {
  assert.equal(fileStem("entries.csv"), "entries");
  assert.equal(fileStem("entries.CSV"), "entries");
  assert.equal(fileStem("entries (2).csv"), "entries");
  assert.equal(fileStem("a.b.csv"), "a.b");
});

test("짝짓기: 같은 이름이면 exact(서버가 map 없이도 받는 경우)", () => {
  assert.deepEqual(pairFiles(["entries.csv", "events.csv"], TABLES),
    [{ name: "entries.csv", table: "entries", how: "exact" }, { name: "events.csv", table: "events", how: "exact" }]);
});

test("짝짓기: 대소문자·공백·_·-·(1) 차이는 loose로 맞춘다", () => {
  assert.deepEqual(pairFiles(["Line Items.csv", "line-items (1).csv", "MARKS.csv"], TABLES).map((p) => [p.table, p.how]),
    [["line_items", "loose"], ["line_items", "loose"], ["Marks", "loose"]]);
});

test("짝짓기: 맞는 테이블이 없거나 둘에 맞으면 null", () => {
  assert.deepEqual(pairFiles(["other.csv"], TABLES), [{ name: "other.csv", table: null, how: null }]);
  assert.deepEqual(pairFiles(["a_b.csv"], ["a-b", "ab"]).map((p) => p.table), [null]);
  assert.deepEqual(pairFiles(["x.csv"], []), [{ name: "x.csv", table: null, how: null }]);
  assert.deepEqual(pairFiles([], TABLES), []);
});

test("파일 더하기: 같은 이름은 제자리에서 새 것으로 바꾼다", () => {
  const a = { name: "a.csv", v: 1 }, b = { name: "b.csv", v: 1 }, a2 = { name: "a.csv", v: 2 }, c = { name: "c.csv", v: 1 };
  const out = mergeFiles([a, b], [a2, c]);
  assert.deepEqual(out.files, [a2, b, c]);
  assert.deepEqual(out.replaced, ["a.csv"]);
  assert.deepEqual(mergeFiles([], [a, a2]).files, [a2]);
});

test("진행률: 읽은 바이트 / 전체", () => {
  assert.equal(progressRatio({ step: "read", bytes: 50, total_bytes: 200 }), 0.25);
  assert.equal(progressRatio({ bytes: 300, total_bytes: 200 }), 1);
  assert.equal(progressRatio({ step: "lock" }), null);
  assert.equal(progressRatio({ bytes: 5, total_bytes: 0 }), null);
  assert.equal(progressRatio(null), null);
});

test("unknown_columns가 가리키는 파일: file이 있으면 그 파일, 없으면 그 테이블의 파일 모두", () => {
  const pairs = [{ name: "a.csv", table: "entries" }, { name: "b.csv", table: "events" }, { name: "c.csv", table: "entries" }];
  assert.deepEqual(filesForError(pairs, { file: "c.csv", table: "entries" }), ["c.csv"]);
  assert.deepEqual(filesForError(pairs, { table: "entries" }), ["a.csv", "c.csv"]);
  assert.deepEqual(filesForError(pairs, { file: "zz.csv", table: "events" }), ["b.csv"]);
  assert.deepEqual(filesForError(pairs, { table: "none" }), []);
});

test("aliases로 저장할 맞춤: 버린 것·빈 머리글·같은 이름은 뺀다", () => {
  assert.deepEqual(aliasesToSave({ "상태명": "state", "버릴것": null, "": "memo", " ": "memo", qty: "qty" }), { "상태명": "state" });
  assert.deepEqual(aliasesToSave(null), {});
});

test("대용량 미리보기 안내 기준(499,999 / 500,000행 경계, 여러 파일 합산, invalid 제외)", () => {
  assert.equal(LARGE_BATCH_ROWS, 500_000);
  // 단일 파일 경계
  assert.equal(isLargeBatchPreview([{ rows: { total: 499_999, invalid: 0 } }]), false);
  assert.equal(isLargeBatchPreview([{ rows: { total: 500_000, invalid: 0 } }]), true);
  // invalid 제외 후 넣을 행 수 기준
  assert.equal(previewInsertRows([{ rows: { total: 500_000, invalid: 1 } }]), 499_999);
  assert.equal(isLargeBatchPreview([{ rows: { total: 500_000, invalid: 1 } }]), false);
  assert.equal(isLargeBatchPreview([{ rows: { total: 500_001, invalid: 1 } }]), true);
  // 여러 파일 합산
  assert.equal(isLargeBatchPreview([{ rows: { total: 250_000, invalid: 0 } }, { rows: { total: 249_999, invalid: 0 } }]), false);
  assert.equal(isLargeBatchPreview([{ rows: { total: 250_000, invalid: 0 } }, { rows: { total: 250_000, invalid: 0 } }]), true);
  assert.equal(isLargeBatchPreview(null), false);
});
