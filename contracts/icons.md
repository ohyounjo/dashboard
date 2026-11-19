# 탭 아이콘 이름

`dashboard.yaml`의 `tabs[].icon`에 쓰는 이름이다. 웹앱(`web/icons.js`)에 단색 SVG로 내장하고 `currentColor`로 칠한다. 목록을 바꾸면 이 파일, `dashboard.schema.json`의 `$defs.icon`, `web/icons.js`를 함께 고친다.

| 이름 | 모양 |
|---|---|
| `grid` | 2×2 사각형 |
| `calendar` | 달력 |
| `alert` | 삼각형 느낌표 |
| `doc` | 문서 |
| `chart` | 막대 차트 |
| `table` | 표 |
| `users` | 사람 두 명 |
| `box` | 상자 |
| `list` | 목록 |
| `flag` | 깃발 |
| `target` | 과녁 |
| `cart` | 장바구니 |
| `megaphone` | 확성기 |
| `home` | 집 |

이름은 모양을 뜻할 뿐 특정 도메인을 뜻하지 않는다.
