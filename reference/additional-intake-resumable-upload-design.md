# 추가 접수 방식과 중단된 업로드 이어 올리기 설계 (#42)

> 대상: [이슈 #42](https://github.com/ohyounjo/dashboard/issues/42) (`[6] 추가 접수 방식과 이어 올리기`)
> 기준 커밋: `main` (`15aab80`)
> 상위 설계 기준: [데이터 설계서](data-design.md) 5-3, 5-4, 5-8, 10-3, 10-4, 15절
> 관련 계약: [`contracts/ingest.md`](../contracts/ingest.md) · [`contracts/manage.md`](../contracts/manage.md) · [`contracts/studio.md`](../contracts/studio.md) · [`contracts/auth.md`](../contracts/auth.md) · [`contracts/scaffold.md`](../contracts/scaffold.md)

---

## 1. 배경과 설계 범위

현재 접수(`dashkit/ingest/intake.py`)는 `csv`(UTF-8/CP949, 쉼표·탭·세미콜론 자동 판별)와 `rows`(JSON 행 목록) 두 가지 파서를 제공하며, 파일 업로드는 단일 `multipart/form-data` 요청으로 처리한다. 6단계(후순위) 이슈 #42는 실무에서 수요가 발생하는 네 가지 입력·전송 방식을 접수 단계에 확장한다.

| 하위 기능 | 핵심 목표 | 난이도 / 영향도 | 설계 원칙 |
|---|---|---|---|
| **1) `xlsx` 파서** | 엑셀 통합 문서(`.xlsx`)를 CSV로 변환하지 않고 그대로 접수·추론·적재 | 중 / 낮음 | ① 외부 패키지 의존성 없이 Python 표준 라이브러리(`zipfile`, `xml.etree.ElementTree`) 스트리밍(`iterparse`)으로 구현하거나 ZIP 폭탄·XXE·메모리 상한을 엄격히 통제<br>② `PARSERS["xlsx"]`로 등록하고 `header() -> list[str]`, `rows() -> Iterator[(위치, 값들)]` 인터페이스 준수<br>③ 날짜·시각·참거짓·숫자·공유 문자열·인라인 문자열을 정규형에 맞는 문자열로 복원해 기존 `Intake` 파이프라인에 합류 |
| **2) 원본 DB에서 가져오기** | 외부 PostgreSQL(또는 읽기 전용 DB) 쿼리 결과를 파일로 내보내지 않고 스트리밍으로 읽어 접수에 전달(`make ingest-db` / `python -m dashkit ingest-db`) | 중 / 낮음 | ① `PARSERS["cursor"]`(또는 스트리밍 행 이터레이터)를 통해 행을 메모리에 모두 올리지 않고 `ingest()`에 전달<br>② 외부 DB 접속 DSN·비밀번호는 **환경 변수 이름(`--dsn-env`, 기본 `SOURCE_DB_DSN`)** 으로만 받고 CLI 인자·코드·작업 로그·감사 로그·오류 메시지에 절대 남기지 않음<br>③ 외부 DB 접속은 `default_transaction_read_only=on` 읽기 전용 세션으로만 열며, 단일 `SELECT` 쿼리 파일 또는 테이블 조회만 허용 |
| **3) 감시 폴더 (Watch Folder)** | 지정된 입고 폴더(`data/_inbox/<테이블>/` 또는 `--dir`)에 떨어지는 `.csv`·`.csv.gz`·`.xlsx` 파일을 감지·소비해 배치로 적재(`make ingest-watch` / `python -m dashkit ingest-watch`) | 중 / 낮음 | ① 쓰기 중인 불완전 파일을 읽지 않도록 `.`·`_`·`.part`·`.tmp` 제외 및 크기·수정 시각 안정성(quiescence) 확인 후 처리<br>② 파일마다 `ingest()`를 호출해 기존 다듬기·검증·중복 감지·쓰기 잠금·원자적 확정을 100% 재사용<br>③ 성공 파일은 `_done/`, 실패 파일은 `_failed/`(+ `.error.json` 사유, 민감 열 값 마스킹)로 원자적 이동하여 무한 재시도와 원본 방치를 방지 |
| **4) 중단된 업로드 이어 올리기 (Resumable Upload)** | 불안정한 네트워크에서 대용량 파일(최대 `INGEST_MAX_UPLOAD_MB`) 업로드가 끊겼을 때 처음부터 다시 올리지 않고 중단된 바이트 오프셋부터 이어 올리기 | 중~상 / 중간 | ① 세션 생성(`POST /uploads`) → 조각 전송(`PATCH /uploads/{id}`) → 상태 조회(`GET /uploads/{id}`) → 취소(`DELETE /uploads/{id}`) → 미리보기·적재 확정(`upload_id` 연동) 계약 정의<br>② 오프셋 불일치(409 `offset_mismatch`), 이미 받은 중복 조각(멱등 응답), 동시 PATCH 직렬화(409 `upload_in_progress`), 크기 초과(413 `upload_too_large`), 만료(410 `upload_expired`), 최종 `sha256` 무결성 검증(422 `checksum_mismatch`)을 명문화<br>③ 조각 데이터는 디스크 임시 파일 하나에 순차 기록하고 만료·취소·확정 시 즉시 정리 |

### 공통 불변 제약 (절대 유지)

1. **로더 불변 및 정규형 단일 경로:** 어떤 입력 방식으로 들어오든 접수 단계(`Intake`)를 거쳐 **`data/<테이블>/<번호>-<UTC 시각>.csv.gz` 정규형 CSV**로만 저장된다. `dashkit/loader/`와 `dashkit/ingest/apply.py`에는 입력 방식별 분기를一切 추가하지 않는다.
2. **동일 입력 = 동일 정규형 배치 & 동일 재생성 결과:** 같은 데이터를 CSV, JSON `rows`, `xlsx`, 외부 DB 가져오기, 감시 폴더, 이어 올리기로 넣었을 때 만들어지는 정규형 배치의 내용 해시(`sha256`)와 `make apply`(`load`) 재생성 결과가 100% 일치해야 한다.
3. **계정 및 비밀값 분리:** 읽기 경로는 `dash_inspect` 또는 `dash_reader`만 사용하고, `dash_owner`는 쓰기 작업(`ingest`)에서만 연다. 외부 DB 자격 증명과 토큰은 환경 변수로만 받고 코드·커밋·로그·오류 응답·감사 로그에 남기지 않는다.
4. **단계적·독립 도입 가능:** 네 가지 하위 기능은 공통 접수(`PARSERS`)와 업로드 저장소(`dashkit/manage/uploads.py`) 위에서 서로 결합 없이 분리되어 있어, 실제 수요가 발생한 항목부터 개별 PR 또는 통합 PR로 독립 구현할 수 있다.

---

## 2. 하위 기능 1: `xlsx` 파서 상세 설계

### 2-1. 구현 방식 및 보안·자원 방어 (표준 라이브러리 기반 스트리밍)

새로운 외부 서드파티 패키지를 컨테이너 이미지(`app`, `studio`)에 추가하지 않고도 동작하며 보안 경계를 직접 통제할 수 있도록, Python 표준 라이브러리(`zipfile` + `xml.etree.ElementTree.iterparse`)만으로 Strict/Transitional OOXML(`.xlsx`) 리더를 구현한다 (`dashkit/ingest/xlsx.py`).

