# 호스트에는 Docker와 make만 있으면 된다. 파이썬 작업은 모두 tools 컨테이너에서 돈다.
SHELL := /bin/sh
.DEFAULT_GOAL := help

# .env.example을 먼저, .env를 나중에 읽는다(.env가 이긴다). 예전에 만든 .env에 없는 새 변수는 .env.example의 값을 쓴다.
# 예: SUPERSET_VERSION은 .env.example 한 곳에만 둔다.
COMPOSE      ?= docker compose --env-file .env.example --env-file .env
WAIT_TIMEOUT ?= 300
# tools가 만든 파일(.state/, .build/, 캐시)이 호스트 사용자 소유가 되도록 같은 uid:gid로 실행한다
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
TOOLS    := $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) tools

.NOTPARALLEL:
.PHONY: help env env-secrets auth-init admin outbox up check load sync apply plan migrate new studio studio-down studio-image test test-unit test-down unit-image golden gen reset down logs dirs _test _test_run

# ---- 테스트 스택(보강설계서 3-2, 7절 D5): 개발 스택과 이름·포트·볼륨·상태 디렉터리가 모두 다르다.
# 상태·빌드 디렉터리는 .test/ 고정이다(아래 정리 단계가 이 경로를 그대로 쓴다).
TEST_NAME          := dashkit-test
TEST_WEB_PORT      ?= 18080
TEST_SUPERSET_PORT ?= 18088
TEST_STUDIO_PORT    ?= 18090
TEST_ENV := COMPOSE_PROJECT_NAME=$(TEST_NAME) \
  WEB_PORT=$(TEST_WEB_PORT) SUPERSET_PORT=$(TEST_SUPERSET_PORT) \
  WEB_ORIGINS=http://localhost:$(TEST_WEB_PORT),http://127.0.0.1:$(TEST_WEB_PORT) \
  SUPERSET_PUBLIC_URL=http://localhost:$(TEST_SUPERSET_PORT) \
  STATE_DIR=/app/.test/state BUILD_DIR=/app/.test/build HOST_STATE_DIR=./.test/state HOST_BUILD_DIR=./.test/build \
  STUDIO_PORT=$(TEST_STUDIO_PORT) WEB_PUBLIC_URL=http://localhost:$(TEST_WEB_PORT)
# 단위 테스트는 compose 없이 tools 이미지만으로 돈다(.env도 필요 없다)
APP_IMAGE := dashkit-app:local
STUDIO_IMAGE := dashkit-studio:local
# 웹앱 JS의 순수 함수 테스트(tests/js/, node --test). 호스트에 node가 없어도 된다
NODE_IMAGE := node:22.21.1-alpine3.22
# Studio가 projects/에 쓰는 파일이 호스트 사용자 소유가 되게 한다(compose의 studio.user)
export HOST_UID HOST_GID
# Studio 포트: 환경 > .env > .env.example
STUDIO_PORT ?= $(or $(shell sed -n 's/^STUDIO_PORT=//p' .env 2>/dev/null | tail -1),$(shell sed -n 's/^STUDIO_PORT=//p' .env.example | tail -1),8090)
UNIT_RUN  := docker run --rm --user $(HOST_UID):$(HOST_GID) -v "$(CURDIR)":/app -w /app $(APP_IMAGE)

