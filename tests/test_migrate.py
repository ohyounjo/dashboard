"""설정 버전(config.CONFIG_VERSION)과 dashkit migrate. DB 없이 돈다."""
from __future__ import annotations

import shutil
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from dashkit import config, migrate
from dashkit.__main__ import main
from dashkit.config import ConfigError, load_project

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = Path(__file__).resolve().parent.parent / "projects"
FILES = ("schema.yaml", "dashboard.yaml")


def copy_basic(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES / "basic", tmp_path / "basic")
    return tmp_path


def prepend(path: Path, text: str) -> None:
    path.write_text(text + path.read_text(encoding="utf-8"), encoding="utf-8")


# ---------------------------------------------------------------- version 검사

def test_version_is_optional_and_defaults_to_one(tmp_path):
    p = load_project("basic", copy_basic(tmp_path))
    assert p.schema["version"] == 1 and p.dashboard["version"] == 1
    assert config.config_version({}) == 1


def test_version_one_is_accepted(tmp_path):
    root = copy_basic(tmp_path)
    for f in FILES:
        prepend(root / "basic" / f, "version: 1\n")
    p = load_project("basic", root)
    assert p.schema["version"] == 1 and p.dashboard["version"] == 1


@pytest.mark.parametrize("file", FILES)
def test_unknown_version_is_one_clear_error(tmp_path, file):
    root = copy_basic(tmp_path)
    prepend(root / "basic" / file, "version: 2\nsome_future_key: 1\n")   # 형식 오류는 버전 오류에 가려진다
    with pytest.raises(ConfigError) as exc:
        load_project("basic", root)
    assert len(exc.value.errors) == 1, exc.value.errors
    err = exc.value.errors[0]
    assert err.startswith(f"{file}:version: ")
    assert "설정 버전 1까지 지원" in err and "버전 2" in err and "업그레이드" in err


@pytest.mark.parametrize("value", ['"1"', "0", "1.5", "true"])
def test_version_must_be_a_positive_integer(tmp_path, value):
    root = copy_basic(tmp_path)
    prepend(root / "basic" / "schema.yaml", f"version: {value}\n")
    with pytest.raises(ConfigError) as exc:
        load_project("basic", root)
    assert any(e.startswith("schema.yaml:version: ") for e in exc.value.errors), exc.value.errors


def test_older_version_points_to_migrate(tmp_path, monkeypatch):
    """버전이 올라간 뒤의 dashkit은 옛 파일을 읽지 않고 migrate를 안내한다."""
    monkeypatch.setattr(config, "CONFIG_VERSION", 2)
    root = copy_basic(tmp_path)
    with pytest.raises(ConfigError) as exc:
        load_project("basic", root)
    assert [e.split(": ")[0] for e in exc.value.errors] == ["schema.yaml:version", "dashboard.yaml:version"]
    assert all("dashkit migrate" in e for e in exc.value.errors)


@pytest.mark.parametrize("name", ["devtracker", "sales-sample", "chart-gallery"])
def test_example_projects_pass_without_a_version_key(name):
    """완료 조건: 기존 예시는 고치지 않아도 통과한다."""
    for f in FILES:
        assert "version" not in yaml.safe_load((PROJECTS / name / f).read_text(encoding="utf-8"))
    load_project(name, PROJECTS)


# ---------------------------------------------------------------- migrate(텍스트)

def test_insert_version_keeps_every_other_byte():
    text = "# 머리 주석\ntables:   # 줄 끝 주석\n  a: { x: 1 }   \n\n# 꼬리 주석\n"
    out, before, steps = migrate.migrate_text("schema.yaml", text)
    assert out == "version: 1\n\n" + text
    assert before == 1 and len(steps) == 1
    assert yaml.safe_load(out) == {"version": 1, **yaml.safe_load(text)}


def test_migrate_text_is_idempotent():
    once, _b, _s = migrate.migrate_text("schema.yaml", "tables: {}\n")
    twice, before, steps = migrate.migrate_text("schema.yaml", once)
    assert twice == once and steps == [] and before == 1


def test_insert_version_respects_bom_document_marker_and_newlines():
    out, _b, _s = migrate.migrate_text("schema.yaml", "\ufeff# 주석\n---\ntables: {}\n")
    assert out == "\ufeff# 주석\n---\nversion: 1\n\ntables: {}\n"
    out, _b, _s = migrate.migrate_text("schema.yaml", "# 주석\r\ntables: {}\r\n")
    assert out == "version: 1\r\n\r\n# 주석\r\ntables: {}\r\n"
    out, _b, _s = migrate.migrate_text("schema.yaml", "\ntables: {}\n")
    assert out == "version: 1\n\ntables: {}\n"


