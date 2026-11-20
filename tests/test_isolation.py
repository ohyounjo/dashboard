"""작업 폴더 분리(README "여러 작업 폴더에서 동시에"). Docker 없이 Makefile과 compose 파일의 글자로 확인한다.

같은 컴퓨터에서 복제본·git worktree 여러 개가 동시에 빌드·테스트해도 서로의 이미지와 테스트 스택을 덮어쓰지 않으려면
(1) 직접 빌드하는 이미지의 태그, (2) 테스트 스택의 compose 프로젝트 이름, (3) 테스트 포트가 폴더마다 달라야 한다.
실제 동시 실행은 사람이 `make info`와 `make test`로 확인한다.
"""
from __future__ import annotations

import re

from conftest import ROOT

MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_built_images_take_the_folder_suffix():
    images = re.findall(r"^\s+image:\s*(\S.*)$", COMPOSE, flags=re.M)
    built = [i for i in images if i.startswith("dashkit-")]
    assert len(built) == 5, built            # superset-init, superset, backend, tools, studio
    for image in built:
        assert image.endswith("${IMAGE_SUFFIX:-}"), f"직접 빌드하는 이미지에 접미사가 없음: {image}"
    # 받아 쓰는 이미지(postgres, nginx)는 고정 버전 그대로다
    assert all("IMAGE_SUFFIX" not in i for i in images if not i.startswith("dashkit-"))


def test_makefile_derives_names_from_the_folder():
    assert re.search(r'^WT_SUM := .*\$\(CURDIR\).*cksum', MAKEFILE, flags=re.M), "식별자는 작업 폴더 경로에서 나와야 함"
    assert re.search(r"^IMAGE_SUFFIX \?= -\$\(WT_ID\)$", MAKEFILE, flags=re.M)
    assert re.search(r"^export IMAGE_SUFFIX$", MAKEFILE, flags=re.M), "compose가 읽도록 내보내야 함"
    assert re.search(r"^APP_IMAGE := dashkit-app:local\$\(IMAGE_SUFFIX\)$", MAKEFILE, flags=re.M)
    assert re.search(r"^STUDIO_IMAGE := dashkit-studio:local\$\(IMAGE_SUFFIX\)$", MAKEFILE, flags=re.M)
    assert re.search(r"^TEST_NAME\s+:= dashkit-test-\$\(WT_ID\)$", MAKEFILE, flags=re.M)
    assert re.search(r"^TEST_PORT_BASE\s+\?= .*\$\(WT_SUM\)", MAKEFILE, flags=re.M), "테스트 포트도 폴더에서 나와야 함"
    for name in ("TEST_WEB_PORT", "TEST_SUPERSET_PORT", "TEST_STUDIO_PORT"):
        assert re.search(rf"^{name}\s+\?= .*\$\(TEST_PORT_BASE\)", MAKEFILE, flags=re.M), name
    # 고정된 테스트 포트나 이름이 남아 있으면 안 된다
    assert not re.search(r"\b180(80|88|90)\b", MAKEFILE)
    assert not re.search(r"-p dashkit-test\b(?!-)", MAKEFILE)


def test_stack_targets_are_guarded_against_another_folders_stack():
    guarded = re.search(r"^GUARDED := (.+)$", MAKEFILE, flags=re.M)
    assert guarded, "다른 폴더의 스택을 건드리지 않게 하는 확인 목록이 없음"
    names = set(guarded.group(1).split())
    # 컨테이너를 만들거나 지우거나, 스택의 DB·Superset에 쓰는 목표는 모두 확인을 거쳐야 한다
    assert {"up", "down", "reset", "apply", "load", "sync", "studio", "studio-down", "env-secrets"} <= names
    assert re.search(r"^\$\(GUARDED\): _guard_project$", MAKEFILE, flags=re.M)
    assert MAKEFILE.index("$(GUARDED): _guard_project") < MAKEFILE.index("\nup:"), "확인 줄이 목표 규칙보다 앞에 있어야 함"
    # 테스트 스택을 지우는 목표는 프로젝트 이름을 직접 지정한다(개발 스택에 닿지 않는다)
    down = re.search(r"^test-down:\n\t(.+)$", MAKEFILE, flags=re.M)
    assert down and "-p $(TEST_NAME)" in down.group(1) and "--profile studio" in down.group(1)
