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
.PHONY: help env up check load sync apply test reset down logs dirs

help:
	@echo "make env     .env 생성(없을 때만, 비밀값 자동 생성)"
	@echo "make up      전체 빌드·기동 후 superset/backend/web healthy 대기"
	@echo "make check   설정 검사"
	@echo "make load    CSV 적재"
	@echo "make sync    Superset 반영"
	@echo "make apply   check → load → sync → backend 재시작"
	@echo "make test    테스트"
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

# 1) 현재 프로젝트로 전체 pytest
# 2) 다른 예시 프로젝트로 apply → 전환 검사(tests/test_switch.py) → 원래 프로젝트로 다시 apply
# 어느 단계가 실패해도 복원 apply는 실행하고, 실패가 하나라도 있으면 종료 코드 1.
test: dirs
	@orig="$${PROJECT:-$$(sed -n 's/^PROJECT=//p' .env)}"; \
	other=$$( [ "$$orig" = devtracker ] && echo sales-sample || echo devtracker ); \
	rc=0; \
	echo "== 1단계: PROJECT=$$orig 전체 테스트"; \
	PROJECT=$$orig $(TOOLS) python -m pytest -q -p no:cacheprovider tests/ || rc=1; \
	echo "== 2단계: PROJECT=$$other 로 전환해 범용성 검사"; \
	if PROJECT=$$other $(MAKE) --no-print-directory apply; then \
	  PROJECT=$$other $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) -e SWITCH_FROM=$$orig tools \
	    python -m pytest -q -p no:cacheprovider tests/test_switch.py || rc=1; \
	else rc=1; fi; \
	echo "== 복원: PROJECT=$$orig 로 다시 apply"; \
	PROJECT=$$orig $(MAKE) --no-print-directory apply || rc=1; \
	if [ $$rc -eq 0 ]; then echo "✔ make test 통과"; else echo "✖ make test 실패"; fi; \
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
