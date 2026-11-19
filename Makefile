# 호스트에는 Docker와 make만 있으면 된다. 파이썬 작업은 모두 tools 컨테이너에서 돈다.
SHELL := /bin/sh
.DEFAULT_GOAL := help

COMPOSE      ?= docker compose
WAIT_TIMEOUT ?= 300
# tools가 만든 파일(.state/, .build/, 캐시)이 호스트 사용자 소유가 되도록 같은 uid:gid로 실행한다
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
TOOLS    := $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) tools

.NOTPARALLEL:
.PHONY: help env up check load sync apply test test-unit test-down unit-image golden reset down logs dirs _test _test_run

# ---- 테스트 스택(보강설계서 3-2, 7절 D5): 개발 스택과 이름·포트·볼륨·상태 디렉터리가 모두 다르다.
# 상태·빌드 디렉터리는 .test/ 고정이다(아래 정리 단계가 이 경로를 그대로 쓴다).
TEST_NAME          := dashkit-test
TEST_WEB_PORT      ?= 18080
TEST_SUPERSET_PORT ?= 18088
TEST_ENV := COMPOSE_PROJECT_NAME=$(TEST_NAME) \
  WEB_PORT=$(TEST_WEB_PORT) SUPERSET_PORT=$(TEST_SUPERSET_PORT) \
  WEB_ORIGINS=http://localhost:$(TEST_WEB_PORT),http://127.0.0.1:$(TEST_WEB_PORT) \
  SUPERSET_PUBLIC_URL=http://localhost:$(TEST_SUPERSET_PORT) \
  STATE_DIR=/app/.test/state BUILD_DIR=/app/.test/build HOST_STATE_DIR=./.test/state
# 단위 테스트는 compose 없이 tools 이미지만으로 돈다(.env도 필요 없다)
APP_IMAGE := dashkit-app:local
UNIT_RUN  := docker run --rm --user $(HOST_UID):$(HOST_GID) -v "$(CURDIR)":/app -w /app $(APP_IMAGE)

help:
	@echo "make env     .env 생성(없을 때만, 비밀값 자동 생성)"
	@echo "make up      전체 빌드·기동 후 superset/backend/web healthy 대기"
	@echo "make check   설정 검사"
	@echo "make load    CSV 적재"
	@echo "make sync    Superset 반영"
	@echo "make apply   check → load → sync → backend 재시작"
	@echo "make test-unit  단위 테스트(스택 불필요: 설정 검증, 번들 스냅샷, CSV 검증, 도메인 단어)"
	@echo "make test    통합 테스트(별도 테스트 스택을 띄우고 끝나면 내림. KEEP=1이면 남김)"
	@echo "make golden  번들 스냅샷(tests/golden/) 갱신"
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

up: dirs
	$(COMPOSE) up -d --build
	$(call wait_healthy,superset backend web)

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

# ---------------------------------------------------------------- 설정 반영
check: dirs
	$(TOOLS) python -m dashkit check

load: dirs
	$(TOOLS) python -m dashkit load

sync: dirs
	$(TOOLS) python -m dashkit sync

apply: check load sync
	@# 템플릿 코드가 바뀌었으면 backend 이미지를 다시 만든다(바뀐 게 없으면 캐시로 즉시 끝남).
	@# 설정(projects/)은 시작할 때 읽으므로 항상 재시작한다.
	$(COMPOSE) up -d --build --no-deps backend
	$(COMPOSE) restart backend
	$(call wait_healthy,backend)

# ---------------------------------------------------------------- 테스트
unit-image:
	@docker build -q -t $(APP_IMAGE) -f dashkit/Dockerfile . > /dev/null

test-unit: unit-image
	$(UNIT_RUN) python -m pytest -q -p no:cacheprovider -m "not integration" tests/

# 번들 스냅샷 갱신. COLUMNS=1이면 먼저 현재 스택의 DB에서 PROJECT의 뷰 열 정보를 다시 읽는다(스택 필요).
golden: unit-image
ifeq ($(COLUMNS),1)
	$(TOOLS) python -m tests.golden --columns
endif
	$(UNIT_RUN) python -m tests.golden

# 통합 테스트는 별도 compose 프로젝트(dashkit-test)에서 돈다. 개발 스택과 .state/, .build/는 건드리지 않는다.
# 1) devtracker로 up·apply → 전체 pytest
# 2) sales-sample로 apply → 전환 검사(tests/test_switch.py)와 스냅샷 열 정보 대조
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
	docker compose -p $(TEST_NAME) down -v --remove-orphans
	rm -rf ./.test

_test_run:
	@[ "$$COMPOSE_PROJECT_NAME" = "$(TEST_NAME)" ] || { echo "✖ _test_run은 make test가 부른다"; exit 1; }
	@a=devtracker; b=sales-sample; rc=0; \
	echo "== 테스트 스택 기동($(TEST_NAME)), PROJECT=$$a apply"; \
	PROJECT=$$a $(MAKE) --no-print-directory up apply || exit 1; \
	echo "== 1단계: PROJECT=$$a 전체 테스트"; \
	PROJECT=$$a $(TOOLS) python -m pytest -q -p no:cacheprovider tests/ || rc=1; \
	echo "== 2단계: PROJECT=$$b 로 전환해 범용성 검사"; \
	if PROJECT=$$b $(MAKE) --no-print-directory apply; then \
	  PROJECT=$$b $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) -e SWITCH_FROM=$$a tools \
	    python -m pytest -q -p no:cacheprovider tests/test_switch.py tests/test_bundle_golden.py || rc=1; \
	else rc=1; fi; \
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