- **지원 형식:** `.xlsx` (Office Open XML SpreadsheetML). 암호화된 통합 문서, 구형 바이너리 `.xls`, 매크로 포함 `.xlsm`은 `422 invalid_xlsx`("암호화되지 않은 .xlsx 파일만 지원합니다")로 명확히 거절한다.
- **ZIP 폭탄 및 악성 XML 방어 (`XlsxLimits`):**
  1. **압축 해제 크기 상한:** ZIP 내부 엔트리들의 비압축 크기 합계(`sum(zinfo.file_size)`)와 실제 읽은 바이트 합계를 `MAX_XLSX_UNCOMPRESSED_BYTES`(기본 `512 * 1024 * 1024`, 512MiB) 및 압축률 상한(예: 100배)으로 제한한다. 초과 시 즉시 읽기를 중단하고 `IntakeError("invalid_csv", "<파일>:1:-: xlsx 압축 해제 크기가 한도를 넘음")`로 거절한다.
  2. **XML 엔티티 확장(Billion Laughs / XXE) 차단:** Python 3.12의 `xml.etree.ElementTree`(내부 `expat`)는 기본적으로 외부 엔티티를 해석하지 않으며, 추가로 XML 선언에 `<!DOCTYPE` 또는 `<!ENTITY`가 포함된 엔트리는 스트림 앞단에서 감지해 즉시 `invalid_csv`로 거절한다.
  3. **공유 문자열(`xl/sharedStrings.xml`) 메모리 상한:** 공유 문자열 항목별 길이는 `MAX_CELL_CHARS`(10MiB), 전체 고유 문자열 합계는 `MAX_SHARED_STRINGS_BYTES`(기본 128MiB)로 제한한다.
  4. **스트리밍 행 읽기:** 워크시트 XML(`xl/worksheets/sheet*.xml`)은 전체 DOM을 메모리에 올리지 않고 `ET.iterparse(stream, events=("start", "end"))`로 `<row>` 요소를 하나씩 읽은 뒤 즉시 `elem.clear()`로 메모리에서 해제한다.

### 2-2. 시트 선택과 셀 값 복원 규칙

1. **시트 선택 (`sheet: str | int | None = None`):**
   - `xl/workbook.xml`과 `xl/_rels/workbook.xml.rels`를 읽어 시트 목록(`[(이름, 타깃 경로, state)]`)을 구한다.
   - `sheet`를 지정하지 않으면(`None`) **숨겨지지 않은(`state != "hidden"` / `"veryHidden"`) 첫 번째 시트**를 읽는다(모든 시트가 숨김이면 첫 번째 시트).
   - `sheet`를 시트 이름(`str`)이나 0-기반 번호(`int`)로 지정할 수 있으며, 해당 시트가 없으면 `IntakeError("invalid_csv", "시트를 찾을 수 없습니다", [f"{self.name}:1:-: 시트 '{sheet}'가 없음"])`를 낸다.
2. **열 좌표(`r="C5"`)와 희소 셀(Sparse Cells) 정렬:**
   - 엑셀 XML은 빈 셀을 생략하거나 `r="A1"`, `r="C1"`처럼 건너뛰어 기록할 수 있다.
   - 열 문자(`A` → 0, `Z` → 25, `AA` → 26)를 0-기반 열 인덱스로 변환해 빠진 중간 셀을 빈 문자열(`""`)로 채운다. `r` 속성이 생략된 셀은 직전 셀의 다음 열 인덱스를 부여한다.
   - 머리글은 **비어 있지 않은 첫 번째 행**으로 삼고(그 앞의 완전히 빈 행은 건너뛴다), 머리글 행의 실제 엑셀 행 번호(예: 1행)를 기록한다.
   - 데이터 행에서 머리글 너비(`width`)보다 오른쪽에 있는 빈 셀들은 무시하고, 머리글 너비보다 오른쪽에 **값이 있는 셀**이 나타나면 `CsvParser`와 동일하게 `RowError(self.where(row_num, "-"), f"값 개수 {len(record)}개가 머리글 {width}개와 다름", raw=record)`를 내놓는다. 머리글 너비보다 짧은 행은 오른쪽을 `""`로 패딩한다. Completely 빈 행(모든 셀이 공백/빈 문자열)은 `CsvParser`와 동일하게 건너뛴다.
3. **셀 유형(`t` 속성)과 서식(`s` 속성 → `xl/styles.xml`) 복원:**

| 셀 `t` 속성 / 서식 | XML 표현 | 복원 문자열 (`list[str]`) | 비고 |
|---|---|---|---|
| `t="s"` (Shared String) | `<v>0</v>` | `shared_strings[int(v)]` | `<si>` 안의 `<t>` 또는 서식 있는 `<r><t>` 조각들을 이어 붙임(루비 주석 `<rPh>`는 제외) |
| `t="inlineStr"` / `t="str"` | `<is><t>…</t></is>` 또는 `<v>…</v>` | 텍스트 그대로 | 수식 문자열 결과(`t="str"`)도 `<v>` 값을 사용 |
| `t="b"` (Boolean) | `<v>1</v>` / `<v>0</v>` | `"true"` / `"false"` | 정규형 boolean 표기로 즉시 복원 |
| `t="e"` (Formula Error) | `<v>#DIV/0!</v>` 등 | 원문 그대로(예: `"#DIV/0!"`) 또는 오류 | `RowChecker`가 대상 열 유형 검증에서 명확히 오류로 잡아내도록 함(또는 `RowError`로 `"엑셀 수식 오류 '#DIV/0!'"` 보고) |
| `t="d"` (ISO 8601 날짜) | `<v>2026-11-20</v>` | 텍스트 그대로 | Strict OOXML 날짜 |
| `t="n"` 또는 `t` 생략 (숫자 / 날짜 일련번호) | `<v>46346</v>` 또는 `<v>1234.5</v>` | - **날짜·시각 서식(`numFmtId`):** 엑셀 일련번호를 ISO 날짜(`YYYY-MM-DD`) 또는 ISO 시각(`YYYY-MM-DDTHH:MM:SS`)으로 변환<br>- **일반 숫자:** 정수부만 있거나 부동소수점 오차 없는 `Decimal` 문자열 | 아래 날짜 일련번호 규칙 참고 |

4. **엑셀 날짜·시각 일련번호(Serial Number) 변환 규칙:**
   - `xl/styles.xml`의 `<cellXfs>` → `numFmtId`를 조회한다. 표준 날짜·시각 서식 ID(`14`~`22`, `27`~`36`, `45`~`47`, `50`~`58`)이거나 사용자 정의 `formatCode`에서 따옴표(`"..."`)·이스케이프(`\x`)·색상(`[Red]`)을 제거한 뒤 숫자 기호(`0`, `#`, `?`) 없이 날짜·시각 토큰(`y`, `m`, `d`, `h`, `s`)이 포함된 경우 날짜·시각 서식으로 판정한다.
   - 단, 대상 테이블의 스키마 열이 `integer`나 `number`인데 엑셀에서 실수로 날짜 서식이 입혀진 경우까지 깨지지 않도록, `XlsxParser`는 셀 값을 복원할 때 날짜 서식이면 ISO 문자열로 복원하되, **1900년 윤년 버그(일련번호 60 = 1900-02-29, 1900-03-01 이후는 `1899-12-30 + timedelta(days=serial)`)** 와 `workbookPr date1904="1"`(`1904-01-01 + timedelta(days=serial)`)을 모두 정확히 처리한다.
   - 시·분·초 토큰(`h`, `s`)이 없고 소수부가 0이면 `YYYY-MM-DD`, 시각이 포함되면 초 단위 반올림 후 `YYYY-MM-DDTHH:MM:SS`로 변환한다.

### 2-3. `PARSERS` 인터페이스 및 오류 위치

```python
class XlsxParser(Parser):
    kind = "xlsx"
    encoding = "xlsx"
    delimiter = None

    def __init__(self, path: Path, name: str, *, strict: bool = False, sheet: str | int | None = None):
        ...
    def header(self) -> list[str]: ...
    def rows(self) -> Iterator[tuple[int, list[str] | RowError]]: ...
    def where(self, at: int, column: str) -> str:
        # 단일 시트 기본이면 "<파일 이름>:<엑셀 행 번호>:<열>", 시트를 지정했거나 다중 시트면 필요 시 유지
        return f"{self.name}:{at}:{column}"
```

