"""위젯 매니페스트(dashkit/widgets.py, web/widgets/<유형>/widget.json). DB와 스택 없이 돈다.

보강설계서 4-2의 완료 조건 "위젯 추가가 폴더 하나"를 확인한다: 임시 위젯 폴더에 견본 매니페스트 하나를
더하면 스키마 검증, 기본값, 열 참조 수집, /api/data 허용 목록, /api/app 위젯 목록에 그 유형이 반영된다.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from dashkit import config, widgets
from dashkit.config import ConfigError, load_project

from test_config import errors_of, make_project

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = ROOT / "web" / "widgets"

SAMPLE = {
    "type": "sample_list",
    "label": "견본 목록",
    "options": {
        "type": "object",
        "required": ["dataset", "item"],
        "properties": {
            "dataset": {"$ref": "#/$defs/key"},
            "item": {"$ref": "#/$defs/column", "x-column": True},
            "extras": {"type": "array", "items": {"$ref": "#/$defs/column", "x-column": True}, "default": []},
            "note": {"type": "string", "default": None},
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "by": {"$ref": "#/$defs/column", "x-column": True, "default": None},
                    },
                },
                "x-unique": "name",
                "default": [],
            },
            "period": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from", "to"],
                "properties": {"from": {"$ref": "#/$defs/date"}, "to": {"$ref": "#/$defs/date"}},
                "x-date-range": ["from", "to"],
                "default": None,
            },
        },
    },
}


def write_manifest(widgets_dir: Path, folder: str, doc) -> Path:
    d = widgets_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
    (d / "widget.json").write_text(text, encoding="utf-8")
    return widgets_dir


@pytest.fixture
def widgets_dir(tmp_path, monkeypatch) -> Path:
    """저장소의 위젯 폴더 사본. WIDGETS_DIR이 이 폴더를 가리킨다."""
    dst = tmp_path / "widgets"
    shutil.copytree(WIDGETS, dst)
    monkeypatch.setenv("WIDGETS_DIR", str(dst))
    widgets.clear_cache()
    yield dst
    widgets.clear_cache()


def add_sample_tab(section: dict):
    def mutate(d):
        d["tabs"].append({"key": "extra", "label": "추가", "icon": "grid", "sections": [section]})
    return mutate


# ---------------------------------------------------------------- 저장소의 매니페스트

def test_builtin_widgets_are_folders():
    types = widgets.load_registry(WIDGETS)
    folders = sorted(p.name for p in WIDGETS.iterdir() if p.is_dir())
    assert list(types) == folders and folders
    for name in folders:
        assert (WIDGETS / name / "widget.js").is_file(), name
        assert types[name].label


def test_no_widget_type_names_outside_manifests():
    """유형 이름은 매니페스트에만 있다: config.py, 셸, 계약 스키마 파일에 박힌 이름이 없어야 한다."""
    names = list(widgets.load_registry(WIDGETS))
    pattern = re.compile(r"(?<![a-z0-9_])(" + "|".join(map(re.escape, names)) + r")(?![a-z0-9_])")
    for rel in ("dashkit/config.py", "dashkit/widgets.py", "dashkit/api/app.py", "web/app.js"):
        hits = [line for line in (ROOT / rel).read_text(encoding="utf-8").splitlines() if pattern.search(line)]
        assert hits == [], f"{rel}: {hits}"
    raw = json.loads((ROOT / "contracts" / "dashboard.schema.json").read_text(encoding="utf-8"))
    assert raw["$defs"]["section"]["properties"]["type"]["enum"] == ["superset"]
    assert not [k for k in raw["$defs"] if k.startswith("s_") and k != "s_superset"]


def test_schema_is_composed_from_manifests():
    defs = config.load_schema("dashboard.schema.json")["$defs"]
    names = list(widgets.registry())
    assert defs["section"]["properties"]["type"]["enum"] == ["superset", *names]
    assert [c["then"]["$ref"] for c in defs["section"]["allOf"]] == [f"#/$defs/s_{n}" for n in ["superset", *names]]
    for n in names:
        s = defs[f"s_{n}"]
        assert s["additionalProperties"] is False and s["required"][0] == "type"
        assert s["properties"]["type"] is True and s["properties"]["title"]["type"] == "string"


# ---------------------------------------------------------------- 옮긴 뒤에도 그대로인 동작

def test_defaults_filled_as_before(tmp_path):
    def m(d):
        kanban = d["tabs"][1]["sections"][1]
        kanban["card"].pop("chips")
        kanban.pop("filter")
    p = load_project("basic", make_project(tmp_path, mutate_dash=m))
    gantt, kanban = p.dashboard["tabs"][1]["sections"]
    assert gantt["title"] is None and gantt["range"] is None and gantt["sort"] is None
    assert gantt["statuses"] == {"a": {"label": "에이", "tone": "good"}}   # 표시 사전 항목은 채우지 않는다
    assert gantt["fields"] == {"label": "id", "start": "begins", "end": "ends", "progress": "score", "status": None}
    assert kanban["title"] is None and kanban["filter"] is None
    assert kanban["columns"] == [
        {"value": "a", "label": "A", "icon": None, "tone": None},
        {"value": "b", "label": "B", "icon": "✅", "tone": None},
    ]
    assert kanban["card"] == {"title": "id", "chips": []}
    assert config.widget_datasets(p) == {"things"}


def test_referenced_column_paths_as_before(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    paths = sorted(x for cols in config.referenced_columns(p).values() for ps in cols.values() for x in ps
                   if x.startswith("tabs"))
    assert paths == sorted([
        "tabs[1].sections[0].fields.label", "tabs[1].sections[0].fields.start", "tabs[1].sections[0].fields.end",
        "tabs[1].sections[0].fields.progress",
        "tabs[1].sections[1].group_by", "tabs[1].sections[1].card.title", "tabs[1].sections[1].card.chips[0]",
        "tabs[1].sections[1].filter.column",
    ])


def test_type_checks_keep_messages(tmp_path):
    def dup(d):
        d["tabs"][1]["sections"][1]["columns"].append({"value": "a", "label": "또 A"})
    assert "dashboard.yaml:tabs[1].sections[1].columns: 칸 value가 중복됨 (a)" in errors_of(tmp_path, mutate_dash=dup)

    def late(d):
        d["tabs"][1]["sections"][0]["range"] = {"start": "2026-02-01", "end": "2026-01-01"}
    assert "dashboard.yaml:tabs[1].sections[0].range: start가 end보다 늦음" in errors_of(tmp_path, mutate_dash=late)

    def bad_date(d):
        d["tabs"][1]["sections"][0]["range"] = {"start": "2026-02-30", "end": "2026-03-01"}
    assert "dashboard.yaml:tabs[1].sections[0].range: 날짜 형식 오류" in errors_of(tmp_path, mutate_dash=bad_date)

    def unknown_key(d):
        d["tabs"][1]["sections"][0]["nope"] = 1
    errs = errors_of(tmp_path, mutate_dash=unknown_key)
    assert any(e.startswith("dashboard.yaml:tabs[1].sections[0]:") and "'nope'" in e for e in errs), errs

    def missing(d):
        d["tabs"][1]["sections"][1].pop("group_by")
    errs = errors_of(tmp_path, mutate_dash=missing)
    assert errs == ["dashboard.yaml:tabs[1].sections[1]: 'group_by' is a required property"]

    def bad_col(d):
        d["tabs"][1]["sections"][1]["dataset"] = "owners"
    errs = errors_of(tmp_path, mutate_dash=bad_col)
    assert "dashboard.yaml:tabs[1].sections[1].group_by: 데이터셋 'owners' 뷰에 열 'phase'가 없음" in errs


# ---------------------------------------------------------------- 위젯 추가 = 폴더 하나

def test_unknown_type_is_rejected_without_its_folder(tmp_path, widgets_dir):
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({"type": "sample_list", "dataset": "owners", "item": "label"}))
    assert any(e.startswith("dashboard.yaml:tabs[2].sections[0].type:") and "sample_list" in e for e in errs), errs


def test_new_widget_is_one_folder(tmp_path, widgets_dir):
    write_manifest(widgets_dir, "sample_list", SAMPLE)
    widgets.clear_cache()

    # 스키마 합성과 기본값
    section = {"type": "sample_list", "dataset": "owners", "item": "label", "groups": [{"name": "g1"}]}
    p = load_project("basic", make_project(tmp_path / "ok", mutate_dash=add_sample_tab(section)))
    sec = p.dashboard["tabs"][2]["sections"][0]
    assert sec == {
        "type": "sample_list", "title": None, "dataset": "owners", "item": "label", "extras": [], "note": None,
        "groups": [{"name": "g1", "by": None}], "period": None,
    }
    defs = config.load_schema("dashboard.schema.json")["$defs"]
    assert "sample_list" in defs["section"]["properties"]["type"]["enum"] and "s_sample_list" in defs

    # 열 참조 수집("x-column")과 /api/data 허용 목록
    assert config.referenced_columns(p)["owners"] == {"label": ["tabs[2].sections[0].item"]}
    assert config.widget_datasets(p) == {"things", "owners"}

    # 스키마 검증: 필수 키, 모르는 키, 값 형식
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({"type": "sample_list", "dataset": "owners"}))
    assert errs == ["dashboard.yaml:tabs[2].sections[0]: 'item' is a required property"]
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({**section, "other": 1}))
    assert any(e.startswith("dashboard.yaml:tabs[2].sections[0]:") and "'other'" in e for e in errs), errs
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({**section, "note": 3}))
    assert any(e.startswith("dashboard.yaml:tabs[2].sections[0].note:") for e in errs), errs

    # 참조 검사: 데이터셋, 열(table 데이터셋은 정적으로 확인), 표시로 선언한 검사
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({**section, "dataset": "nope"}))
    assert errs == ["dashboard.yaml:tabs[2].sections[0].dataset: 데이터셋 'nope'이 없음"]
    bad_cols = {**section, "item": "no_col", "extras": ["id", "no_col2"], "groups": [{"name": "g", "by": "no_col3"}]}
    assert errors_of(tmp_path, mutate_dash=add_sample_tab(bad_cols)) == [
        "dashboard.yaml:tabs[2].sections[0].item: 데이터셋 'owners' 뷰에 열 'no_col'가 없음",
        "dashboard.yaml:tabs[2].sections[0].extras[1]: 데이터셋 'owners' 뷰에 열 'no_col2'가 없음",
        "dashboard.yaml:tabs[2].sections[0].groups[0].by: 데이터셋 'owners' 뷰에 열 'no_col3'가 없음",
    ]
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({**section, "groups": [{"name": "g"}, {"name": "g"}]}))
    assert errs == ["dashboard.yaml:tabs[2].sections[0].groups: 항목 name가 중복됨 (g)"]
    errs = errors_of(tmp_path, mutate_dash=add_sample_tab({**section, "period": {"from": "2026-03-01", "to": "2026-01-01"}}))
    assert errs == ["dashboard.yaml:tabs[2].sections[0].period: from가 to보다 늦음"]


def test_new_widget_is_listed_by_api_app(tmp_path, widgets_dir, monkeypatch):
    from dashkit.api import app as api_app
    from dashkit.api.state import CurrentState

    write_manifest(widgets_dir, "sample_list", SAMPLE)
    widgets.clear_cache()
    section = {"type": "sample_list", "dataset": "owners", "item": "label"}
    root = make_project(tmp_path, mutate_dash=add_sample_tab(section))
    project = load_project("basic", root)
    # backend는 현재 프로젝트를 CurrentState로 읽는다(embed.json이 없으면 env PROJECT 자리의 이름)
    monkeypatch.setattr(api_app.ctx, "state", CurrentState(project.name, tmp_path / "embed.json", root), raising=False)

    body = api_app.get_app()
    builtin = [{"type": t.type, "label": t.label} for t in widgets.load_registry(WIDGETS).values()]
    assert body["widgets"] == sorted([*builtin, {"type": "sample_list", "label": "견본 목록"}], key=lambda w: w["type"])
    sec = body["tabs"][2]["sections"][0]
    assert sec["type"] == "sample_list" and sec["extras"] == [] and sec["period"] is None

    # /api/data 허용 목록에도 들어간다(허용하지 않는 데이터셋은 404, DB에 닿기 전에 걸러진다)
    assert "owners" in config.widget_datasets(project)
    with pytest.raises(api_app.ApiError) as exc:
        api_app.get_data("not_referenced")
    assert (exc.value.status, exc.value.detail) == (404, "unknown_dataset")


def test_widget_without_dataset(tmp_path, widgets_dir):
    doc = {"type": "note", "label": "글", "options": {"type": "object", "required": ["text"],
                                                      "properties": {"text": {"type": "string"}}}}
    write_manifest(widgets_dir, "note", doc)
    widgets.clear_cache()
    p = load_project("basic", make_project(tmp_path, mutate_dash=add_sample_tab({"type": "note", "text": "x"})))
    assert p.dashboard["tabs"][2]["sections"][0] == {"type": "note", "title": None, "text": "x"}
    assert config.widget_datasets(p) == {"things"}
    assert not any(path.startswith("tabs[2]") for cols in config.referenced_columns(p).values()
                   for paths in cols.values() for path in paths)


# ---------------------------------------------------------------- 잘못된 매니페스트

def _sample(**changes) -> dict:
    doc = json.loads(json.dumps(SAMPLE))
    doc.update(changes)
    return doc


def _options(fn) -> dict:
    doc = json.loads(json.dumps(SAMPLE))
    fn(doc["options"])
    return doc


BAD_MANIFESTS = [
    ("label 없음", {k: v for k, v in SAMPLE.items() if k != "label"}, "'label'가 필요함"),
    ("type 없음", {k: v for k, v in SAMPLE.items() if k != "type"}, "'type'가 필요함"),
    ("options 없음", {k: v for k, v in SAMPLE.items() if k != "options"}, "'options'가 필요함"),
    ("폴더 이름과 type 불일치", _sample(type="other_name"), "폴더 이름 'sample_list'와 다름"),
    ("type 형식", _sample(type="../sample_list"), "type 형식 오류"),
    ("모르는 키", _sample(extra=1), "알 수 없는 키"),
    ("JSON 문법", "{ not json", "JSON 문법 오류"),
    ("객체 아님", [1, 2], "JSON 객체가 아님"),
    ("options가 스키마 아님", _options(lambda o: o["properties"].update(bad={"type": "nope", "default": 1})),
     "JSON Schema가 아님"),
    ("공통 키 재정의", _options(lambda o: o["properties"].update(title={"type": "string", "default": None})),
     "공통 키 'title'"),
    ("required가 properties에 없음", _options(lambda o: o["required"].append("ghost")), "'ghost'가 properties에 없음"),
    ("선택 옵션에 default 없음", _options(lambda o: o["properties"]["note"].pop("default")), "'note'에 default가 필요함"),
    ("dataset이 선택", _options(lambda o: (o["required"].remove("dataset"),
                                          o["properties"]["dataset"].update(default=None))), "'dataset' 옵션은 required"),
    ("dataset 없이 x-column", _options(lambda o: (o["required"].remove("dataset"), o["properties"].pop("dataset"))),
     "'dataset' 옵션이 있어야 함"),
    ("x-column 값", _options(lambda o: o["properties"]["item"].update({"x-column": "yes"})), "\"x-column\"은 true만"),
    ("x-unique 값", _options(lambda o: o["properties"]["groups"].update({"x-unique": 3})), "\"x-unique\""),
    ("x-date-range 값", _options(lambda o: o["properties"]["period"].update({"x-date-range": ["from"]})),
     "\"x-date-range\""),
]


@pytest.mark.parametrize("doc,expected", [(d, e) for _, d, e in BAD_MANIFESTS], ids=[n for n, _, _ in BAD_MANIFESTS])
def test_bad_manifest_is_a_clear_error(tmp_path, doc, expected):
    write_manifest(tmp_path, "sample_list", doc)
    with pytest.raises(widgets.ManifestError) as exc:
        widgets.load_registry(tmp_path)
    assert str(exc.value).startswith("sample_list/widget.json: ") and expected in str(exc.value), str(exc.value)


def test_reserved_type_and_missing_files(tmp_path):
    write_manifest(tmp_path / "a", "superset", _sample(type="superset"))
    with pytest.raises(widgets.ManifestError, match="위젯 이름으로 쓸 수 없음"):
        widgets.load_registry(tmp_path / "a")
    (tmp_path / "b" / "empty").mkdir(parents=True)
    with pytest.raises(widgets.ManifestError, match=r"empty/widget\.json: 파일이 없음"):
        widgets.load_registry(tmp_path / "b")
    with pytest.raises(widgets.ManifestError, match="위젯 폴더가 없음"):
        widgets.load_registry(tmp_path / "missing")


def test_bad_manifest_fails_project_load(tmp_path, widgets_dir):
    write_manifest(widgets_dir, "sample_list", _sample(type="other_name"))
    widgets.clear_cache()
    with pytest.raises(ConfigError) as exc:
        load_project("basic", make_project(tmp_path))
    assert exc.value.errors == ["sample_list/widget.json: type 'other_name'이 폴더 이름 'sample_list'와 다름"]
