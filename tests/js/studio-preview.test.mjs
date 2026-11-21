// web/studio/preview.js의 순수 함수(node --test). make test-unit이 node 컨테이너에서 돌린다.
// 웹앱은 STUDIO_ORIGIN 하나만 frame-ancestors로 허용하고, 로그인 쿠키(SameSite=Lax)는 다른 사이트의 틀 안에서 실리지 않는다.
// 그래서 Studio를 다른 주소(예: 127.0.0.1)로 열면 미리보기가 빈 칸이 된다. 그때 바른 주소를 알려 준다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { previewHome } from "../../web/studio/preview.js";

test("Studio 주소와 같은 출처에서 열었으면 null", () => {
  assert.equal(previewHome("http://localhost:8090", "http://localhost:8090"), null);
  assert.equal(previewHome("http://localhost:8090", "http://LOCALHOST:8090/"), null);
  assert.equal(previewHome("https://studio.internal", "https://studio.internal:443"), null);
});

test("다른 호스트 이름·포트·스킴이면 Studio 주소의 출처를 돌려준다", () => {
  assert.equal(previewHome("http://127.0.0.1:8090", "http://localhost:8090"), "http://localhost:8090");
  assert.equal(previewHome("http://localhost:18090", "http://localhost:8090/"), "http://localhost:8090");
  assert.equal(previewHome("https://localhost:8090", "http://localhost:8090"), "http://localhost:8090");
});

test("Studio 주소를 모르거나 읽을 수 없으면 막지 않는다(null)", () => {
  for (const url of [undefined, null, "", "not a url", 42]) assert.equal(previewHome("http://127.0.0.1:8090", url), null, String(url));
  assert.equal(previewHome("null", "http://localhost:8090"), null);
});