- `at`은 엑셀 시트의 실제 1-기반 행 번호(`r` 속성 기준, 머리글이 1행이면 첫 데이터 행은 2)이므로 사용자가 엑셀에서 열었을 때 줄 번호가 정확히 일치한다.
- `PARSERS["xlsx"] = XlsxParser`로 등록한다.
- **`make_parser` (`dashkit/ingest/add.py`):**
  - `source["kind"] == "xlsx"`를 지원하고, `source["kind"] == "csv"`나 `"file"`이더라도 파일 확장자가 `.xlsx`(대소문자 무시)이면 `PARSERS["xlsx"]`를 선택할 수 있게 한다(`strict=True`로 로더가 정규형 배치를 읽을 때는 언제나 `CsvParser`만 사용한다).
- **업로드 파일 이름 검증 (`dashkit/project/upload.py`):**
  - Studio의 `replace` 원본 CSV 업로드(`check_filename`)는 기존대로 `.csv`만 허용한다(프로젝트 폴더의 원본은 CSV이기 때문).
  - 관리 서비스(`dashkit/manage/routes/tables.py`, `structure.py`의 `POST /tables/{t}/batches`, `POST /batches`, `POST /tables/infer`)와 CLI(`python -m dashkit ingest`)는 `upload.check_ingest_filename(name)`을 통해 **`<이름>.csv`와 `<이름>.xlsx`를 모두 허용**한다.
  - `POST /tables/infer`(`dashkit/scaffold.py` 추론)에 `.xlsx` 파일을 올리면 `XlsxParser`로 머리글과 표본 행을 읽어 기존 CSV 추론과 동일한 스키마·데이터셋·초안 그래프 추론 결과를 만들고, `POST /tables` 확정 시에는 기존과 동일하게 정규형 첫 파일(`append`는 `000001-….csv.gz`, `replace`는 `data/<테이블>.csv`)로 저장한다.

---

## 3. 하위 기능 2: 원본 DB에서 가져오기 (`ingest-db`) 상세 설계

### 3-1. 아키텍처와 보안 원칙 (자격 증명 무노출)

외부 원본 DB(예: 운영 서비스의 읽기 복제본 PostgreSQL)에서 데이터를 읽어 대시보드 프로젝트의 `append` 테이블에 배치로 넣는 CLI 명령을 추가한다.

```
외부 원본 DB (읽기 전용 접속, 서버사이드 커서)
  │  (DSN은 환경 변수 SOURCE_DB_DSN 에서만 읽음)
  ▼
CursorParser (PARSERS["cursor"])
  │  header() -> SELECT 결과 열 이름 목록
  │  rows()   -> (1-기반 행 번호, 문자열로 바꾼 칸 목록) 스트리밍
  ▼
Intake (머리글 맞추기 → 값 다듬기 → 한 줄씩 검증)
  ▼
stage_batch / apply_batch / finish (정규형 .csv.gz 배치 저장 + dashkit_meta.batch 기록)
```

- **자격 증명 보안 규칙 (완료 조건 3):**
  1. CLI 인자로 접속 문자열(DSN)이나 비밀번호 값을 직접 받는 옵션(`--dsn`, `--password`)을 **두지 않는다.**(`ps aux`, 셸 히스토리, CI 로그 노출 원천 차단)
  2. 대신 **환경 변수 이름**만 받는 `--dsn-env <ENV_NAME>`(기본값: `SOURCE_DB_DSN`)을 사용한다. 환경 변수 이름 자체는 `^[A-Z_][A-Z0-9_]{0,127}$` 형식만 허용한다.
  3. 외부 DB 접속 오류(`psycopg.Error`)가 발생할 때 `conninfo`, 호스트, 사용자, 비밀번호가 메시지에 섞이지 않도록 `sanitize_db_error(exc, dsn_value)`로 환경 변수 원문과 URI 비밀번호(`postgresql://user:pass@host` → `***`)를 마스킹하고 오류 유형과 SQLSTATE/안전한 요약만 출력한다.
  4. 감사 로그(`data.batch.create` / `data.batch.failed`)의 `method`는 `"db"`, `source_name`은 `--source-name`(지정하지 않으면 `"db:<테이블 또는 쿼리 파일명>"`)으로만 기록하고, 접속 정보나 실행 SQL 전문(리터럴에 민감 값이 있을 수 있음)은 감사 로그에 넣지 않는다.

### 3-2. 외부 DB 읽기 전용 격리 및 쿼리 규칙

1. **읽기 전용 세션 강제:**
   - 외부 DB에 접속할 때 연결 직후 `conn.read_only = True` (`SET default_transaction_read_only = on`) 및 `statement_timeout`(기본 `SOURCE_DB_TIMEOUT_MS`, 기본값 `300000`ms = 5분)을 설정한다.
   - 설령 사용자가 실수로 쓰기 권한이 있는 외부 DB 계정을 환경 변수에 넣었더라도, 읽기 전용 트랜잭션과 아래 쿼리 검사로 인해 외부 DB에 `INSERT`/`UPDATE`/`DELETE`/`DDL`이 실행될 수 없다.
2. **쿼리 지정 방식 (둘 중 하나 필수):**
   - `--source-table [<스키마>.]<테이블>`: 식별자 형식(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)을 검증한 뒤 `psycopg.sql.Identifier`로 `SELECT * FROM < 테이블 >`을 구성한다.
   - `--query-file <SQL 파일 경로>` (또는 `--query <SELECT 문>`): 세미콜론으로 구분된 복수 문장을 금지하고, `SELECT * FROM (<쿼리>) AS _dashkit_src`로 감싸 실행한다(감싸는 것만으로 `DDL`이나 최상위 `INSERT`/`UPDATE`/`DELETE`가 구문 오류로 차단되며 `read_only = True`가 쓰기 CTE까지 이중 차단한다).
3. **스트리밍 추출 (`CursorParser`, `PARSERS["cursor"]`):**
   - 서버사이드 커서(또는 배치를 나눠 읽는 이터레이터, `itersize = 2000`)로 결과를 읽는다.
   - `header() -> list[str]`: `cur.description`의 열 이름(`[col.name.strip() for col in cur.description]`). 열이 없거나 이름이 모두 비어 있으면 `IntakeError("invalid_rows", "쿼리 결과에 열이 없습니다")`.
   - 각 셀 값(`Any`)을 접수용 문자열(`str`)로 변환하는 `_db_cell(val)` 규칙:
     - `None` → `""` (NULL)
     - `bool` → `"true"` / `"false"`
     - `int`, `Decimal` → `str(val)` (`Decimal`은 지수 표기 없는 `format(val, "f")` 또는 정규 표기)
     - `float` → 유한 수가 아니면(`nan`, `inf`) 원문 문자열로 넘겨 검증기가 거절하게 하고, 유한 수면 `repr(val)`
     - `datetime` → `val.isoformat()`
     - `date` → `val.isoformat()`
     - `str` → `val`
     - `UUID` → `str(val)`
     - `bytes`, `bytearray`, `memoryview` → UTF-8 디코딩 시도, 실패 시 `RowError(where, "바이너리(bytea) 값은 적재할 수 없음")`
     - `list`, `dict` (JSON/배열 열) → `RowError(where, "값은 스칼라(문자열·숫자·참거짓·날짜·null)만 받음")`
   - `where(at, column)`은 `f"{self.name}:{at}:{column}"`(예: `db:orders.sql:15:amount`)을 반환한다(`at`은 1부터 시작하는 결과 행 번호).

### 3-3. CLI 명령 인터페이스

```bash
python -m dashkit ingest-db \
  --table <대상 append 테이블> \
  [--dsn-env SOURCE_DB_DSN] \
  (--source-table <원본 테이블> | --query-file <SQL 파일>) \
  [--columns '{"원본열": "대상열"}'] \
  [--source-name <표시 이름>] \
  [--dry-run] [--force] [--skip-invalid]
```