help:
	@echo "make env     .env 생성(없을 때만, 비밀값 자동 생성)"
	@echo "make auth-init   인증 DB(dashkit_auth 스키마, dash_auth 계정) 만들기·올리기(여러 번 실행해도 됨)"
	@echo "make admin EMAIL=<주소>   첫 admin 계정 만들기(비밀번호는 프롬프트)"
	@echo "make outbox  테스트 메일함의 최근 메일과 링크(메일 테스트 모드, 등급 제한 없음)"
	@echo "make up      전체 빌드·기동 후 superset/backend/web healthy 대기"
	@echo "make check   설정 검사"
	@echo "make load    CSV 적재"
	@echo "make sync    Superset 반영"
	@echo "make apply   check → load → sync (backend는 설정을 스스로 다시 읽는다)"
	@echo "make plan    적용 전에 바뀌는 것 보기(데이터, 뷰, Superset). 아무것도 바꾸지 않음"
	@echo "make new NAME=<이름> FROM=<CSV 폴더>   CSV에서 프로젝트 초안 생성"
	@echo "make migrate [CHECK=1]   설정 파일을 최신 설정 버전으로"
	@echo "make studio / make studio-down   구성 화면(http://localhost:8090, 127.0.0.1 전용)"
	@echo "make test-unit  단위 테스트(스택 불필요: 설정 검증, 번들 스냅샷, CSV 검증, 도메인 단어, 웹앱 JS)"
	@echo "make test    통합 테스트(별도 테스트 스택을 띄우고 끝나면 내림. KEEP=1이면 남김)"
	@echo "make golden  번들 스냅샷(tests/golden/) 갱신"
	@echo "make gen     레지스트리에서 계약 문서의 생성 구간 갱신"
	@echo "make reset CONFIRM=1   볼륨과 .state/.build 삭제"
	@echo "make down / make logs"

# ---------------------------------------------------------------- .env
env:
	@if [ -f .env ]; then \
	  echo "• .env가 이미 있음 → 그대로 둠"; \
	else \
	  umask 077; tmp=.env.tmp.$$$$; : > "$$tmp"; \
	  while IFS= read -r line || [ -n "$$line" ]; do \
	    case "$$line" in \
	      [A-Z_]*=__GENERATE__) \
	        v=$$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n'); \
	        [ $${#v} -eq 48 ] || { echo "✖ 임의 값 생성 실패"; rm -f "$$tmp"; exit 1; }; \
	        line="$${line%%=*}=$$v";; \
	    esac; \
	    printf '%s\n' "$$line" >> "$$tmp"; \
	  done < .env.example; \
	  mv "$$tmp" .env; \
	  echo "✔ .env 생성(비밀값 $$(grep -c '=__GENERATE__$$' .env.example)개 생성)"; \
	fi

# 예전에 만든 .env에 없는 비밀값(.env.example의 __GENERATE__ 자리)을 임의 값으로 덧붙인다. 있는 값은 건드리지 않는다.
# 예: 로그인 기능과 함께 생긴 DATA_DB_AUTH_PASSWORD. make up·make auth-init이 먼저 부른다.
env-secrets:
	@[ -f .env ] || { echo "✖ .env가 없음. make env를 먼저 실행하세요"; exit 1; }
	@umask 077; \
	if [ -n "$$(tail -c1 .env)" ]; then echo >> .env; fi; \
	while IFS= read -r line || [ -n "$$line" ]; do \
	  case "$$line" in \
	    [A-Z_]*=__GENERATE__) \
	      k=$${line%%=*}; \
	      if ! grep -q "^$$k=" .env; then \
	        v=$$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n'); \
	        [ $${#v} -eq 48 ] || { echo "✖ 임의 값 생성 실패"; exit 1; }; \
	        printf '%s=%s\n' "$$k" "$$v" >> .env; \
	        echo "• .env에 $$k 추가(임의 값)"; \
	      fi;; \
	  esac; \
	done < .env.example

dirs:
	@mkdir -p .state .build

# ---------------------------------------------------------------- 기동
# $(call wait_healthy,<서비스 목록>)
define wait_healthy
	@deadline=$$(( $$(date +%s) + $(WAIT_TIMEOUT) )); \
	for s in $(1); do \
	  while :; do \
	    id=$$($(COMPOSE) ps -q $$s 2>/dev/null); \
	    st=; [ -n "$$id" ] && st=$$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $$id 2>/dev/null); \
	    if [ "$$st" = healthy ]; then echo "✔ $$s healthy"; break; fi; \
	    if [ "$$st" = exited ] || [ "$$st" = dead ] || [ $$(date +%s) -ge $$deadline ]; then \
	      echo "✖ $$s: $${st:-not running} (제한 $(WAIT_TIMEOUT)초)"; \
	      $(COMPOSE) ps -a; $(COMPOSE) logs --tail=80 $$s; exit 1; \
	    fi; \
	    sleep 3; \
	  done; \
	done
endef