def test_migrate_text_rejects_newer_and_malformed():
    with pytest.raises(ConfigError) as exc:
        migrate.migrate_text("dashboard.yaml", "version: 3\napp: {}\n")
    assert exc.value.errors[0].startswith("dashboard.yaml:version: ") and "업그레이드" in exc.value.errors[0]
    with pytest.raises(ConfigError) as exc:
        migrate.migrate_text("dashboard.yaml", "app: [1, 2\n")
    assert "YAML 문법 오류" in exc.value.errors[0]
    with pytest.raises(ConfigError):
        migrate.migrate_text("dashboard.yaml", "- 목록\n")


def test_registered_step_runs_and_bumps_the_version_line(monkeypatch):
    """1 → 2가 생기면 함수 하나를 등록하면 된다. version 줄은 migrate가 고치고 주석은 남는다."""
    def v1_to_v2(file: str, text: str) -> str:
        return text.replace("old_key:", "new_key:")

    monkeypatch.setattr(migrate, "CONFIG_VERSION", 2)
    monkeypatch.setitem(migrate.MIGRATIONS, 1, v1_to_v2)
    out, before, steps = migrate.migrate_text("schema.yaml", "version: 1   # 형식 버전\nold_key: 1  # 그대로\n")
    assert out == "version: 2   # 형식 버전\nnew_key: 1  # 그대로\n"
    assert before == 1 and steps == ["버전 1 → 2"]
    # version 키가 없는 파일은 1로 보고 같은 길을 간다
    out, _b, steps = migrate.migrate_text("schema.yaml", "old_key: 1\n")
    assert out == "version: 2\n\nnew_key: 1\n" and len(steps) == 2
    assert migrate.migrate_text("schema.yaml", out)[2] == []


def test_missing_step_is_an_error(monkeypatch):
    monkeypatch.setattr(migrate, "CONFIG_VERSION", 2)
    with pytest.raises(ConfigError) as exc:
        migrate.migrate_text("schema.yaml", "tables: {}\n")
    assert "변환이 등록되어 있지 않음" in exc.value.errors[0]


# ---------------------------------------------------------------- migrate(파일, CLI)

def test_migrate_project_writes_once_and_result_still_loads(tmp_path):
    root = copy_basic(tmp_path)
    before = {f: (root / "basic" / f).read_text(encoding="utf-8") for f in FILES}
    results = migrate.migrate_project(root / "basic")
    assert [r.changed for r in results] == [True, True]
    for f in FILES:
        assert (root / "basic" / f).read_text(encoding="utf-8") == "version: 1\n\n" + before[f]
    assert sorted(p.name for p in (root / "basic").iterdir()) == ["dashboard.yaml", "schema.yaml"]   # 임시 파일이 남지 않음
    load_project("basic", root)
    again = migrate.migrate_project(root / "basic")
    assert [r.changed for r in again] == [False, False]


def test_migrate_check_changes_nothing(tmp_path):
    root = copy_basic(tmp_path)
    before = {f: (root / "basic" / f).read_bytes() for f in FILES}
    results = migrate.migrate_project(root / "basic", check=True)
    assert [r.changed for r in results] == [False, False]
    assert {f: (root / "basic" / f).read_bytes() for f in FILES} == before


def test_migrate_writes_nothing_when_one_file_fails(tmp_path):
    root = copy_basic(tmp_path)
    prepend(root / "basic" / "dashboard.yaml", "version: 9\n")
    before = (root / "basic" / "schema.yaml").read_bytes()
    with pytest.raises(ConfigError):
        migrate.migrate_project(root / "basic")
    assert (root / "basic" / "schema.yaml").read_bytes() == before


def test_cli_migrate_and_check_exit_codes(tmp_path, monkeypatch, capsys):
    root = copy_basic(tmp_path)
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    before = (root / "basic" / "schema.yaml").read_bytes()

    assert main(["migrate", "--check"]) == 0
    assert (root / "basic" / "schema.yaml").read_bytes() == before
    assert "이미 최신" in capsys.readouterr().out

    assert main(["migrate"]) == 0
    assert (root / "basic" / "schema.yaml").read_bytes().startswith(b"version: 1\n")
    assert main(["migrate", "--check"]) == 0
    assert main(["migrate"]) == 0
    assert "이미 최신" in capsys.readouterr().out
    assert main(["check"]) == 0

    monkeypatch.setenv("PROJECT", "Bad Name")
    assert main(["migrate"]) == 1


