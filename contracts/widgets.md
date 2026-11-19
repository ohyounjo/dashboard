# 웹앱 위젯 계약 (`web/widgets/<유형>.js`)

## 모듈 형태

```js
// web/widgets/gantt.js
export default function render(el, rows, options, app) { … return { destroy() {} }; }
```

- 셸이 `import(\`./widgets/${section.type}.js\`)`로 불러온다. `section.type`은 셸의 허용 목록(`["gantt", "kanban"]`)에 있을 때만 부른다.
- 위젯은 네트워크를 쓰지 않는다. 데이터는 셸이 `GET /api/data/{options.dataset}`로 받아 넘긴다.
- 위젯은 `el` 안에만 DOM을 만든다(`el`은 비어 있는 상태로 넘어온다). 전역 상태, 전역 이벤트, `localStorage`를 쓰지 않는다. 필요하면 `destroy()`에서 정리한다.
- 텍스트는 `textContent`로 넣는다. 설정과 데이터 값을 `innerHTML`에 넣지 않는다.
- 고정 문구는 `app.strings`(= `web/strings.js`의 객체)에서 꺼낸다. 위젯 파일에 한국어·영어 문구를 직접 쓰지 않는다.
- 색은 CSS 변수만 쓴다. 색조는 `data-tone="good|warn|bad|primary|neutral"` 속성으로 표현하고, CSS가 토큰으로 칠한다.
- 모션은 `transform`과 `opacity`만 쓰고, `app.reducedMotion`이 참이면 끈다.
- 오류(필수 열 없음 등)는 `throw new Error(app.strings.widgetColumnMissing(열))`처럼 던진다. 셸이 받아 섹션 오류 상태로 그린다.

## 인자

| 인자 | 타입 | 내용 |
|---|---|---|
| `el` | `HTMLElement` | 섹션 본문 컨테이너 |
| `rows` | `object[]` | `/api/data`의 `rows`(열 이름 → 값). date는 `"YYYY-MM-DD"` 문자열 |
| `options` | `object` | `/api/app`의 섹션 객체 그대로(`type`, `title`, `dataset` 포함. 선택 필드는 `null`) |
| `app` | `object` | 아래 표 |

`app`:

| 필드 | 내용 |
|---|---|
| `asOf` | 기준일 `"YYYY-MM-DD"`(`/api/app`의 `app.as_of`). 간트 오늘 선 |
| `strings` | `web/strings.js` 기본 내보내기 객체 |
| `reducedMotion` | `prefers-reduced-motion: reduce`이면 `true` |
| `truncated` | `/api/data`가 행을 잘랐으면 `true`(위젯은 안내 한 줄을 표시) |

## gantt (`options` = `s_gantt`)

- 행 하나 = 막대 하나. 정렬은 `options.sort ?? fields.start` 오름차순, 같으면 원래 순서.
- 축 범위는 `options.range`, 없으면 행의 min(start) ~ max(end)다. 월 눈금을 그린다(월 라벨은 `app.strings.monthLabel(m)`).
- 막대: start~end. `fields.progress`가 있으면 안쪽 채움(0~100으로 자른다).
- 라벨: `[statuses[s].icon] <label>` / 보조 줄 `MM.DD–MM.DD · <statuses[s].label> <progress>%`(없는 항목은 생략).
- 색조: `statuses[s].tone ?? "neutral"`. reference/index.html 기준 done→good, delayed→warn, 나머지→primary.
- 오늘 선: `app.asOf`가 범위 안이면 세로선. 첫 줄에만 `app.strings.today(asOf)` 라벨.
- start > end이거나 날짜가 아니면 그 행은 건너뛰고 `console.warn`.

## kanban (`options` = `s_kanban`)

- 칸 = `options.columns` 순서. 카드 = `group_by` 값이 같은 행. 칸 머리글에 `label`과 카드 수를 표시한다.
- 카드: 제목 `[columns[i].icon] <row[card.title]>`, 칩 = `card.chips`의 각 열 값(값이 null이면 생략).
- 빈 칸: `app.strings.kanbanEmpty`.
- `options.filter`가 있으면 칸반 위에 `<select>`(첫 항목 `app.strings.all`, 나머지는 `row[filter.column]` 고유값 정렬)를 둔다. 바꾸면 위젯 안에서 다시 그린다.
- 드래그 없음(`draggable="false"`).
- 어느 칸에도 없는 `group_by` 값의 카드는 표시하지 않고 `console.warn` 한 번.

## 새 위젯 유형 추가(확장)

1. `web/widgets/<유형>.js`를 이 계약대로 만든다.
2. `contracts/dashboard.schema.json`의 `section`에 유형과 옵션 스키마를 더한다.
3. `dashkit/config.py`의 위젯 데이터셋·열 참조 수집에 유형을 더한다.
4. 셸의 허용 목록에 더한다.