- `Makefile` 타깃: `make ingest-db TABLE=<테이블> [SOURCE_TABLE=<원본>] [QUERY_FILE=<파일>] [DSN_ENV=SOURCE_DB_DSN] [DRY_RUN=1] [SKIP_INVALID=1] [FORCE=1]`
- 기존 `ingest()` 함수를 그대로 호출하므로 머리글 자동 맞춤(`aliases`, `labels`, 느슨한 비교), 값 다듬기(`NORMALIZERS`), 미리보기(`--dry-run`), 오류 행 빼고 넣기(`--skip-invalid`), 같은 내용 해시 중복 감지(`409 duplicate_file`), `checks` 검증, 원자적 확정과 복구가 CSV/JSON 적재와 100% 동일하게 동작한다.

---

## 4. 하위 기능 3: 감시 폴더 (`ingest-watch`) 상세 설계

### 4-1. 폴더 구조와 파일 수명주기

외부 시스템(SFTP, 공유 볼륨, 배치 스크립트 등)이 파일을 폴더에 넣어 두면 자동으로 접수해 배치로 적재하는 감시 폴더 방식을 정의한다.

```
<감시 루트 폴더>/                        # 기본: projects/<PROJECT>/data/_inbox/ 또는 --dir <경로>
└─ <테이블 이름>/                        # schema.yaml의 append 테이블 이름과 일치
   ├─ 20261122-orders.csv                # 대기 중인 입고 파일 (.csv, .csv.gz, .xlsx)
   ├─ .uploading.csv.part                # 무시: '.' 또는 '_'로 시작하거나 .part / .tmp 확장자
   ├─ _processing/                       # 현재 적재 중인 파일 (중단 시 시작/스캔 복구가 회수)
   ├─ _done/                             # 적재 성공한 원본 파일 보관 (또는 --delete-done 시 삭제)
   │  └─ 20261122T130500Z-20261122-orders.csv
   └─ _failed/                           # 적재 실패한 원본 파일 + 오류 사유
      ├─ 20261122T130500Z-bad.csv
      └─ 20261122T130500Z-bad.csv.error.json
```

- 기본 감시 루트 `projects/<PROJECT>/data/_inbox/`는 `_`로 시작하므로 기존 로더(`dashkit/loader/`)와 배치 목록(`batches.list_batches`)이 테이블이나 배치로 읽지 않는다(기존 규칙: `_`나 `.`으로 시작하는 이름은 무시).
- 또는 `--dir /path/to/inbox --table <테이블>`로 특정 테이블의 단일 폴더를 감시할 수도 있다.

### 4-2. 안전한 파일 소비 규칙 (불완전 파일 방지·재시작 복구·원자적 격리·중복 방지)

1. **대상 파일 필터링:**
   - 파일 이름이 `.` 또는 `_`로 시작하거나, 확장자가 `.tmp`, `.part`, `.crdownload`, `.error.json`, `.lock`인 파일은 건너뛴다.
   - 허용 확장자: `.csv`, `.csv.gz`, `.xlsx` (대소문자 무시). 그 외 확장자는 건너뛰고 경고 로그를 남긴다.
   - 심볼릭 링크(`path.is_symlink()`)는 경로 이탈 방지를 위해 거절하고 `_failed/`로 격리한다.
   - 파일 크기가 `INGEST_MAX_UPLOAD_MB`를 넘으면 읽지 않고 즉시 `_failed/`로 옮기며 `upload_too_large` 오류를 기록한다.
2. **쓰기 완료(Quiescence) 확인:**
   - 외부 프로세스가 파일을 복사하는 도중에 읽는 일을 막기 위해, 권장 방식은 임시 이름(`.name.tmp`)으로 쓴 뒤 `mv`로 이름을 바꾸는 것이며, 이에 더해 감시기는 파일의 `(st_size, st_mtime_ns)`가 최소 `settle_ms`(기본 `500`ms, `--settle-ms`로 조정 가능; `--once`에서는 `mtime`이 현재 시각보다 `settle_ms` 이상 지났거나 두 번 측정해 변하지 않았는지 확인) 동안 변하지 않고 배타적 열기/읽기가 가능한 파일만 처리한다.
3. **`_processing/` 중단 파일 재시작 복구 절차:**
   - 감시기는 단일 테이블(또는 감시 루트) 당 하나의 프로세스만 돌도록 `<감시 루트>/.watch.lock` 파일 잠금(`fcntl.flock(LOCK_EX | LOCK_NB)`)을 잡는다.
   - 잠금을 잡은 직후(시작 시 및 매 스캔 주기 첫 단계) 각 테이블 폴더의 `_processing/` 안에 남아 있는 파일이 있으면 **이전 실행이 `_processing/` 이동 직후 또는 `ingest()` 도중 강제 종료된 것**이므로 새 대기 파일보다 먼저 회수해 재처리한다:
     1. `_processing/<UTC 시각>-<원래 이름>`에서 `<원래 이름>`을 복원해 `ingest()`를 실행한다(내부 `locked()`가 데이터 DB에 남은 `data/_pending/` 묶음 기록을 먼저 `recover`한다).
     2. 이전 실행이 `ingest()` 커밋(또는 `recover`의 자동 반영)까지 마친 직후 죽었다면 이번 `ingest()` 호출은 `409 duplicate_file`을 돌려준다. `_processing/` 복구 경로에서 `409 duplicate_file`이 반환되고 중복 대상 배치의 생성 시각이 해당 `_processing/` 파일 이동 시각 이후(또는 방금 복구된 배치)이면 이미 적재가 완료된 파일로 판정해 `_done/`(또는 `--delete-done` 시 삭제)으로 정상 마감한다.
     3. 아직 적재되지 않았던 파일이면 이번 `ingest()`가 정상 적재하여 `_done/`으로 옮기고, 검증 실패면 아래 5번 규칙에 따라 `_failed/`로 격리한다. 이로써 `_processing/` 이동 후 프로세스가 죽더라도 파일이 영구 누락되지 않는다.
4. **처리 순서와 `ingest()` 호출:**
   - 여러 테이블 폴더가 있으면 `config.table_order(project)`(참조 부모 테이블 먼저) 순으로, 같은 테이블 폴더 안에서는 (`_processing/` 복구 파일 먼저 → 새 대기 파일) 이름 오름차순으로 하나씩 처리한다.
   - 새 대기 파일을 처리하기 직전 같은 파일시스템 안의 `_processing/<타임스탬프>-<파일명>`으로 `os.replace` 이동해 중복 소비를 막는다.
   - `ingest(project.name, [Item(table, {"kind": kind, "path": proc_path, "name": orig_name})], ...)`를 호출한다.
