// 문구 파일(web/strings.js, web/studio/strings.js)의 정적 검사(node --test). make test-unit이 node 컨테이너에서 돌린다.
// 객체 리터럴에서 같은 키를 두 번 쓰면 JS는 오류 없이 뒤의 값으로 덮어쓴다. 웹앱의 사용자 메뉴 라벨(문자열 account)이
// 내 정보 화면의 문구 묶음(객체 account)에 덮여 "[object Object]"로 보인 일이 있었다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import S from "../../web/strings.js";

const FILES = ["web/strings.js", "web/studio/strings.js"];

/** `export default {` 바로 아래(들여쓰기 두 칸)의 키 목록. 문구 파일은 이 꼴로만 쓴다 */
function topKeys(path) {
  const src = readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
  const body = src.slice(src.indexOf("export default {"));
  return [...body.matchAll(/^ {2}([A-Za-z_$][\w$]*)\s*:/gm)].map((m) => m[1]);
}

for (const path of FILES) {
  test(`${path}: 최상위 키가 겹치지 않는다`, () => {
    const keys = topKeys(path);
    assert.ok(keys.length > 10, "키를 읽지 못함");
    const dup = keys.filter((k, i) => keys.indexOf(k) !== i);
    assert.deepEqual(dup, []);
  });
}

test("웹앱 사용자 메뉴 라벨은 문자열이고 내 정보 화면 문구는 객체다", () => {
  assert.equal(typeof S.accountButton, "string");
  assert.equal(typeof S.account, "object");
  assert.equal(typeof S.account.title, "string");
});
