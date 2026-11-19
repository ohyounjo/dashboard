# Superset 업그레이드 절차

Superset 버전은 `.env.example`의 `SUPERSET_VERSION` **한 곳**에 있다. `superset/Dockerfile`의 `ARG SUPERSET_VERSION`과 `docker-compose.yml`의 빌드 인자·이미지 태그(`dashkit-superset:<버전>`)가 이 값을 읽는다. Makefile은 `.env.example` → `.env` 순으로 읽으므로(`.env`가 이긴다) 예전에 만든 `.env`에 이 변수가 없어도 된다. `tests/test_superset_keys.py`가 다른 파일에 버전이 직접 쓰여 있지 않은지 확인한다.

번들(`dashkit/sync/bundle.py`)은 그 버전의 export 형식에 맞춘 키를 직접 쓴다. 버전을 올리면 키가 바뀌거나 사라질 수 있으므로 아래 순서로 확인한다.

## 순서

1. **버전 변경.** `.env.example`의 `SUPERSET_VERSION`을 바꾼다. 자기 `.env`에 같은 변수가 있으면 그것도 바꾼다. `superset/Dockerfile`의 `psycopg2-binary` 버전이 새 Superset의 `postgres` extra와 같은지 확인한다(`superset-notes.md` 1절).

2. **테스트 스택에서 띄운다.** 개발 스택은 그대로 둔다.

   ```bash
   make test KEEP=1
   ```

   이미지가 새 버전으로 빌드되고(`dashkit-superset:<새 버전>`), 테스트 스택(웹앱 18080, Superset 18088)이 남는다. 이 단계에서 실패하는 통합 테스트가 첫 번째 단서다.

3. **export를 다시 받는다.** 테스트 스택의 Superset에서 sync가 만든 대시보드를 export해 `contracts/superset-<새 버전>-export/`에 풀어 둔다.

   ```bash
   # http://localhost:18088 에 관리자로 로그인 → Dashboards → 전체 선택 → Export
   mkdir -p contracts/superset-<새 버전>-export
   unzip dashboard_export_*.zip -d /tmp/export && cp -r /tmp/export/*/. contracts/superset-<새 버전>-export/
   ```

   - 폴더 구성은 `databases/`, `datasets/<DB 이름>/`, `charts/`, `dashboards/`, `metadata.yaml`이다. 종류 폴더를 한 단계 아래(예: `managed/dashboards/`)에 두어도 된다.
   - DB 비밀번호는 Superset이 `XXXXXXXXXX`로 가린다. 그대로인지 확인하고 커밋한다.
   - 이전 버전의 export 폴더는 비교가 끝나면 지운다.

4. **키 대조.** `make test-unit`의 `tests/test_superset_keys.py`가 번들의 키가 새 export에 있는지 본다.

   | 실패 | 뜻 | 조치 |
   |---|---|---|
   | `…에 export에 없는 키 [...]` | 새 버전이 그 키를 내보내지 않는다(이름이 바뀌었거나 없어졌다) | `bundle.py`에서 그 키를 새 이름으로 바꾸거나 지운다. `make golden`으로 스냅샷을 갱신하고 diff를 검토한다 |
   | `export에 <종류> 키가 없음` | export에 그 종류의 파일이 없다 | 3번에서 대시보드와 딸린 차트·데이터셋·DB가 모두 들어갔는지 확인 |

   번들에는 export에 나오지 않는 import 전용 키(`is_managed_externally`, `external_url`)도 쓴다. 이 둘은 `contracts/superset-<버전>-export/managed/import-only-keys.yaml`에 근거와 함께 적혀 있고 테스트가 그 목록만 허용한다. 새 버전에서 모델의 `extra_import_fields`에 여전히 있는지 다시 확인하고, 통합 테스트 `test_objects_carry_the_mark`가 실제 저장을 본다.

   새 export에만 있는 키(우리가 쓰지 않는 새 키)는 실패가 아니다. 필요하면 `git diff --no-index contracts/superset-<이전>-export contracts/superset-<새 버전>-export`로 훑어본다.

   대조 범위는 종류별 최상위 키, 데이터셋의 `metrics[]`·`columns[]` 항목 키, 데이터베이스 `extra` 키다. 차트 `params`와 대시보드 `position`·`metadata` 안쪽은 Superset이 JSON으로 그대로 보관하므로 export로는 검증되지 않는다. 이 부분은 5번이 맡는다.

5. **갤러리 프로젝트 렌더링 확인.** 모든 축약 유형과 카탈로그의 원본 유형이 들어 있는 `projects/chart-gallery`를 테스트 스택에 적용하고 화면을 본다.

   ```bash
   make test          # 3단계가 갤러리를 적용하고 차트마다 데이터 조회를 확인한다
   ```

   화면은 `make test KEEP=1` 뒤 http://localhost:18080 에서 탭마다 확인한다. 차트 자리에 오류가 보이는 유형은 그 유형 파일(`dashkit/charts/<viz>.yaml`)의 파라미터 이름을 새 버전에 맞춘다. 맞출 수 없으면 축약에서 내리고 카탈로그에 원본 방식 조각으로 남긴다(보강설계서 7절 D4).

6. **기록.** `contracts/superset-notes.md`에 새 버전에서 달라진 점(키 이름, 기본값, 권한, 임베드 SDK 호환)을 적는다. README 맨 아래의 버전 줄과 `web/vendor/`의 SDK 버전도 함께 본다.

7. **개발 스택에 반영.** `make test-down` 후 `make up && make apply`. Superset 메타데이터 DB의 마이그레이션은 `superset-init`이 `superset db upgrade`로 처리한다. 되돌릴 수 없는 마이그레이션일 수 있으므로 운영 데이터가 있으면 먼저 `superset-db` 볼륨을 백업한다.
