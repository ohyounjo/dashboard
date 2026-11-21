// 미리보기(웹앱 iframe)를 이 주소에서 띄울 수 있는지 본다. 순수 함수(tests/js/studio-preview.test.mjs).
//
// 웹앱은 Studio 주소(STUDIO_ORIGIN, make studio) 하나만 frame-ancestors로 허용하고, 로그인 쿠키는 SameSite=Lax라
// 다른 사이트의 틀 안에서는 실리지 않는다. 그래서 Studio를 다른 주소(예: 127.0.0.1)로 열면 미리보기가 빈 칸이 된다.

/**
 * Studio 화면이 열린 출처(pageOrigin)가 서버가 알려 준 Studio 주소(studioUrl)와 다르면 그 주소의 출처를 돌려준다.
 * 같거나, Studio 주소를 모르거나 읽을 수 없으면 null(미리보기를 그대로 띄운다).
 */
export function previewHome(pageOrigin, studioUrl) {
  if (typeof studioUrl !== "string" || !studioUrl) return null;
  let want, have;
  try {
    want = new URL(studioUrl).origin;
    have = new URL(pageOrigin).origin;
  } catch (e) {
    return null;
  }
  if (want === "null" || have === "null") return null;
  return want === have ? null : want;
}
