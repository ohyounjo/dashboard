"""설정 검사(dashkit/config.py). DB 없이 돈다."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from dashkit import config
from dashkit.config import ConfigError, load_project

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = Path(__file__).resolve().parent.parent / "projects"


def make_project(tmp_path: Path, mutate_dash=None, mutate_schema=None, name="basic") -> Path:
    dst = tmp_path / name
    shutil.copytree(FIXTURES / "basic", dst)
    for fname, fn in (("dashboard.yaml", mutate_dash), ("schema.yaml", mutate_schema)):
        if fn:
            p = dst / fname
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            fn(doc)
            p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp_path


_counter = iter(range(10**6))


def errors_of(tmp_path, **kw) -> list[str]:
    base = tmp_path / f"case{next(_counter)}"
    with pytest.raises(ConfigError) as exc:
        load_project("basic", make_project(base, **kw))
    return exc.value.errors


# ---------------------------------------------------------------- 통과

def test_fixture_loads(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    assert p.schema["tables"]["things"]["primary_key"] == ["id"]
    assert p.schema["tables"]["things"]["columns"]["id"]["required"] is True  # PK는 required
    assert config.table_order(p) == ["owners", "things"]
    assert config.widget_datasets(p) == {"things"}
    sec = p.dashboard["tabs"][0]["sections"][0]
    assert sec["title"] is None and sec["height"] is None  # 선택 필드는 None으로 채움


@pytest.mark.parametrize("name", sorted(d.name for d in PROJECTS.iterdir() if (d / "dashboard.yaml").exists())
                         if PROJECTS.exists() else [])
def test_example_projects_pass_check(name):
    load_project(name, PROJECTS)


def test_example_projects_exist():
    names = {d.name for d in PROJECTS.iterdir()} if PROJECTS.exists() else set()
    assert {"devtracker", "sales-sample"} <= names


# ---------------------------------------------------------------- 요구된 3가지 오류

def test_unknown_dataset_reference(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["dataset"] = "nope"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("charts[0].dataset" in e and "'nope'" in e for e in errs), errs


def test_layout_chart_key_not_in_charts(tmp_path):
    def m(d):
        d["dashboards"]["main"]["layout"][1][0] = "ghost"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("layout[1][0]" in e and "'ghost'" in e for e in errs), errs
    assert any("'c_table'가 layout에 배치되지 않음" in e for e in errs), errs


def test_filter_column_not_in_view(tmp_path):
    def m(d):
        d["dashboards"]["main"]["filters"][0]["column"] = "no_such_col"
    p = load_project("basic", make_project(tmp_path, mutate_dash=m))  # sql 데이터셋은 정적으로 통과
    view_cols = {"things": {"id", "owner_id", "phase", "score", "begins", "ends", "owner_label"},
                 "owners": {"id", "label"}}
    errs = config.check_view_columns(p, view_cols)
    assert errs == ["dashboard.yaml:dashboards.main.filters[0].column: 데이터셋 'things' 뷰에 열 'no_such_col'가 없음"]


def test_filter_column_missing_on_table_dataset_is_static(tmp_path):
    def m(d):
        d["dashboards"]["main"]["filters"][0].update(dataset="owners", column="no_such_col")
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("filters[0].column" in e and "no_such_col" in e for e in errs), errs


# ---------------------------------------------------------------- 그 밖의 참조 검사

def test_unknown_metric_name(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["metric"] = "missing_metric"
    assert any("지표 'missing_metric'" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_duplicate_chart_key(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][1]["key"] = "c_total"
    assert any("중복" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_section_refs(tmp_path):
    def m(d):
        d["tabs"][0]["sections"][0]["dashboard"] = "nope"
        d["tabs"][1]["sections"][1]["dataset"] = "nope2"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("sections[0].dashboard" in e for e in errs)
    assert any("sections[1].dataset" in e for e in errs)


def test_row_too_wide(tmp_path):
    def m(d):
        d["dashboards"]["main"]["layout"][0][1]["width"] = 12
    assert any("12칸" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_reserved_word_column(tmp_path):
    def m(s):
        s["tables"]["things"]["columns"]["end"] = {"type": "date"}
    assert any("'end'은 SQL 예약어" in e for e in errors_of(tmp_path, mutate_schema=m))


def test_ref_cycle_and_bad_ref(tmp_path):
    def m(s):
        s["tables"]["owners"]["columns"]["thing_id"] = {"type": "text", "ref": "things.id"}
    assert any("순환" in e for e in errors_of(tmp_path, mutate_schema=m))

    def m2(s):
        s["tables"]["things"]["columns"]["owner_id"]["ref"] = "owners.label"
    assert any("단일 열 primary_key가 아님" in e for e in errors_of(tmp_path, mutate_schema=m2))


def test_json_schema_errors_have_paths(tmp_path):
    def m(d):
        d["tabs"][0]["icon"] = "rocket"
        d["dashboards"]["main"]["charts"][1].pop("x")
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any(e.startswith("dashboard.yaml:tabs[0].icon:") for e in errs), errs
    assert any(e.startswith("dashboard.yaml:dashboards.main.charts[1]") for e in errs), errs


def test_all_errors_collected(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["dataset"] = "nope"
        d["dashboards"]["main"]["layout"][1][0] = "ghost"
        d["tabs"][0]["sections"][0]["dashboard"] = "nope"
    assert len(errors_of(tmp_path, mutate_dash=m)) >= 3


# ---------------------------------------------------------------- 도구 함수

def test_object_uuid_is_deterministic():
    a = config.object_uuid("p", "chart", "k")
    assert a == config.object_uuid("p", "chart", "k")
    assert a != config.object_uuid("q", "chart", "k")
    assert config.object_uuid("devtracker", "chart", "kpi_progress") == "1a48d16a-b967-5f07-b247-5f1864bafae2"


def test_layout_rows_widths(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    rows = config.layout_rows(p.dashboard["dashboards"]["main"])
    assert [(i["chart"], i["width"]) for i in rows[0]] == [("c_total", 4), ("c_line", 8)]
    assert [i["width"] for i in rows[1]] == [6, 6]
    assert all(i["height"] == 50 for r in rows for i in r)


def test_referenced_columns(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    refs = config.referenced_columns(p)
    assert set(refs["things"]) == {"begins", "id", "phase", "owner_label", "ends", "score"}
    assert "owners" not in refs  # viz_type 원본 차트의 params는 검사하지 않음


def test_resolve_as_of(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    assert config.resolve_as_of(p) == date(2026, 1, 15)

    def m(d):
        d["app"]["as_of"] = "today"
    p2 = load_project("basic", make_project(tmp_path / "x", mutate_dash=m))
    assert config.resolve_as_of(p2, today=date(2030, 5, 1)) == date(2030, 5, 1)