up: dirs env-secrets
	$(COMPOSE) up -d --build
	$(call wait_healthy,superset backend web)

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

# ---------------------------------------------------------------- 로그인(contracts/auth.md)
# 인증 DB를 만들거나 최신으로 올린다. 역할 생성에는 superuser가 필요하므로 data-db 컨테이너 안에서 postgres 계정으로
# data-db/init/02-auth.sh를 실행한다(새 볼륨은 initdb 때 같은 스크립트가 돈다). 볼륨을 다시 만들지 않는다.
# data-db 컨테이너는 마운트·환경변수가 바뀌었으면 다시 만들어진다(데이터는 볼륨에 그대로 있다).
auth-init: env-secrets
	$(COMPOSE) up -d --wait data-db
	$(COMPOSE) exec -T data-db sh /docker-entrypoint-initdb.d/02-auth.sh
	$(TOOLS) python -m dashkit auth init

# 첫 admin 계정. 비밀번호는 프롬프트로 두 번 받는다(코드·.env에 넣지 않는다).
# 자동화(CI 화면 캡처)에서는 PASSWORD_STDIN=1로 표준 입력의 첫 줄을 읽는다.
admin: dirs
	@test -n "$(EMAIL)" || { echo "사용법: make admin EMAIL=<주소>" >&2; exit 2; }
	$(COMPOSE) run --rm $(if $(PASSWORD_STDIN),-T) --user $(HOST_UID):$(HOST_GID) tools \
	  python -m dashkit auth create-admin --email "$(EMAIL)" $(if $(PASSWORD_STDIN),--password-stdin)

# 테스트 메일함(AUTH_MAIL_MODE=test)의 최근 메일과 링크. 등급 제한 없이 전부 나온다. LIMIT=<개수>(기본 20)
outbox: dirs
	$(TOOLS) python -m dashkit auth outbox --limit $(or $(LIMIT),20)

# ---------------------------------------------------------------- 설정 반영
check: dirs
	$(TOOLS) python -m dashkit check

load: dirs
	$(TOOLS) python -m dashkit load

sync: dirs
	$(TOOLS) python -m dashkit sync

apply: check load sync
	@# backend는 projects/의 설정 변경을 파일 시각으로 감지해 다시 읽는다(재빌드·재시작 없음).
	@# backend는 마지막으로 sync에 성공한 프로젝트(.state/embed.json)를 따른다. 아래 up은 컨테이너가 없을 때만 일한다.
	@# 템플릿 코드(dashkit/, web/widgets/*/widget.json)를 바꿨으면 make up으로 이미지를 다시 만든다.
	$(COMPOSE) up -d --no-deps backend
	$(call wait_healthy,backend)

# 적용 전에 무엇이 바뀌는지 본다(데이터, 뷰, Superset). 아무것도 바꾸지 않는다. contracts/plan.md
plan:
	$(TOOLS) python -m dashkit plan

# 설정 파일을 최신 설정 버전 형식으로 고친다. CHECK=1이면 고칠 것이 있는지만 본다(있으면 종료 코드 1)
migrate:
	$(TOOLS) python -m dashkit migrate $(if $(CHECK),--check)

# CSV 폴더에서 프로젝트 초안을 만든다: make new NAME=myproject FROM=~/exports/  (스택 불필요. contracts/scaffold.md)
new: unit-image
	@test -n "$(NAME)" -a -n "$(FROM)" || { echo "사용법: make new NAME=<이름> FROM=<CSV 폴더>" >&2; exit 2; }
	@from=$$(cd -- "$(FROM)" 2>/dev/null && pwd) || { echo "FROM 폴더가 없음: $(FROM)" >&2; exit 2; }; \
	docker run --rm --user $(HOST_UID):$(HOST_GID) -v "$(CURDIR)":/app -v "$$from":/import:ro -w /app $(APP_IMAGE) \
	  python -m dashkit new "$(NAME)" --from /import

