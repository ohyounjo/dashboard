// web/common.js의 순수 함수(node --test). make test-unit이 node 컨테이너에서 돌린다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { safeNext } from "../../web/common.js";

const ORIGIN = "http://localhost:8080";

test("같은 출처의 경로·쿼리·해시는 그대로 돌려준다", () => {
  assert.equal(safeNext("/", ORIGIN), "/");
  assert.equal(safeNext("/#schedule", ORIGIN), "/#schedule");
  assert.equal(safeNext("/admin/?x=1#groups", ORIGIN), "/admin/?x=1#groups");
  assert.equal(safeNext("http://localhost:8080/admin/", ORIGIN), "/admin/");
  // 스킴만 같은 상대 표기는 URL 파서가 같은 출처의 경로로 읽는다(돌려주는 값도 경로뿐이다)
  assert.equal(safeNext("http:evil.example", ORIGIN), "/evil.example");
});

test("다른 출처로 가는 입력은 / 로 바꾼다", () => {
  for (const raw of [
    "//evil.example", "/\t/evil.example", "/\n/evil.example", "/\r\n/evil.example", "\t//evil.example",
    "/\\evil.example", "\\\\evil.example", "/\\/evil.example", "https://evil.example/", "http://localhost:8081/",
    "javascript:alert(1)", "data:text/html,x", " //evil.example", "///evil.example",
  ]) {
    assert.equal(safeNext(raw, ORIGIN), "/", JSON.stringify(raw));
  }
});

test("로그인 전 화면으로는 돌아가지 않는다", () => {
  for (const p of ["/login.html", "/signup.html?x", "/forgot.html", "/reset.html#token=x", "/verify.html"]) {
    assert.equal(safeNext(p, ORIGIN), "/", p);
  }
});

test("문자열이 아니거나 비어 있으면 /", () => {
  for (const raw of [null, undefined, "", 42, {}]) assert.equal(safeNext(raw, ORIGIN), "/");
});
