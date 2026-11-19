# 웹앱 위젯 계약 (`web/widgets/<유형>/`)

위젯 유형 하나는 폴더 하나다.

```
web/widgets/<유형>/
├─ widget.js      # 그리는 모듈(아래 "모듈 형태")
└─ widget.json    # 매니페스트: 유형 이름, 표시 이름, 섹션 옵션 JSON Schema(아래 "매니페스트")
```

`superset` 섹션은 위젯이 아니다(계약 스키마 `s_superset`에 직접 정의돼 있다).

## 모듈 형태

```js
// web/widgets/gantt/widget.js
export default function render(el, rows, options, app) { … return { destroy() {} }; }
```

- 셸이 `import(\`./widgets/${section.type}/widget.js\`)`로 불러온다. `section.type`이 `/api/app`의 `widgets[].type` 중 하나와 정확히 같을 때만 부른다(셸에 고정 목록은 없다).
- 모듈은 다른 파일을 상대 경로로 import하지 않는다(필요한 것은 `app` 인자로 받는다).
- 위젯은 네트워크를 쓰지 않는다. 데이터는 셸이 `GET /api/data/{options.dataset}`로 받아 넘긴다. 매니페스트에 `dataset` 옵션이 없는 유형은 데이터를 받지 않고 `rows`가 빈 배열이다.
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

## 매니페스트 (`widget.json`)

```json
{
  "type": "kanban",
  "label": "칸반",
  "options": {
    "type": "object",
    "required": ["dataset", "group_by", "columns", "card"],
    "properties": {
      "dataset": { "$ref": "#/$defs/key" },
      "group_by": { "$ref": "#/$defs/column", "x-column": true },
      "columns": { "type": "array", "items": { "…": "…" }, "x-unique": { "key": "value", "name": "칸" } },
      "filter": { "type": "object", "…": "…", "default": null }
    }
  }
}
```

| 키 | 내용 |
|---|---|
| `type` | 유형 이름 = `dashboard.yaml`의 섹션 `type` = 폴더 이름. `^[a-z][a-z0-9_]*$`. `superset`은 쓸 수 없다 |
| `label` | 표시 이름(`/api/app`의 `widgets[].label`) |
| `options` | 섹션 옵션의 JSON Schema(2020-12). `type: "object"`와 `properties`가 있어야 한다 |

이 세 키 말고는 쓸 수 없다. `dashkit/widgets.py`가 `web/widgets/*/widget.json`(환경변수 `WIDGETS_DIR`로 위치 변경)을 프로세스 시작 때 읽어 아래를 만든다. 매니페스트가 잘못되면 `<유형>/widget.json: <이유>`로 설정 로딩이 실패한다.

1. **섹션 스키마.** `options`에 공통 키 `type`, `title`(문자열, 선택)과 `additionalProperties: false`를 더해 `$defs.s_<유형>`으로 넣고, `section`의 유형 enum과 분기를 채운다. 합성 결과는 `config.load_schema("dashboard.schema.json")`으로 볼 수 있다. `options` 안에서는 계약 스키마의 정의를 `#/$defs/<이름>`으로 가리킬 수 있다(`key`, `column`, `date`, `tone`, `icon`, `status_map` 등).
2. **기본값.** 설정에 없는 키를 스키마의 `default`로 채운다(중첩 객체와 배열 항목 포함). `title`은 항상 `null`로 채운다. 최상위 선택 옵션에는 `default`가 반드시 있어야 한다(`/api/app`의 섹션에 키가 항상 있도록. 보통 `null`).
3. **데이터셋.** `dataset` 옵션(있으면 `required`여야 한다)이 가리키는 데이터셋이 `datasets`에 있는지 검사하고 `/api/data` 허용 목록에 넣는다.
4. **열 참조와 검사.** 아래 표시를 따른다.

표시(스키마 노드에 적는 확장 키워드. JSON Schema 검증에는 영향이 없다):

| 표시 | 붙이는 곳 | 뜻 |
|---|---|---|
| `"x-column": true` | 문자열 값 | 이 값은 `dataset` 뷰의 열 이름이다. 열 참조 검사(`contracts/README.md`) 대상이 된다. `dataset` 옵션이 있어야 쓸 수 있다 |
| `"x-unique": "<키>"` 또는 `{ "key": "<키>", "name": "<항목 이름>" }` | 객체 배열 | 항목의 `<키>` 값이 배열 안에서 유일해야 한다. 오류: `<항목 이름> <키>가 중복됨 (<값>, …)`(`name` 기본값 `항목`) |
| `"x-date-range": ["<시작 키>", "<끝 키>"]` | 객체 | 두 값이 날짜이고 시작 ≤ 끝이어야 한다. 오류: `날짜 형식 오류`, `<시작 키>가 <끝 키>보다 늦음` |

- `default`와 표시는 매니페스트 안에 직접 적은 것만 본다. `properties`, `additionalProperties`, `items`를 따라 내려가고 `$ref` 너머와 `oneOf` 같은 조합 키워드 안쪽은 보지 않는다.
- 열 참조의 수집 순서는 매니페스트의 선언 순서다.

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

`web/widgets/<유형>/` 폴더 하나를 만든다. 다른 파일은 고치지 않는다.

1. `widget.json`: 위 "매니페스트" 형식으로 유형 이름, 표시 이름, 옵션 스키마를 적는다. 열 이름을 받는 옵션에는 `"x-column": true`, 선택 옵션에는 `default`를 적는다.
2. `widget.js`: 위 "모듈 형태" 계약대로 만든다.

- 확인: `make check`(매니페스트 형식과 그 유형을 쓴 설정), `make test-unit`(`tests/test_widgets.py`가 저장소의 모든 위젯 폴더를 읽는다).
- backend 이미지는 `widget.json`만 담는다. 매니페스트를 바꾸면 이미지를 다시 빌드한다(`make apply`). `widget.js`는 web 컨테이너가 `web/`을 그대로 서빙한다.
- 문구는 `web/strings.js`, 모양은 `web/styles.css`의 `.widget-<유형>` 아래에 둔다(셸이 본문 컨테이너에 이 클래스를 붙인다). 이 둘은 아직 폴더 밖이다.