# ---------------------------------------------------------------- Studio(구성 화면, 보강설계서 6절)
# 127.0.0.1에만 열리고 인증이 없다. 외부에 열지 않는다. 웹앱과 Superset은 Studio 출처를 frame-ancestors에 더해 다시 뜬다
# (미리보기 iframe). make studio-down이 원래 정책('none')으로 되돌린다.
studio: dirs
	STUDIO_PORT=$(STUDIO_PORT) STUDIO_ORIGIN=http://localhost:$(STUDIO_PORT) $(COMPOSE) --profile studio up -d --build studio web superset
	$(call wait_healthy,superset web studio)
	@echo "✔ Studio: http://localhost:$(STUDIO_PORT)  (끄기: make studio-down)"

studio-down:
	$(COMPOSE) --profile studio rm -sf studio
	$(COMPOSE) up -d web superset
	$(call wait_healthy,superset web)

# ---------------------------------------------------------------- 테스트
unit-image:
	@docker build -q -t $(APP_IMAGE) --target app -f dashkit/Dockerfile . > /dev/null

studio-image:
	@docker build -q -t $(STUDIO_IMAGE) --target studio -f dashkit/Dockerfile . > /dev/null

# Studio 테스트(tests/test_studio*.py)는 ruamel.yaml이 있는 studio 이미지에서 따로 돈다. app 이미지에서는 그 파일들을 빼고 돈다.
test-unit: unit-image studio-image
	$(UNIT_RUN) python -m pytest -q -p no:cacheprovider -m "not integration" --ignore-glob='tests/test_studio*.py' tests/
	@echo "== 웹앱 JS 테스트(node)"
	docker run --rm --user $(HOST_UID):$(HOST_GID) -v "$(CURDIR)":/app -w /app $(NODE_IMAGE) sh -c 'node --test tests/js/*.test.mjs'
	@if ls tests/test_studio*.py > /dev/null 2>&1; then \
	  echo "== Studio 테스트(studio 이미지)"; \
	  docker run --rm --user $(HOST_UID):$(HOST_GID) -v "$(CURDIR)":/app -w /app $(STUDIO_IMAGE) \
	    python -m pytest -q -p no:cacheprovider -m "not integration" tests/test_studio*.py; \
	fi

# 번들 스냅샷 갱신. COLUMNS=1이면 먼저 현재 스택의 DB에서 PROJECT의 뷰 열 정보를 다시 읽는다(스택 필요).
golden: unit-image
ifeq ($(COLUMNS),1)
	$(TOOLS) python -m tests.golden --columns
endif
	$(UNIT_RUN) python -m tests.golden

# 차트 유형 레지스트리(dashkit/charts/)에서 계약 문서의 생성 구간을 다시 쓴다.
gen: unit-image
	$(UNIT_RUN) python -m dashkit gen