5. **결과 처리, 민감 열 오류 값 마스킹 및 자원 정리:**
   - **성공 (`200`):**
     - 감사 로그에 `data.batch.create`(`method: "watch"`, `source_name: orig_name`, 배치 요약)를 남긴다.
     - `--delete-done` 옵션이 켜져 있으면 원본 파일을 삭제하고, 기본값에서는 `<테이블>/_done/<UTC 시각>-<원래 이름>`으로 옮긴다(`_done/` 보관 개수는 `--keep-done`, 기본 100개로 오래된 파일부터 정리).
   - **실패 (`IngestError`, 예: `422 invalid_rows`, `422 unknown_columns`, `409 duplicate_file`, `422 key_conflict`):**
     - 감사 로그에 `data.batch.failed`(`method: "watch"`, `source_name: orig_name`, `detail_code: exc.detail`)를 남긴다.
     - 원본 파일을 `<테이블>/_failed/<UTC 시각>-<원래 이름>`으로 옮기고, 옆에 `<같은 이름>.error.json`(`{"at", "table", "file", "status", "detail", "message", "errors"}`)을 기록한다.
     - **민감 열 오류 값 명시적 마스킹 (`redact_sensitive_errors`):** 현재 `dashkit/loader/validate.py`의 `_parse()`·`_check_value()`와 `Intake.rows()`는 검증 실패 시 원문 값(`'<raw>'`)을 오류 문자열에 포함하고 `RowError.raw`에는 원래 행 칸들을 담는다. 따라서 `schema.yaml`에서 `sensitive: true`가 설정된 열이 있는 테이블은(감시 폴더의 `.error.json` 기록 및 `Intake` 오류 보고 공통으로) `redact_sensitive_errors(table_spec, errors)`를 거쳐:
       - `<위치>:<민감 열 이름>: <이유>` 줄에서 작은따옴표로 인용된 원문 값(`'...'`)을 `'***'`로 치환하거나 제거해 위치와 이유만 남긴다(예: `bad.csv:17:phone: 형식 불일치 '***'`).
       - 열 개수 불일치 등으로 `원래 칸: ...`을 남길 때도 민감 열 위치나 전체 원문 칸 인용을 마스킹한다.
     - 실패한 파일이 `_failed/`로 격리되므로 다음 폴링 주기에 같은 실패 파일을 무한 반복 실행하지 않는다.
   - **일시적 잠금·DB 장애 (`409 apply_running`, `503 db_unavailable`):**
     - 파일 자체의 결함이 아니므로 `_failed/`로 버리지 않고 `_processing/`에서 원래 자리로 되돌려(또는 `_processing/`에 남겨 다음 주기의 3번 복구 단계에서) 다음 폴링 주기에 재시도한다(`--once` 모드에서는 종료 코드 1 반환).

### 4-3. CLI 인터페이스

```bash
# 1회 스캔 후 종료 (cron / systemd timer 연동에 적합)
python -m dashkit ingest-watch --once [--dir <경로>] [--table <테이블>] [--settle-ms 500] [--delete-done]

# 상주 폴링 모드
python -m dashkit ingest-watch [--interval 5] [--dir <경로>] [--table <테이블>] [--settle-ms 500]
```

- `Makefile` 타깃: `make ingest-watch [DIR=<폴더>] [TABLE=<테이블>] [ONCE=1]`

---

## 5. 하위 기능 4: 중단된 업로드 이어 올리기 (Resumable Upload) 계약 및 설계

### 5-1. 설계 목표와 기존 `upload_id` 구조와의 통합

현재 `dashkit/manage/uploads.py`는 파일 미리보기(`dry_run=true`)가 끝난 임시 파일을 `upload_id`로 1시간(`UPLOAD_TTL = 3600`) 보관하고, 확정 시 `POST /api/manage/tables/{t}/batches`에 `{"upload_id": "..."}`를 보내 다시 올리지 않고 적재한다.

**핵심 아이디어:**
이어 올리기를 별도의 적재 경로로 만들지 않고, **`upload_id`를 "업로드 세션(`receiving`) → 완료된 보관 파일(`ready`)"의 2단계 수명주기로 확장**한다.
즉, 조각들을 모두 이어 올려 `status: "ready"`가 된 `upload_id`는 기존 `POST /api/manage/tables/{t}/batches` (단일 파일, `dry_run: true` 미리보기 및 `dry_run: false` 확정 모두 지원), `POST /api/manage/batches` (여러 파일), `POST /api/manage/tables` (새 테이블 만들기)에 **그대로 넘겨 쓸 수 있다!**

```
[1] POST /api/manage/uploads
    {table, filename, total_size, sha256?}
       │  201 Created: {upload_id, offset: 0, total_size, chunk_max_bytes, expires_at, status: "receiving"}
       ▼
[2] PATCH /api/manage/uploads/{upload_id}   (Content-Type: application/octet-stream, Upload-Offset: <바이트>)
       │  네트워크 중단 시 → GET /api/manage/uploads/{upload_id} 로 서버의 현재 offset 확인 후 그 위치부터 재개
       │  중복 조각(offset < 서버 offset) → 이미 받은 바이트와 일치하면 현재 서버 offset을 200으로 멱등 반환
       ▼
[3] offset == total_size 도달
       │  sha256 지정 시 전체 파일 해시 대조(불일치 시 파일 폐기 + 422 checksum_mismatch)
       │  통과 시 status: "ready" 전환
       ▼
[4] POST /api/manage/tables/{t}/batches  {"upload_id": "<id>", "dry_run": true}  → 미리보기 (파일 유지)
    POST /api/manage/tables/{t}/batches  {"upload_id": "<id>", "dry_run": false} → 본 적재 확정 (성공 시 파일 해제)
```

### 5-2. 이어 올리기 API 계약 (`contracts/manage.md` 확장)

모든 경로는 `/api/manage` 아래에 두며, 세션 쿠키 기반 운용자 인증(`require_operator`) 또는 선택 기능 `tokens`가 켜졌을 때 허용된 테이블의 API 토큰(`Authorization: Bearer <토큰>`)을 따른다.

#### 1) 업로드 세션 시작: `POST /api/manage/uploads`

- **요청 본문 (`application/json`):**
  ```json
  {
    "table": "entries",
    "filename": "entries_202611.csv",
    "total_size": 52428800,
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```
  - `table`: 대상 `append` 테이블 이름(여러 파일 묶음이나 새 테이블 추론용이면 `"*"` 허용; 테이블 이름을 주면 `check_table`로 존재 및 `mode == "append"`를 즉시 검증해 200MB를 다 올린 뒤 `409 not_append`나 `404 unknown_table`이 나는 일을 막는다).
  - `filename`: `<이름>.csv` 또는 `<이름>.xlsx` (`upload.check_ingest_filename` 검증. 틀리면 `400 invalid_filename`).
  - `total_size`: 전체 파일 바이트 수 (`1 <= total_size <= INGEST_MAX_UPLOAD_MB * 1024 * 1024`). `0` 이하이면 `400 invalid_request`, 한도를 넘으면 `413 upload_too_large`.
  - `sha256` (선택): 전체 파일의 원본 바이트 SHA-256(64자리 소문자 hex). 지정하면 마지막 조각이 도착했을 때 서버가 전체 해시를 검증한다.
- **응답 (`201 Created`):**
  ```json
  {
    "upload_id": "u_8f9a...",
    "table": "entries",
    "filename": "entries_202611.csv",
    "offset": 0,
    "total_size": 52428800,
    "chunk_max_bytes": 16777216,
    "status": "receiving",
    "expires_at": "2026-11-22T14:45:00Z"
  }
  ```

#### 2) 현재 상태·오프셋 조회: `GET /api/manage/uploads/{upload_id}`

- 연결이 끊긴 뒤 클라이언트가 서버가 어디까지 안전하게 디스크에 썼는지 확인할 때 부른다.
- 요청한 사람(`actor_id`)과 세션 소유자가 다르면 존재 여부를 노출하지 않고 `410 upload_expired`로 거절한다.
- 만료(`UPLOAD_TTL` 1시간 경과)되었거나 없는 `upload_id`면 `410 upload_expired`.
- 살아 있으면 마지막 활동 시각(`at`)을 갱신하고 `{upload_id, table, filename, offset, total_size, chunk_max_bytes, status: "receiving" | "ready", expires_at}`를 `200 OK`(헤더 `Upload-Offset: <offset>`, `Upload-Length: <total_size>`)로 돌려준다.

#### 3) 조각(Chunk) 이어 올리기: `PATCH /api/manage/uploads/{upload_id}`

- **요청 헤더:**
  - `Content-Type: application/octet-stream` (`ManageGuard`에서 `PATCH /api/manage/uploads/<id>`에 한해 `application/octet-stream`을 허용하되 `Origin` 및 `Sec-Fetch-Site` CSRF 검사는 기존과 동일하게 강제한다).
  - `Upload-Offset: <0 이상의 정수>` (필수. 없거나 정수가 아니면 `400 bad_request`).
  - `Content-Sha256: <64자리 hex>` (선택. 이 조각 바이트의 SHA-256. 지정 시 조각 기록 전 검증하고 틀리면 디스크 오프셋을 바꾸지 않고 `422 checksum_mismatch`).
