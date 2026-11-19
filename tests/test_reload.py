"""backend가 projects/<PROJECT>/의 설정 변경을 다시 읽는다(dashkit/api/state.py의 ProjectState). 스택 없이 돈다.

저장소의 projects/는 건드리지 않는다. 임시 폴더에 복사한 프로젝트를 PROJECTS_DIR로 가리킨다.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from dashkit import config
from dashkit.api.state import CurrentState, ProjectState, file_signature
from dashkit.config import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def root(tmp_path) -> Path:
    shutil.copytree(FIXTURES / "basic", tmp_path / "basic")
    return tmp_path


@pytest.fixture
def records():
    """dashkit.api 로거의 기록(이 로거는 propagate를 끄므로 직접 붙인다)."""
    out: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            out.append(record)

    logger = logging.getLogger("dashkit.api")
    handler = Collect(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield out
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def set_title(root: Path, title: str, *, atomic: bool = False) -> None:
    path = root / "basic" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["app"]["title"] = title
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    if atomic:   # 편집기·git이 하듯 새 파일로 바꿔치기(inode가 바뀐다)
        tmp = path.with_name("dashboard.yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    else:
        path.write_text(text, encoding="utf-8")


def break_config(root: Path) -> None:
    path = root / "basic" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["dashboards"]["main"]["charts"][0]["dataset"] = "no_such_dataset"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def counting_loader():
    calls: list[int] = []

    def loader(name: str, projects_dir: Path) -> config.Project:
        calls.append(1)
        return config.load_project(name, projects_dir)

    return loader, calls


def errors_logged(records) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- ProjectState

def test_reads_once_while_files_are_unchanged(root):
    loader, calls = counting_loader()
    state = ProjectState("basic", root, loader)
    first = state.project()
    for _ in range(50):
        assert state.project() is first
    assert len(calls) == 1
    assert state.status() == {"valid": True, "errors": 0, "reloads": 0}


@pytest.mark.parametrize("atomic", [False, True])
def test_change_is_detected(root, atomic):
    loader, calls = counting_loader()
    state = ProjectState("basic", root, loader)
    assert state.project().dashboard["app"]["title"] == "테스트"
    set_title(root, "바뀐 제목입니다", atomic=atomic)
    assert state.project().dashboard["app"]["title"] == "바뀐 제목입니다"
    assert state.project().dashboard["app"]["title"] == "바뀐 제목입니다"
    assert len(calls) == 2 and state.status()["reloads"] == 1


def test_schema_file_change_is_detected_too(root):
    state = ProjectState("basic", root)
    path = root / "basic" / "schema.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["tables"]["owners"]["columns"]["team"] = {"type": "text"}
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert "team" in state.project().schema["tables"]["owners"]["columns"]


def test_broken_config_keeps_last_valid_and_logs_once(root, records):
    loader, calls = counting_loader()
    state = ProjectState("basic", root, loader)
    good = state.project()
    original = (root / "basic" / "dashboard.yaml").read_text(encoding="utf-8")
    break_config(root)
    for _ in range(20):
        assert state.project() is good                      # 마지막으로 유효했던 설정
    assert len(calls) == 2                                  # 같은 서명에서는 다시 읽지 않는다
    logged = errors_logged(records)
    assert len([m for m in logged if "마지막으로 유효했던 설정" in m]) == 1
    assert len([m for m in logged if "no_such_dataset" in m]) == 1
    assert state.status() == {"valid": False, "errors": 1, "reloads": 0}
    assert "no_such_dataset" in state.errors()[0]

    # 고치면 다시 읽고 상태가 돌아온다
    (root / "basic" / "dashboard.yaml").write_text(original, encoding="utf-8")
    set_title(root, "고친 뒤의 제목")
    fixed = state.project()
    assert fixed is not good and fixed.dashboard["app"]["title"] == "고친 뒤의 제목"
    assert state.status() == {"valid": True, "errors": 0, "reloads": 1}
    assert len(errors_logged(records)) == len(logged)


def test_half_written_yaml_and_missing_file_keep_last_valid(root, records):
    state = ProjectState("basic", root)
    good = state.project()
    path = root / "basic" / "dashboard.yaml"
    original = path.read_text(encoding="utf-8")
    path.write_text(original[: len(original) // 2] + "\n  - [닫지 않은", encoding="utf-8")
    assert state.project() is good and state.status()["valid"] is False
    path.unlink()
    assert state.project() is good and state.status()["valid"] is False
    path.write_text(original, encoding="utf-8")
    assert state.status()["valid"] is True
    assert state.project().dashboard == good.dashboard


def test_broken_config_at_start_raises(root):
    break_config(root)
    with pytest.raises(ConfigError):
        ProjectState("basic", root)


def test_concurrent_requests_reload_once_per_change(root):
    loader, calls = counting_loader()
    state = ProjectState("basic", root, loader)
    stop = threading.Event()
    failures: list[BaseException] = []
    titles: set[str] = set()

    def reader() -> None:
        try:
            while not stop.is_set():
                titles.add(state.project().dashboard["app"]["title"])
        except BaseException as exc:  # noqa: BLE001 - 스레드의 실패를 본 스레드로 옮긴다
            failures.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    written = ["테스트"]
    try:
        for i in range(5):
            written.append(f"제목 {i} " + "가" * i)
            set_title(root, written[-1], atomic=True)
            assert state.project().dashboard["app"]["title"] == written[-1]
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert failures == []
    assert titles <= set(written)
    assert len(calls) == 6          # 처음 1번 + 변경 5번. 스레드가 여럿이어도 변경마다 한 번만 읽는다


def test_file_signature_states(tmp_path):
    assert file_signature(tmp_path / "none") == ("missing",)
    f = tmp_path / "a"
    f.write_text("1")
    a = file_signature(f)
    f.write_text("22")
    assert file_signature(f) != a


# ---------------------------------------------------------------- app(HTTP 없이 엔드포인트 함수를 직접 부른다)

@pytest.fixture
def api(root, tmp_path, monkeypatch):
    from dashkit.api import app as app_module
    from dashkit.api import data

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("STATE_DIR", str(state_dir))
    monkeypatch.setattr(app_module, "ctx", app_module._Ctx())
    monkeypatch.setattr(data, "ping", lambda: True)
    app_module._load()
    return app_module


def health(app_module) -> dict:
    return json.loads(app_module.get_health().body)


def test_api_serves_changed_config_without_restart(api, root):
    assert api.get_app()["app"]["title"] == "테스트"
    assert health(api)["config"] == {"valid": True, "errors": 0}

    def edit(doc):
        doc["app"]["title"] = "다시 읽은 제목"
        doc["tabs"] = [t for t in doc["tabs"] if t["key"] == "main"]

    path = root / "basic" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    body = api.get_app()
    assert body["app"]["title"] == "다시 읽은 제목"
    assert [t["key"] for t in body["tabs"]] == ["main"]
    # 위젯 섹션이 없어졌으므로 /api/data 허용 목록도 새 설정을 따른다
    with pytest.raises(api.ApiError) as exc:
        api.get_data("things")
    assert exc.value.detail == "unknown_dataset"


def test_api_keeps_serving_last_valid_config_and_health_says_so(api, root, records):
    break_config(root)
    assert api.get_app()["app"]["title"] == "테스트"
    body = health(api)
    assert body["status"] == "ok" and body["config"] == {"valid": False, "errors": 1}
    assert api.get_health().status_code == 200
    for _ in range(5):
        api.get_app()
    assert len([m for m in errors_logged(records) if "마지막으로 유효했던 설정" in m]) == 1


# ---------------------------------------------------------------- 현재 프로젝트(embed.json의 project를 따른다)

def write_embed(state_dir: Path, project, dashboards=("main",)) -> None:
    doc = {"version": 1, "project": project, "synced_at": "2026-11-20T00:00:00Z",
           "dashboards": {k: {"dashboard_uuid": f"d-{k}", "embed_uuid": f"e-{project}-{k}"} for k in dashboards}}
    tmp = state_dir / "embed.json.tmp"
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, state_dir / "embed.json")       # sync처럼 원자적으로


def add_project(root: Path, name: str, title: str) -> None:
    shutil.copytree(FIXTURES / "basic", root / name)
    path = root / name / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["app"]["title"] = title
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path) -> Path:
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return d


def test_current_project_is_env_project_without_embed_file(root, state_dir):
    cur = CurrentState("basic", state_dir / "embed.json", root).current()
    assert (cur.project.name, cur.source, cur.embeds) == ("basic", "env", {})


def test_current_project_follows_embed_file(root, state_dir):
    add_project(root, "other", "다른 프로젝트")
    state = CurrentState("basic", state_dir / "embed.json", root)
    write_embed(state_dir, "basic")
    cur = state.current()
    assert (cur.project.name, cur.source, cur.embeds) == ("basic", "env", {"main": "e-basic-main"})

    write_embed(state_dir, "other")                 # 다른 프로젝트를 sync함(Studio의 apply, PROJECT=other make apply)
    cur = state.current()
    assert (cur.project.name, cur.source) == ("other", "embed")
    assert cur.project.dashboard["app"]["title"] == "다른 프로젝트"
    assert cur.embeds == {"main": "e-other-main"}

    write_embed(state_dir, "basic")                 # 되돌아옴
    cur = state.current()
    assert (cur.project.name, cur.embeds) == ("basic", {"main": "e-basic-main"})


def test_followed_project_reloads_config_changes_and_keeps_last_valid(root, state_dir, records):
    add_project(root, "other", "처음 제목")
    write_embed(state_dir, "other")
    state = CurrentState("basic", state_dir / "embed.json", root)
    assert state.current().project.dashboard["app"]["title"] == "처음 제목"

    path = root / "other" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["app"]["title"] = "고친 제목입니다"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cur = state.current()
    assert cur.project.dashboard["app"]["title"] == "고친 제목입니다" and cur.valid

    path.write_text("tabs: [", encoding="utf-8")    # 깨진 설정: 마지막으로 유효했던 설정을 유지하고 env PROJECT로 넘어가지 않는다
    for _ in range(3):
        cur = state.current()
        assert cur.project.name == "other" and cur.project.dashboard["app"]["title"] == "고친 제목입니다"
        assert not cur.valid and len(cur.errors) == 1
        assert cur.embeds == {"main": "e-other-main"}
    assert len([m for m in errors_logged(records) if "마지막으로 유효했던 설정" in m]) == 1


@pytest.mark.parametrize("case", ["missing_folder", "invalid_config", "bad_name", "broken_json", "no_project_key"])
def test_unusable_embed_project_falls_back_to_env_project(root, state_dir, records, case):
    """embed.json을 쓸 수 없으면 env PROJECT를 보여 주고, 그 프로젝트는 미적용(임베드 없음)이다."""
    if case == "missing_folder":
        write_embed(state_dir, "gone")
    elif case == "invalid_config":
        add_project(root, "other", "x")
        (root / "other" / "dashboard.yaml").write_text("tabs: [", encoding="utf-8")
        write_embed(state_dir, "other")
    elif case == "bad_name":
        write_embed(state_dir, "../basic")
    elif case == "broken_json":
        (state_dir / "embed.json").write_text("{", encoding="utf-8")
    else:
        (state_dir / "embed.json").write_text(json.dumps({"version": 1, "dashboards": {}}), encoding="utf-8")
    loader, calls = counting_loader()
    state = CurrentState("basic", state_dir / "embed.json", root, loader)
    for _ in range(5):
        cur = state.current()
        assert (cur.project.name, cur.source, cur.embeds) == ("basic", "env", {})
    # 쓸 수 없는 프로젝트를 요청마다 다시 검증하지 않는다(파일이 바뀔 때까지)
    assert len(calls) == (2 if case == "invalid_config" else 1)
    if case == "invalid_config":
        assert len([m for m in errors_logged(records) if "env PROJECT를 씁니다" in m]) == 1


def test_embed_project_is_picked_up_once_its_config_becomes_valid(root, state_dir):
    add_project(root, "other", "나중에 고침")
    good = (root / "other" / "dashboard.yaml").read_text(encoding="utf-8")
    (root / "other" / "dashboard.yaml").write_text("tabs: [", encoding="utf-8")
    write_embed(state_dir, "other")
    state = CurrentState("basic", state_dir / "embed.json", root)
    assert state.current().project.name == "basic"
    (root / "other" / "dashboard.yaml").write_text(good + "\n", encoding="utf-8")
    cur = state.current()
    assert (cur.project.name, cur.embeds) == ("other", {"main": "e-other-main"})


def test_removed_project_folder_is_not_served_from_memory(root, state_dir):
    add_project(root, "other", "지워질 프로젝트")
    write_embed(state_dir, "other")
    state = CurrentState("basic", state_dir / "embed.json", root)
    assert state.current().project.name == "other"
    shutil.rmtree(root / "other")
    cur = state.current()
    assert (cur.project.name, cur.source, cur.embeds) == ("basic", "env", {})


def test_starts_with_embed_project_even_if_env_project_is_unusable(root, state_dir):
    write_embed(state_dir, "basic")
    cur = CurrentState("no-such-project", state_dir / "embed.json", root).current()
    assert (cur.project.name, cur.source) == ("basic", "embed")
    (state_dir / "embed.json").unlink()
    with pytest.raises(ConfigError):                # 둘 다 쓸 수 없으면 시작하지 않는다
        CurrentState("no-such-project", state_dir / "embed.json", root)


def test_keeps_last_state_when_nothing_is_usable(root, state_dir):
    write_embed(state_dir, "basic")
    state = CurrentState("no-such-project", state_dir / "embed.json", root)
    before = state.current()
    (state_dir / "embed.json").unlink()
    cur = state.current()
    assert cur.project is before.project and cur.source == "stale" and not cur.valid
    assert state.current() is cur


def test_all_endpoints_use_the_same_current_project(api, root, state_dir, monkeypatch):
    """/api/app, /api/health, /api/guest-token, /api/data가 모두 embed.json의 프로젝트를 따른다."""
    monkeypatch.setenv("GUEST_TOKEN_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("GUEST_TOKEN_JWT_AUDIENCE", "aud")
    add_project(root, "other", "다른 프로젝트")
    path = root / "other" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["dashboards"]["second"] = doc["dashboards"].pop("main")
    for tab in doc["tabs"]:
        for sec in tab["sections"]:
            if sec.get("dashboard") == "main":
                sec["dashboard"] = "second"
    doc["tabs"] = [t for t in doc["tabs"] if any(s["type"] == "superset" for s in t["sections"])]
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 처음: embed.json이 없으므로 env PROJECT, 미적용
    body = api.get_app()
    assert body["project"] == "basic" and body["applied"] is False
    with pytest.raises(api.ApiError) as exc:
        api.post_guest_token(api.GuestTokenRequest(dashboard="main"))
    assert (exc.value.status, exc.value.detail) == (409, "not_applied")

    write_embed(state_dir, "other", dashboards=("second",))
    body = api.get_app()
    assert body["project"] == "other" and body["app"]["title"] == "다른 프로젝트" and body["applied"] is True
    assert [s["embed_id"] for t in body["tabs"] for s in t["sections"] if s["type"] == "superset"] == ["e-other-second"]
    assert health(api)["project"] == "other" and health(api)["applied"] is True
    assert api.post_guest_token(api.GuestTokenRequest(dashboard="second"))["token"]
    with pytest.raises(api.ApiError) as exc:        # 이전 프로젝트의 대시보드 키는 이제 설정에 없다
        api.post_guest_token(api.GuestTokenRequest(dashboard="main"))
    assert (exc.value.status, exc.value.detail) == (404, "unknown_dashboard")
    with pytest.raises(api.ApiError) as exc:        # 위젯 탭을 뺐으므로 허용 데이터셋도 없다
        api.get_data("things")
    assert exc.value.detail == "unknown_dataset"