@pytest.mark.parametrize("versions", [(False, False), (True, True), (False, True)])
def test_cli_check_current_versions(tmp_path, monkeypatch, capsys, versions):
    root = copy_basic(tmp_path) / "basic"
    for file, explicit in zip(FILES, versions):
        if explicit:
            prepend(root / file, "version: 1\n")
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    before = {f: (root / f).read_bytes() for f in FILES}
    assert main(["migrate", "--check"]) == 0
    output = capsys.readouterr()
    assert "이미 최신" in output.out and output.err == ""
    assert {f: (root / f).read_bytes() for f in FILES} == before


@pytest.mark.parametrize("value", ['"1"', "0", "1.5", "true", "null", "2", "[broken"])
def test_cli_check_invalid_file_is_read_only(tmp_path, monkeypatch, capsys, value):
    root = copy_basic(tmp_path) / "basic"
    prepend(root / "dashboard.yaml", f"version: {value}\n")
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    before = {f: (root / f).read_bytes() for f in FILES}
    assert main(["migrate", "--check"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "dashboard.yaml:" in output.err
    assert {f: (root / f).read_bytes() for f in FILES} == before
    assert main(["migrate"]) == 1
    assert {f: (root / f).read_bytes() for f in FILES} == before


def test_cli_future_migration_and_idempotence(tmp_path, monkeypatch, capsys):
    root = copy_basic(tmp_path) / "basic"
    prepend(root / "dashboard.yaml", "version: 1\n")
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(migrate, "CONFIG_VERSION", 2)
    monkeypatch.setitem(migrate.MIGRATIONS, 1, lambda file, text: text)
    before = {f: (root / f).read_bytes() for f in FILES}
    assert main(["migrate", "--check"]) == 1
    output = capsys.readouterr()
    assert all(f in output.out for f in FILES)
    assert "버전 1 → 2" in output.out and "맨 위에 넣음" not in output.out
    assert "파일은 바꾸지 않음" in output.err
    assert {f: (root / f).read_bytes() for f in FILES} == before
    assert main(["migrate"]) == 0
    after = {f: (root / f).read_bytes() for f in FILES}
    assert all(b"version: 2" in b for b in after.values())
    assert main(["migrate"]) == 0
    assert {f: (root / f).read_bytes() for f in FILES} == after


def test_cli_missing_migration_and_file(tmp_path, monkeypatch, capsys):
    root = copy_basic(tmp_path) / "basic"
    monkeypatch.setenv("PROJECT", "basic")
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(migrate, "CONFIG_VERSION", 2)
    before = {f: (root / f).read_bytes() for f in FILES}
    assert main(["migrate", "--check"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and all(f in output.err for f in FILES)
    assert "변환이 등록되어 있지 않음" in output.err
    assert {f: (root / f).read_bytes() for f in FILES} == before
    (root / "dashboard.yaml").unlink()
    assert main(["migrate", "--check"]) == 1
    assert "파일이 없음" in capsys.readouterr().err
    assert (root / "schema.yaml").read_bytes() == before["schema.yaml"]


@pytest.mark.parametrize("name", ["devtracker", "sales-sample", "chart-gallery"])
def test_cli_check_original_examples(name, monkeypatch, capsys):
    monkeypatch.setenv("PROJECT", name)
    monkeypatch.setenv("PROJECTS_DIR", str(PROJECTS))
    before = {f: (PROJECTS / name / f).read_bytes() for f in FILES}
    assert main(["migrate", "--check"]) == 0
    output = capsys.readouterr()
    assert "이미 최신" in output.out and output.err == ""
    assert {f: (PROJECTS / name / f).read_bytes() for f in FILES} == before


@pytest.mark.parametrize("invalid", [False, True])
def test_process_check_exit_and_streams(tmp_path, invalid):
    root = copy_basic(tmp_path) / "basic"
    if invalid:
        prepend(root / "dashboard.yaml", "version: null\n")
    before = {f: (root / f).read_bytes() for f in FILES}
    env = {**os.environ, "PROJECT": "basic", "PROJECTS_DIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "-m", "dashkit", "migrate", "--check"],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == int(invalid)
    if invalid:
        assert result.stdout == "" and "dashboard.yaml:version:" in result.stderr
    else:
        assert "이미 최신" in result.stdout and result.stderr == ""
    assert {f: (root / f).read_bytes() for f in FILES} == before