- **요청 본문:** 조각의 원시 바이트 스트림(`1 <= 조각 크기 <= INGEST_CHUNK_MAX_MB * 1024 * 1024`, 기본 16MB; 마지막 조각까지 합산이 `total_size`를 넘으면 `413 upload_too_large`).
- **동시성·중복 조각·오프셋 검증 규칙 (핵심 계약):**

| 케이스 | 조건 | 서버 동작 및 응답 |
|---|---|---|
| **동시 PATCH 충돌** | 같은 `upload_id`에 다른 PATCH가 이미 디스크에 쓰는 중 | 즉시 `409 upload_in_progress` 반환(세션별 non-blocking `threading.Lock`) |
| **정상 순차 조각** | `req_offset == session.offset` 이고 `req_offset + len(chunk) <= total_size` | 임시 파일의 `session.offset` 위치에 조각을 쓰고 `fsync`(또는 flush) 후 `session.offset += len(chunk)` 갱신 |
| **완전 중복 조각 (재전송)** | `req_offset < session.offset` 이고 `req_offset + len(chunk) <= session.offset` | 클라이언트가 응답만 못 받고 같은 조각을 재전송한 경우. 디스크에 이미 쓰인 `[req_offset : req_offset + len(chunk)]` 바이트와 비교해 **일치하면 디스크를 다시 쓰지 않고 현재 `session.offset`을 `200 OK`로 멱등 반환**한다. 바이트가 다르면 `409 offset_mismatch`(+ 현재 `offset`)로 거절한다 |
| **부분 겹침 조각** | `req_offset < session.offset < req_offset + len(chunk)` | 앞부분 `[req_offset : session.offset]`이 이미 쓰인 바이트와 일치하고 `req_offset + len(chunk) <= total_size`이면 꼬리 부분(`[session.offset :]`)만 이어 쓴다. 앞부분이 다르면 `409 offset_mismatch` |
| **건너뛴 오프셋 (Gap)** | `req_offset > session.offset` | 중간 조각이 빠졌으므로 아무것도 쓰지 않고 `409 offset_mismatch` (`{"detail": "offset_mismatch", "message": "...", "offset": session.offset}`) 반환. 클라이언트는 응답의 `offset`부터 다시 보낸다 |
| **크기 초과** | `req_offset + len(chunk) > total_size` 또는 조각이 `chunk_max_bytes` 초과 | 아무것도 쓰지 않고(스트림 읽기 중 초과 시 `session.offset`으로 `truncate` 복원) `413 upload_too_large` 반환 |
| **도중 끊김 (Partial Stream)** | PATCH 스트림을 읽는 도중 네트워크가 끊김 | 해당 PATCH가 시작되기 전의 `session.offset`으로 임시 파일을 `os.truncate`하거나, 안전하게 `fsync`된 바이트까지만 `session.offset`을 전진시켜 다음 `GET` 조회 시 정확한 재개 지점을 알려 준다 |
| **마지막 조각 완료 (`offset == total_size`)** | 전체 바이트 수신 완료 | ① 세션에 `sha256`이 지정되어 있으면 임시 파일 전체의 SHA-256을 계산해 대조한다. 다르면 임시 파일과 세션을 즉시 폐기하고 `422 checksum_mismatch` 반환.<br>② 파일 형식 기본 검사(`.xlsx`면 ZIP 매직 바이트 `PK\x03\x04` 확인).<br>③ 통과하면 `session.status = "ready"`로 바꾸고 `200 OK` (`status: "ready"`, `offset: total_size`) 반환 |

#### 4) 업로드 취소 및 정리: `DELETE /api/manage/uploads/{upload_id}`

- 진행 중(`receiving`)이거나 보관 중(`ready`)인 업로드 세션을 즉시 폐기하고 디스크의 임시 파일·폴더를 삭제한다 → `200 {"ok": true, "upload_id": "..."}`.
- 사용자가 화면에서 `[취소]`를 누르거나 다른 파일을 고를 때 호출해 `UPLOAD_TTL`까지 기다리지 않고 디스크를 즉시 회수한다.

#### 5) 미리보기 및 적재 확정 (`POST /api/manage/tables/{t}/batches`)

- 기존에는 `{"upload_id": "..."}` 요청이 `dry_run=False`(확정)만 수행했으나, 이제 **`{"upload_id": "...", "dry_run": true | false, "columns"?, "force"?, "skip_invalid"?, "strict"?}`를 모두 지원**한다.
- 아직 조각 전송이 끝나지 않은(`status != "ready"`, 즉 `offset < total_size`) `upload_id`로 미리보기나 확정을 요청하면 `409 upload_incomplete` (`{"detail": "upload_incomplete", "message": "파일 업로드가 아직 끝나지 않았습니다", "offset": session.offset, "total_size": session.total_size}`)로 거절한다.
- `dry_run: true`로 호출하면 미리보기 작업을 돌리고 **완료된 파일을 그대로 유지**해 이어서 `dry_run: false`로 다시 올리지 않고 확정할 수 있다(미리보기가 실패해도 사용자가 머리글 맞춤 `columns`나 `skip_invalid`, `force`를 바꿔 같은 `upload_id`로 재시도할 수 있도록 `status == "ready"`인 이어 올리기 파일은 유효 시간 동안 유지하거나, 사용자가 취소/확정 성공 시 삭제한다).
- `dry_run: false`로 본 적재가 성공하면 `uploads.drop(upload_id)`로 임시 파일을 즉시 삭제한다.

### 5-3. 만료·용량·권한 보호 (`dashkit/manage/uploads.py`)

1. **만료(`UPLOAD_TTL`):**
   - 완료된 파일(`status == "ready"`)은 마지막 활동으로부터 `UPLOAD_TTL`(기본 3,600초 = 1시간) 동안 보관한다.
   - 미완료 세션(`status == "receiving"`)은 마지막 조각 도착/조회로부터 `INCOMPLETE_UPLOAD_TTL`(기본 1,800초 = 30분) 동안 조각이 오지 않으면 만료시켜 임시 파일을 삭제한다.
2. **동시 보관 개수 및 총 디스크 상한:**
   - 최대 세션 수 `MAX_KEEP = 20`, 전체 임시 업로드 폴더 총합 상한 `MAX_TOTAL_UPLOAD_BYTES = INGEST_MAX_REQUEST_MB * 2 * 1024 * 1024`(기본 1GB).
   - 새 세션을 만들 때 만료된 세션을 먼저 치우고, 그래도 상한을 넘으면 가장 오래 활동이 없던 세션부터 정리한다(진행 중인 `in_progress` 세션은 건드리지 않는다).
3. **권한 격리:**
   - `upload_id`는 생성한 사용자(`principal.id`)와 대상 `table`에 귀속된다. 다른 운용자(설령 `admin`이라도 다른 세션의 `upload_id`)가 조회·조각 전송·확정·취소를 시도하면 `410 upload_expired`로 거절한다.
4. **프런트엔드(`web/admin/menus/tables.js`, `web/shared/ingest.js`) 연동:**
   - 파일 탭에서 파일 크기가 기준(`RESUMABLE_THRESHOLD_BYTES`, 예: `5 * 1024 * 1024` = 5MB 이상, 또는 모든 파일 업로드에서 선택/자동) 이상일 때 조각 단위(`CHUNK_BYTES = 4 * 1024 * 1024` = 4MB)로 이어 올리기 프로토콜을 사용한다.
   - 조각 전송 중 네트워크 오류나 5xx가 발생하면 자동 재시도(최대 3회, `GET /uploads/{id}`로 서버 `offset` 확인 후 해당 슬라이스부터 `PATCH`)하고, 자동 재시도도 실패하면 화면에 **"[이어 올리기]"** 버튼과 현재 업로드된 바이트/비율(`42% (84MB / 200MB)`)을 남겨 사용자가 클릭 한 번으로 끊긴 지점부터 재개할 수 있게 한다.