# 통합 테스트는 별도 compose 프로젝트(dashkit-test)에서 돈다. 개발 스택과 .state/, .build/는 건드리지 않는다.
# 1) devtracker로 up·apply → 전체 pytest(Studio 테스트는 ruamel.yaml이 있는 studio 이미지에서 따로 돈다: make test-unit)
#    이어서 Studio를 띄워 tests/test_studio_integration.py(끝나면 원래 프로젝트를 다시 적용한다)
# 2) sales-sample로 apply → 전환 검사(tests/test_switch.py)와 스냅샷 열 정보 대조
# 3) chart-gallery로 apply → 차트 유형별 데이터 조회(tests/test_gallery.py)와 스냅샷 열 정보 대조
# 4) make new로 예시 CSV 폴더에서 초안(projects/zz-newtest, 끝나면 지움)을 만들어 apply → tests/test_new_project.py
# 끝나면 테스트 스택과 그 볼륨, .test/를 지운다(KEEP=1이면 남긴다). 실패가 하나라도 있으면 종료 코드 1.
test:
	@before=$$(cat .state/*.json 2>/dev/null | cksum); \
	$(TEST_ENV) $(MAKE) --no-print-directory _test; rc=$$?; \
	after=$$(cat .state/*.json 2>/dev/null | cksum); \
	if [ "$$before" != "$$after" ]; then echo "✖ 테스트가 개발 스택의 .state/를 바꿈"; rc=1; fi; \
	if [ $$rc -eq 0 ]; then echo "✔ make test 통과"; else echo "✖ make test 실패"; fi; \
	exit $$rc

_test:
	@[ "$$COMPOSE_PROJECT_NAME" = "$(TEST_NAME)" ] || { echo "✖ _test는 make test가 부른다"; exit 1; }
	@mkdir -p .test/state .test/build
	@rc=0; $(MAKE) --no-print-directory _test_run || rc=1; \
	if [ "$(KEEP)" = 1 ]; then \
	  echo "• KEEP=1: 테스트 스택을 남김(웹앱 :$(TEST_WEB_PORT), Superset :$(TEST_SUPERSET_PORT)). 내리기: make test-down"; \
	else \
	  $(MAKE) --no-print-directory test-down; \
	fi; \
	exit $$rc

# 테스트 스택과 그 볼륨만 지운다(프로젝트 이름을 직접 지정하므로 개발 스택에는 닿지 않는다)
test-down:
	$(COMPOSE) -p $(TEST_NAME) down -v --remove-orphans
	rm -rf ./.test

_test_run:
	@[ "$$COMPOSE_PROJECT_NAME" = "$(TEST_NAME)" ] || { echo "✖ _test_run은 make test가 부른다"; exit 1; }
	@a=devtracker; b=sales-sample; rc=0; \
	echo "== 테스트 스택 기동($(TEST_NAME)), PROJECT=$$a apply"; \
	PROJECT=$$a $(MAKE) --no-print-directory up apply || exit 1; \
	echo "== 1단계: PROJECT=$$a 전체 테스트"; \
	PROJECT=$$a $(TOOLS) python -m pytest -q -p no:cacheprovider --ignore-glob='tests/test_studio*.py' tests/ || rc=1; \
	echo "== Studio 단계: Studio API만으로 CSV → 프로젝트 → 적용 → 탭 변경 → 데이터 교체"; \
	if PROJECT=$$a $(MAKE) --no-print-directory studio; then \
	  PROJECT=$$a $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) -e STUDIO_TEST_URL=http://studio:8090 tools \
	    python -m pytest -q -p no:cacheprovider tests/test_studio_integration.py || rc=1; \
	else rc=1; fi; \
	echo "== 2단계: PROJECT=$$b 로 전환해 범용성 검사"; \
	if PROJECT=$$b $(MAKE) --no-print-directory apply; then \
	  PROJECT=$$b $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) -e SWITCH_FROM=$$a tools \
	    python -m pytest -q -p no:cacheprovider tests/test_switch.py tests/test_bundle_golden.py || rc=1; \
	else rc=1; fi; \
	echo "== 3단계: PROJECT=chart-gallery 로 전환해 차트 유형별 데이터 조회 확인"; \
	if PROJECT=chart-gallery $(MAKE) --no-print-directory apply; then \
	  PROJECT=chart-gallery $(TOOLS) python -m pytest -q -p no:cacheprovider tests/test_gallery.py tests/test_bundle_golden.py || rc=1; \
	else rc=1; fi; \
	echo "== 4단계: 예시 CSV 폴더에서 초안을 만들어(make new) check·apply가 되는지 확인"; \
	rm -rf ./projects/zz-newtest; \
	if $(MAKE) --no-print-directory new NAME=zz-newtest FROM=projects/sales-sample/data \
	   && PROJECT=zz-newtest $(MAKE) --no-print-directory apply; then \
	  PROJECT=zz-newtest $(TOOLS) python -m pytest -q -p no:cacheprovider tests/test_new_project.py || rc=1; \
	else rc=1; fi; \
	rm -rf ./projects/zz-newtest; \
	exit $$rc

# ---------------------------------------------------------------- 초기화
reset:
	@if [ "$(CONFIRM)" != 1 ]; then \
	  echo "⚠ make reset은 DB 볼륨(데이터 DB, Superset 메타데이터)과 .state/, .build/를 모두 지웁니다."; \
	  echo "  계속하려면: make reset CONFIRM=1"; \
	  exit 1; \
	fi
	$(COMPOSE) down -v --remove-orphans
	rm -rf ./.state ./.build
	@echo "✔ reset 완료. 다시 시작: make up && make apply"