---

## 6. 모듈별 변경 파일 범위

### 6-1. 계약 문서 (`contracts/`)

1. [`contracts/ingest.md`](../contracts/ingest.md):
   - "접수" 표(`PARSERS`)에 `xlsx`(엑셀 `.xlsx` 통합 문서, 시트·셀 복원·오류 위치 `<파일 이름>:<엑셀 행>:<열>`)와 `cursor`(외부 DB 읽기 전용 커서 스트림, 오류 위치 `<원본 이름>:<행>:<열>`) 추가.
   - "더하기" 절에 외부 DB 가져오기(`python -m dashkit ingest-db` / `make ingest-db`, 환경 변수 `--dsn-env` 자격 증명 규칙)와 감시 폴더(`python -m dashkit ingest-watch` / `make ingest-watch`, `_done/`·`_failed/` 격리 규칙) 명세 추가.
2. [`contracts/manage.md`](../contracts/manage.md):
   - "요청 보호"에 업로드 파일 확장자(`.csv`, `.xlsx`) 및 이어 올리기 조각 경로(`PATCH /api/manage/uploads/{id}`의 `application/octet-stream`) 명시.
   - "경로" 표와 본문에 이어 올리기 엔드포인트 4종(`POST /uploads`, `GET /uploads/{id}`, `PATCH /uploads/{id}`, `DELETE /uploads/{id}`)과 상태 코드(`409 offset_mismatch`·`upload_in_progress`·`upload_incomplete`, `410 upload_expired`, `413 upload_too_large`, `422 checksum_mismatch`·`invalid_xlsx`) 및 `POST /tables/{t}/batches`의 `{"upload_id", "dry_run": true}` 미리보기 지원 명세.
3. [`contracts/auth.md`](../contracts/auth.md):
   - `data.batch.create` / `data.batch.failed`의 `method` 값에 `"db"`(원본 DB 가져오기), `"watch"`(감시 폴더) 추가 및 외부 DB 자격 증명 무노출 원칙 명시.

### 6-2. 백엔드 (`dashkit/`)

1. `dashkit/ingest/xlsx.py` (신규):
   - 표준 라이브러리(`zipfile`, `xml.etree.ElementTree.iterparse`) 기반 `XlsxParser(Parser)` 구현.
   - ZIP 비압축 크기·압축률 상한, `<!DOCTYPE`/`<!ENTITY` 차단, 공유 문자열·스타일 날짜 서식·1900/1904 날짜 일련번호·희소 셀(`r="C5"`) 복원.
2. `dashkit/ingest/db_import.py` (신규):
   - `CursorParser(Parser)` 구현 (`PARSERS["cursor"]`).
   - `ingest_from_db(...)`: `--dsn-env` 환경 변수 검증·읽기, 외부 DB `read_only=True` 접속, `--source-table` / `--query-file` 단일 SELECT 강제, DSN/비밀번호 마스킹(`sanitize_db_error`), `ingest()` 호출 및 감사 로그(`method="db"`).
3. `dashkit/ingest/watch.py` (신규):
   - `watch_once(...)`, `watch_loop(...)`: 감시 폴더 `.watch.lock` 잠금, `_processing/` 중단 파일 재시작 복구, 대기 폴더 스캔, 임시/숨김 파일 및 심볼릭 링크 필터링, 크기·수정 시각 안정성(quiescence) 확인, `_processing/` → `ingest()` → `_done/` 또는 `_failed/`(+ `.error.json`, `redact_sensitive_errors` 민감 열 오류 값 명시적 마스킹) 원자적 이동 및 감사 로그(`method="watch"`).
4. [`dashkit/ingest/intake.py`](../dashkit/ingest/intake.py) & [`dashkit/ingest/add.py`](../dashkit/ingest/add.py):
   - `PARSERS`에 `"xlsx": XlsxParser`, `"cursor": CursorParser` 등록.
   - `make_parser`에서 `kind == "xlsx"` 및 파일 확장자 `.xlsx` 자동 분기(`strict=False`일 때), `kind == "cursor"` 분기 지원.
   - `METHODS`에 `"db"`, `"watch"` 추가.
5. [`dashkit/project/upload.py`](../dashkit/project/upload.py) & [`dashkit/scaffold.py`](../dashkit/scaffold.py):
   - `check_ingest_filename(name)` 추가(`.csv`와 `.xlsx` 허용, 경로 문자·제어 문자 차단). 기존 `check_filename`(`.csv` 전용)은 Studio `replace` CSV 업로드용으로 유지.
   - `scaffold.py`의 `infer_tables`(관리 서비스 `POST /tables/infer`)에서 `.xlsx` 파일이 들어오면 `XlsxParser`로 행을 읽어 추론하고 첫 파일은 정규형 CSV로 작성.
6. [`dashkit/manage/uploads.py`](../dashkit/manage/uploads.py), [`dashkit/manage/app.py`](../dashkit/manage/app.py), [`dashkit/manage/routes/tables.py`](../dashkit/manage/routes/tables.py):
   - `Uploads`에 이어 올리기 세션(`start_resumable`, `status`, `append_chunk`, `cancel`) 및 오프셋·중복 조각·동시 PATCH 잠금·SHA-256 검증·용량/TTL 정리 추가.
   - `ManageGuard`에서 `PATCH /api/manage/uploads/[A-Za-z0-9_-]+` 경로에 `application/octet-stream` 본문 허용(출처 검사 유지).
   - `POST /uploads`, `GET /uploads/{id}`, `PATCH /uploads/{id}`, `DELETE /uploads/{id}` 라우트 추가 및 `POST /tables/{t}/batches`에서 `{"upload_id", "dry_run": true}` 지원.
7. [`dashkit/__main__.py`](../dashkit/__main__.py) & [`Makefile`](../Makefile):
   - `python -m dashkit ingest`가 `.xlsx` 파일도 받도록 확장(`--sheet` 선택 옵션 추가).
   - `python -m dashkit ingest-db` (`make ingest-db`) 서브커맨드 추가.
   - `python -m dashkit ingest-watch` (`make ingest-watch`) 서브커맨드 추가.

### 6-3. 프런트엔드 (`web/`)

1. [`web/shared/ingest.js`](../web/shared/ingest.js):
   - `isSupportedIngestFile(name)` (`.csv` 및 `.xlsx` 허용), `fileStem(name)` (`.csv`·`.xlsx` 확장자 제거) 확장.
   - 이어 올리기 조각 계산 순수 헬퍼(`nextChunkRange(offset, totalSize, chunkSize)`) 추가.
2. [`web/admin/menus/tables.js`](../web/admin/menus/tables.js), [`web/admin/structure.js`](../web/admin/structure.js), [`web/strings.js`](../web/strings.js):
   - 파일 선택·드롭에서 `.csv`와 `.xlsx` 모두 허용.
   - 대용량 파일 업로드 시 이어 올리기 세션(`POST /uploads` → `PATCH /uploads/{id}`) 진행률 표시, 네트워크 중단 시 `[이어 올리기]` 재개 버튼과 `[취소]`(`DELETE /uploads/{id}`) 연동.

---

## 7. 테스트 및 검증 계획

기존 개발·운영 스택과 `.state/`·`.build/`를 건드리지 않고 단위 테스트(`make test-unit`)와 격리 테스트 스택(`AUTH_COOKIE_SECURE=0 make test`)에서 검증한다.

### 7-1. 단위 및 파서·보안 테스트 (`tests/test_ingest_unit.py`, `tests/test_manage_upload.py`, `tests/js/ingest.test.mjs`)

| 영역 | 검증 시나리오 |
|---|---|
| **동일 정규형 보장 (`xlsx` vs `csv` vs `rows` vs `cursor`)** | - 동일한 행 데이터(문자열, 천 단위 쉼표 숫자, `YYYY/MM/DD` 및 엑셀 날짜 일련번호, 참거짓, 빈 칸 NULL, 빠진 선택 열)를 `CsvParser`, `RowsParser`, `XlsxParser`, `CursorParser`로 각각 접수했을 때 `CanonicalWriter`가 만드는 정규형 `.csv.gz`의 바이트와 `sha256`이 100% 일치함 |
| **`xlsx` 파서 정상·경계·악성 입력** | - 공유 문자열(`t="s"`), 인라인 문자열(`inlineStr`), 서식 있는 텍스트(`<r><t>`), 숫자, 부울(`t="b"`), 1900/1904 날짜 일련번호, 희소 셀(`r="A1"`, `r="D1"`), 앞뒤 빈 행·빈 끝 열 정상 파싱<br>- 다중 시트에서 기본 첫 가시 시트 선택 및 `sheet` 이름/번호 지정, 없는 시트 422 오류<br>- 오류 위치가 `<파일 이름>:<엑셀 행 번호>:<열>`로 정확히 보고됨<br>- ZIP가 아닌 파일, 암호화된 xlsx / 구형 `.xls`, `<!DOCTYPE`/`<!ENTITY` 포함 XML, 비압축 크기 한도 초과(ZIP 폭탄 모사) 시 메모리 폭주 없이 즉시 `422` 거절 및 파일 핸들 정리 |
| **원본 DB 가져오기 (`ingest-db`)** | - `CursorParser`가 2,000행 이상을 스트리밍으로 읽고 데이터 유형(`int`, `Decimal`, `bool`, `date`, `datetime`, `UUID`, `None`)을 정확히 변환함<br>- 지원하지 않는 `bytea`·JSON 배열 열 포함 시 정확한 행·열 위치 오류 보고<br>- `--dsn-env`에 지정된 환경 변수가 없거나 잘못된 변수 이름일 때 거절<br>- 접속 실패·쿼리 오류 시 DSN 원문과 비밀번호가 표준 에러·예외 메시지·로그·감사 로그 어디에도 포함되지 않음(`sanitize_db_error` 검증)<br>- 외부 DB 세션이 `read_only=True`로 열려 쓰기 쿼리(`INSERT`/`UPDATE`/`DROP` 또는 쓰기 CTE)가 외부 DB에서 실행 차단됨 |
| **감시 폴더 (`ingest-watch`)** | - 대기 폴더의 `.csv`, `.csv.gz`, `.xlsx`가 참조 순서·이름순으로 적재되고 `_done/`으로 이동함<br>- `.`·`_` 시작 파일, `.part`·`.tmp` 파일, 아직 수정 시각/크기가 안정되지 않은 파일은 건너뜀<br>- **`_processing/` 재시작 복구:** `_processing/`으로 이동한 직후(적재 전) 중단된 파일은 다음 시작/스캔에서 회수되어 정상 적재되고 `_done/`으로 이동함; 적재 커밋 직후 중단되어 `_processing/`에 남은 파일은 재시작 복구 시 기존 배치 중복 감지(`duplicate_file`)와 연동해 중복 적재나 `_failed/` 격리 없이 `_done/`으로 정상 마감됨<br>- 심볼릭 링크, 크기 초과 파일, 검증 실패 파일은 `_failed/`로 격리되고 `.error.json`이 생성되며 다음 스캔에서 재실행되지 않음<br>- **민감 열 오류 값 마스킹:** `sensitive: true` 열의 값이 유형·패턴·범위 검증에 실패하거나 열 개수 불일치가 발생했을 때 `redact_sensitive_errors`가 원문 값을 마스킹해 `.error.json`에 민감 값이 절대 남지 않음을 검증<br>- `409 apply_running` 등 일시적 잠금 실패 시 `_failed/`로 버리지 않고 다음 스캔에 재시도함 |
| **이어 올리기 (`uploads` 수명주기)** | - `POST /uploads` → 여러 조각 `PATCH /uploads/{id}` → `GET /uploads/{id}` 오프셋 확인 → 완료 후 `POST /tables/{t}/batches` 미리보기(`dry_run: true`) 및 확정(`dry_run: false`) 성공, 일괄 multipart 업로드와 동일한 배치 파일·재생성 결과 생성<br>- **중단 및 재개:** 1·2번째 조각 전송 후 연결 중단 → `GET /uploads/{id}`로 `offset` 조회 → 3번째 조각부터 이어 올려 정상 완료<br>- **중복 조각 및 부분 겹침:** 이미 보낸 오프셋의 동일 바이트 조각 재전송 시 200 멱등 응답; 다른 바이트 재전송 또는 건너뛴 오프셋(`req_offset > offset`) 전송 시 `409 offset_mismatch`와 현재 `offset` 반환<br>- **동시 PATCH:** 같은 `upload_id`에 동시 PATCH 요청 시 하나는 `409 upload_in_progress`<br>- **크기 제한 및 무결성:** `total_size > max_upload_mb` 또는 조각 누적 크기 초과 시 `413 upload_too_large`; 완료 시 `sha256` 불일치 시 임시 파일 즉시 삭제 및 `422 checksum_mismatch`; 미완료 세션을 `batches`에 넘기면 `409 upload_incomplete`<br>- **만료·취소·권한:** 다른 운용자(`operator B`)가 `operator A`의 `upload_id`에 접근 시 `410 upload_expired`; `DELETE /uploads/{id}` 호출 또는 TTL 경과 시 디스크 임시 폴더가 남김없이 삭제됨 |

### 7-2. DB 및 전체 통합 검증 (`tests/test_ingest_db.py`, `tests/test_manage_data.py`)

1. `xlsx`, `ingest-db`(테스트 DB의 별도 읽기 전용 원본 스키마/테이블에서 가져오기), `ingest-watch`, 이어 올리기로 각각 적재한 뒤 `rebuilt_equals_current`(`make apply`로 처음부터 다시 만든 DB와 현재 DB 비교)가 100% 일치하는지 검증한다.
2. 읽기 경로(`GET /uploads/{id}`, 테이블·배치 조회)에서 `dash_owner` 계정이 열리지 않음을 확인한다(`test_read_paths_never_open_the_owner_account`).
3. `AUTH_COOKIE_SECURE=0 make test` 전체 스위트를 통과시킨 뒤 `make test-down`으로 격리 스택을 정리한다.

---

## 8. 완료 조건

- [ ] 도입하는 방식(`xlsx` 파서, 원본 DB 가져오기, 감시 폴더, 이어 올리기)마다 기존 CSV/JSON과 동일한 데이터가 동일한 정규형 배치 파일(`sha256` 일치)과 동일한 `load` 재생성 결과를 만든다.
- [ ] 잘못된 입력(깨진/악성 `xlsx`, 스키마 불일치, 체크섬 불일치), 중단·재시도(오프셋 조회·중복 조각 멱등 처리·불일치 오프셋 409), 크기 제한(413), 권한 격리(타 사용자 세션 차단, 읽기 전용 외부 DB 세션) 및 자원 정리(만료·취소·실패 파일 및 임시 폴더 삭제)를 단위·통합 테스트로 검증했다.
- [ ] 외부 DB의 자격 증명(DSN·비밀번호)은 환경 변수 이름으로만 전달받고 코드·CLI 인자·표준 출력/에러·작업 로그·감사 로그에 남지 않음을 테스트로 고정했다.
- [ ] `contracts/ingest.md`, `contracts/manage.md`, `contracts/auth.md` 계약 문서가 구현과 일치하도록 갱신되었다.
