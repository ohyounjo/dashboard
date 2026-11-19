"""Studio가 YAML을 다시 쓸 때 손으로 쓴 주석·키 순서·모양이 남는다(보강설계서 8절 5a 완료 조건). 스택 없이 돈다.

세 예시 프로젝트의 설정 파일은 **읽기만 한다**(글을 읽어 메모리에서 고친다). 저장소의 projects/는 바뀌지 않는다.
"""
from __future__ import annotations

import difflib
import random

import pytest
import yaml

from studio_client import EXAMPLES, ROOT

from dashkit.studio import ops, roundtrip, store, yamledit

DASHBOARDS = {name: (ROOT / "projects" / name / "dashboard.yaml").read_text(encoding="utf-8") for name in EXAMPLES}
ALL_FILES = sorted([*ROOT.glob("projects/*/*.yaml"), *ROOT.glob("tests/fixtures/*/*.yaml")])


def edit(text: str, op_list: list[dict]) -> tuple[str, str]:
    """연산을 적용해 다시 쓴 글과 쓴 방식. 결과가 기대한 값으로 읽히는지는 store.edit_yaml이 확인한다."""
    return store.edit_yaml(text, lambda doc: ops.apply_ops(doc, op_list))


def line_changes(old: str, new: str) -> tuple[list[str], list[str]]:
    """(없어진 줄, 새로 생긴 줄). 나머지 줄은 바이트 단위로 같고 순서도 같다."""
    a, b = old.split("\n"), new.split("\n")
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += a[i1:i2]
            added += b[j1:j2]
    return removed, added


def comments(text: str) -> list[str]:
    return sorted(line.split("#", 1)[1] for line in text.split("\n") if "#" in line and '"#' not in line)


def tabs_of(text: str) -> list[dict]:
    return yaml.safe_load(text)["tabs"]


# ---------------------------------------------------------------- 바꾸지 않으면 그대로

@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_unchanged_document_is_byte_identical(path):
    """모든 예시 설정 파일을 조각으로 나눴다가 다시 이어 붙이면 원문과 같다(다룰 수 있는 파일이다)."""
    text = path.read_text(encoding="utf-8")
    doc = yamledit.Document(text)
    assert doc.render() == text
    assert yamledit.plain(doc.root) == yaml.safe_load(text)


# ---------------------------------------------------------------- 세 예시: 탭 이동, 섹션 추가, 옵션 변경

@pytest.mark.parametrize("name", EXAMPLES)
def test_tab_move_keeps_every_line(name):
    text = DASHBOARDS[name]
    keys = [t["key"] for t in tabs_of(text)]
    new, how = edit(text, [{"op": "tab.move", "key": keys[-1], "index": 0}])
    assert how == "exact"
    assert [t["key"] for t in tabs_of(new)] == [keys[-1], *keys[:-1]]
    # 줄의 집합이 같다: 옮긴 탭의 줄들이 자리만 바뀌었고 글자는 하나도 바뀌지 않았다
    assert sorted(new.split("\n")) == sorted(text.split("\n"))
    assert comments(new) == comments(text)
    # 탭 목록 앞의 내용(app, datasets, dashboards)은 바이트 단위로 같다
    head = text[:text.index("\ntabs:")]
    assert new.startswith(head)
    # 되돌리면 원문과 완전히 같다
    back, how = edit(new, [{"op": "tab.move", "key": keys[-1], "index": len(keys) - 1}])
    assert how == "exact" and back == text


@pytest.mark.parametrize("name", EXAMPLES)
def test_option_change_touches_only_its_line(name):
    text = DASHBOARDS[name]
    new, how = edit(text, [{"op": "app.update", "values": {"title": "고친 제목: 검사용"}}])
    removed, added = line_changes(text, new)
    assert how == "exact" and len(removed) == 1 and len(added) == 1
    assert removed[0].strip().startswith("title:") and added == ['  title: "고친 제목: 검사용"']

    # superset 섹션에 옵션 하나 추가: 그 섹션이 있는 줄만 바뀐다
    tab, index = next((t["key"], i) for t in tabs_of(text) for i, s in enumerate(t["sections"]) if s["type"] == "superset")
    new, how = edit(text, [{"op": "section.update", "tab": tab, "index": index, "values": {"height": 640}}])
    removed, added = line_changes(text, new)
    assert how == "exact" and len(removed) <= 1 and len(added) == 1 and "height: 640" in added[0]
    assert comments(new) == comments(text)

    # 다시 지우면 원문과 완전히 같다
    back, how = edit(new, [{"op": "section.update", "tab": tab, "index": index, "values": {"height": None}}])
    assert how == "exact" and back == text


@pytest.mark.parametrize("name", EXAMPLES)
def test_section_add_keeps_all_other_lines(name):
    text = DASHBOARDS[name]
    doc = yaml.safe_load(text)
    tab = doc["tabs"][-1]["key"]
    dashboard = next(iter(doc["dashboards"]))
    new, how = edit(text, [{"op": "section.add", "tab": tab, "section": {"type": "superset", "dashboard": dashboard,
                                                                         "title": "추가한 섹션", "height": None}}])
    assert how == "exact"
    assert tabs_of(new)[-1]["sections"][-1] == {"type": "superset", "dashboard": dashboard, "title": "추가한 섹션"}
    removed, added = line_changes(text, new)
    # 여러 줄 목록에는 줄이 더해지기만 한다. 한 줄 목록(`sections: [{ … }]`)이면 그 한 줄만 바뀐다
    assert len(removed) <= 1 and 1 <= len(added) <= 5
    assert all("sections" in line for line in removed)
    assert comments(new) == comments(text)
    back, how = edit(new, [{"op": "section.remove", "tab": tab, "index": len(tabs_of(new)[-1]["sections"]) - 1}])
    assert how == "exact" and yaml.safe_load(back) == doc


def test_long_new_section_unfolds_only_that_list():
    text = DASHBOARDS["devtracker"]
    section = {"type": "kanban", "dataset": "tasks", "group_by": "stage",
               "columns": [{"value": "todo", "label": "할 일"}, {"value": "done", "label": "끝"}], "card": {"title": "title"}}
    new, how = edit(text, [{"op": "section.add", "tab": "overview", "section": section}])
    removed, added = line_changes(text, new)
    assert how == "exact"
    assert removed == ["    sections: [{ type: superset, dashboard: overview }]"]
    assert added == [
        "    sections:",
        "      - { type: superset, dashboard: overview }",        # 있던 항목은 글자 그대로
        "      - type: kanban",
        "        dataset: tasks",
        "        group_by: stage",
        "        columns: [{ value: todo, label: 할 일 }, { value: done, label: 끝 }]",
        "        card: { title: title }",
    ]


def test_block_section_moves_into_one_line_list_with_its_lines_intact():
    text = DASHBOARDS["devtracker"]
    new, how = edit(text, [{"op": "section.move", "tab": "schedule", "index": 0, "to_tab": "issues", "to_index": 1}])
    assert how == "exact"
    removed, added = line_changes(text, new)
    moved = [line for line in text.split("\n")[164:174]]          # gantt 섹션의 줄들
    assert moved[0].strip() == "- type: gantt"
    assert set(moved) <= set(new.split("\n")), "옮긴 섹션의 줄(줄 맞춤 공백, 이모지, 인용 부호)이 그대로여야 함"
    assert "    sections: [{ type: superset, dashboard: issues }]" in removed
    assert "      - { type: superset, dashboard: issues }" in added


# ---------------------------------------------------------------- 주석이 따라가는 규칙

SAMPLE = """\
# 맨 위 설명
version: 1
app:
  title: 견본          # 제목 옆 주석
  as_of: "2026-01-15"

  # 색은 여기서
  theme: { primary: { light: "#112233", dark: "#AABBCC" } }

tabs:                  # 순서대로 표시
  # 첫 탭 설명
  - key: one
    label: 하나         # 라벨 옆 주석
    icon: grid
    sections: [{ type: superset, dashboard: main }]   # 한 줄 목록

  # 둘째 탭 설명
  - key: two
    label: "둘"
    icon:   list
    sections:
      # 섹션 설명
      - type: superset
        dashboard: main    # 대시보드 키
        height: 400
      - { type: superset, dashboard: other }
# 끝 주석
"""


def test_comment_above_a_tab_moves_with_it_and_blank_lines_stay():
    new, how = edit(SAMPLE, [{"op": "tab.move", "key": "two", "index": 0}])
    assert how == "exact"
    lines = new.split("\n")
    i = lines.index("  - key: two")
    assert lines[i - 1] == "  # 둘째 탭 설명" and lines[i - 2].startswith("tabs:")
    j = lines.index("  - key: one")
    assert lines[j - 1] == "  # 첫 탭 설명" and lines[j - 2] == "", "탭 사이의 빈 줄은 자리에 남는다"
    assert sorted(lines) == sorted(SAMPLE.split("\n"))
    assert new.endswith("# 끝 주석\n") and new.startswith("# 맨 위 설명\nversion: 1\n")


def test_same_line_comment_survives_value_change_and_key_removal_takes_its_comment():
    new, how = edit(SAMPLE, [{"op": "app.update", "values": {"title": "새 제목", "theme": None}},
                             {"op": "tab.update", "key": "one", "values": {"label": "첫째"}},
                             {"op": "tab.update", "key": "two", "values": {"icon": "flag"}}])
    assert how == "exact"
    removed, added = line_changes(SAMPLE, new)
    assert "  title: 새 제목          # 제목 옆 주석" in added
    assert "    label: 첫째         # 라벨 옆 주석" in added
    assert "    icon:   flag" in added                         # 키 뒤의 공백도 그대로
    assert set(removed) == {
        "  title: 견본          # 제목 옆 주석", "    label: 하나         # 라벨 옆 주석", "    icon:   list",
        "", "  # 색은 여기서", '  theme: { primary: { light: "#112233", dark: "#AABBCC" } }',
    }


def test_section_edits_inside_block_and_flow_items():
    new, how = edit(SAMPLE, [
        {"op": "section.update", "tab": "two", "index": 0, "values": {"height": 500, "title": "제목"}},
        {"op": "section.update", "tab": "two", "index": 1, "values": {"dashboard": "third"}},
        {"op": "section.move", "tab": "two", "index": 1, "to_index": 0},
    ])
    assert how == "exact"
    removed, added = line_changes(SAMPLE, new)
    assert "        height: 500" in added and "        title: 제목" in added
    assert "      - { type: superset, dashboard: third }" in added
    assert "        dashboard: main    # 대시보드 키" in new       # 건드리지 않은 줄
    lines = new.split("\n")
    assert lines[lines.index("      - type: superset") - 1] == "      # 섹션 설명", "주석은 자기 섹션을 따라간다"
    assert tabs_of(new)[1]["sections"][0] == {"type": "superset", "dashboard": "third"}


def test_emptied_list_keeps_the_key_line_comment():
    new, how = edit(SAMPLE, [{"op": "tab.remove", "key": "one"}, {"op": "tab.remove", "key": "two"}])
    assert how == "exact" and tabs_of(new) == []
    assert "tabs: []                  # 순서대로 표시" in new.split("\n")
    assert new.endswith("# 끝 주석\n")


def test_new_tab_is_written_like_its_neighbours():
    new, how = edit(SAMPLE, [{"op": "tab.add", "tab": {"key": "three", "label": "셋: 새 탭", "icon": "flag"}, "index": 1},
                             {"op": "section.add", "tab": "three", "section": {"type": "superset", "dashboard": "main"}}])
    assert how == "exact"
    removed, added = line_changes(SAMPLE, new)
    assert removed == []
    assert [line for line in added if line] == ["  - key: three", '    label: "셋: 새 탭"', "    icon: flag",
                                                "    sections: [{ type: superset, dashboard: main }]"]


@pytest.mark.parametrize("value", ["yes", "no", "null", "~", "123", "1.5", "2026-01-15", "true", "a: b", "# 주석 아님",
                                   " 앞 공백", "뒤 공백 ", "줄\n바꿈", "따옴표\"와 '작은따옴표'", "{중괄호}", "[대괄호]",
                                   "쉼표, 포함", "*별", "&앰퍼샌드", "!느낌표", "%퍼센트", "@골뱅이", "한글 제목 (괄호)",
                                   "탭\t문자", "이모지 ✅", "끝에 콜론:", "", "-", "- 목록 아님", "0x1F", "1e3", "=",
                                   "줄 구분" + chr(0x2028) + "문자", "다음 줄" + chr(0x85) + "문자", "<<"])
def test_text_values_round_trip_as_text(value):
    """문자열은 다시 읽어도 같은 문자열이다(yes·숫자·날짜 꼴, 특수 문자는 인용 부호를 붙인다)."""
    new, how = edit(SAMPLE, [{"op": "tab.update", "key": "one", "values": {"label": value}},
                             {"op": "section.update", "tab": "one", "index": 0, "values": {"title": value}}])
    assert how == "exact"
    tab = tabs_of(new)[0]
    assert tab["label"] == value and tab["sections"][0]["title"] == value


def test_values_of_other_types_round_trip():
    values = {"count": 3, "ratio": 0.25, "flag": True, "off": False, "names": ["a", "b c", "yes"],
              "nested": {"x": {"y": [1, 2, {"z": "w"}]}, "empty_list": [], "empty_map": {}}}
    new, how = edit(SAMPLE, [{"op": "section.update", "tab": "two", "index": 0, "values": values}])
    assert how == "exact"
    got = tabs_of(new)[1]["sections"][0]
    assert {k: got[k] for k in values} == values and type(got["count"]) is int and got["flag"] is True


# ---------------------------------------------------------------- 연산을 마구 섞어도

@pytest.mark.parametrize("name", EXAMPLES)
@pytest.mark.parametrize("seed", range(4))
def test_random_op_sequences_stay_exact(name, seed):
    """연산을 무작위로 이어 적용해도 매번 "고친 곳만 다시 쓰기"로 처리되고, 결과가 기대한 값으로 읽힌다."""
    rng = random.Random(f"{name}-{seed}")
    text = DASHBOARDS[name]
    dashboards = list(yaml.safe_load(text)["dashboards"])
    untouched = text[:text.index("\ntabs:")]
    for step in range(25):
        tabs = tabs_of(text)
        tab = rng.choice(tabs)
        n = len(tab["sections"])
        choice = rng.randrange(8)
        if choice == 0:
            op_list = [{"op": "tab.move", "key": tab["key"], "index": rng.randrange(len(tabs))}]
        elif choice == 1:
            op_list = [{"op": "section.move", "tab": tab["key"], "index": rng.randrange(n), "to_index": rng.randrange(n)}]
        elif choice == 2 and len(tabs) > 1:
            other = rng.choice([t for t in tabs if t["key"] != tab["key"]])
            if n < 2:
                continue
            op_list = [{"op": "section.move", "tab": tab["key"], "index": rng.randrange(n), "to_tab": other["key"],
                        "to_index": rng.randrange(len(other["sections"]) + 1)}]
        elif choice == 3:
            op_list = [{"op": "section.add", "tab": tab["key"], "index": rng.randrange(n + 1),
                        "section": {"type": "superset", "dashboard": rng.choice(dashboards), "title": f"섹션 {step}"}}]
        elif choice == 4:
            op_list = [{"op": "section.update", "tab": tab["key"], "index": rng.randrange(n),
                        "values": {"title": rng.choice([None, f"제목 {step}", "yes", "a: b"])}}]
        elif choice == 5:
            op_list = [{"op": "tab.update", "key": tab["key"], "values": {"label": f"라벨 {step}", "icon": "flag"}}]
        elif choice == 6:
            key = f"t{step}"
            op_list = [{"op": "tab.add", "tab": {"key": key, "label": f"탭 {step}", "icon": "box"},
                        "index": rng.randrange(len(tabs) + 1)},
                       {"op": "section.add", "tab": key, "section": {"type": "superset", "dashboard": dashboards[0]}}]
        elif choice == 7 and len(tabs) > 2:
            op_list = [{"op": "tab.remove", "key": tab["key"]}]
        else:
            op_list = [{"op": "app.update", "values": {"title": f"제목 {step}", "logo_text": rng.choice([None, "AB"])}}]
        text, how = edit(text, op_list)
        assert how == "exact", (step, op_list)
    # tabs 밖(datasets, dashboards)은 app의 두 줄을 빼고 그대로다
    removed, _added = line_changes(untouched, text[:text.index("\ntabs:")])
    assert all(line.strip().startswith(("title:", "logo_text:")) for line in removed)


# ---------------------------------------------------------------- ruamel round-trip(예비 경로)

ANCHORED = """\
# 앵커가 있는 파일
app:
  title: 견본      # 제목
  as_of: today
shared: &sec { type: superset, dashboard: main }
tabs:
  # 첫 탭
  - key: one
    label: 하나
    icon: grid
    sections: [*sec]
  - key: two     # 둘째
    label: 둘
    icon: list
    sections:
      - type: superset
        dashboard: main
"""


def test_document_the_exact_writer_cannot_handle_falls_back_to_ruamel_and_keeps_comments():
    with pytest.raises(yamledit.Unsupported):
        yamledit.Document(ANCHORED)
    new, how = edit(ANCHORED, [{"op": "tab.move", "key": "two", "index": 0},
                               {"op": "app.update", "values": {"title": "새 제목"}}])
    assert how == "roundtrip"
    doc = yaml.safe_load(new)
    assert [t["key"] for t in doc["tabs"]] == ["two", "one"] and doc["app"]["title"] == "새 제목"
    assert list(doc) == ["app", "shared", "tabs"] and list(doc["app"]) == ["title", "as_of"]      # 키 순서
    for comment in ("# 앵커가 있는 파일", "# 제목", "# 첫 탭", "# 둘째"):
        assert comment in new
    assert "&sec" in new and "*sec" in new


@pytest.mark.parametrize("value", ["yes", "on", "010", "1_000", "2026-01-15", "a: b", "보통 글", "12:30"])
def test_roundtrip_path_quotes_new_text_pyyaml_would_misread(value):
    new, how = edit(ANCHORED, [{"op": "tab.update", "key": "one", "values": {"label": value}}])
    assert how == "roundtrip" and yaml.safe_load(new)["tabs"][0]["label"] == value


@pytest.mark.parametrize("name", EXAMPLES)
def test_ruamel_round_trip_keeps_comments_and_meaning_of_examples(name):
    """예비 경로만으로 다시 써도 주석·키 순서·값은 남는다(모양은 표준 꼴로 바뀐다: 이 경로를 기본으로 쓰지 않는 이유)."""
    text = DASHBOARDS[name]
    new = roundtrip.edit(text, lambda doc: ops.apply_ops(doc, [{"op": "tab.move", "key": doc["tabs"][-1]["key"], "index": 0}]))
    old_doc, new_doc = yaml.safe_load(text), yaml.safe_load(new)
    assert new_doc["tabs"] == [old_doc["tabs"][-1], *old_doc["tabs"][:-1]]
    assert {k: v for k, v in new_doc.items() if k != "tabs"} == {k: v for k, v in old_doc.items() if k != "tabs"}
    assert [list(v) for v in new_doc["dashboards"].values()] == [list(v) for v in old_doc["dashboards"].values()]
    assert len(comments(new)) == len(comments(text))
    assert new != text


def test_mismatch_between_written_text_and_expected_value_is_never_saved(monkeypatch):
    """다시 쓴 글이 기대한 값으로 읽히지 않으면 저장 단계로 가지 않는다."""
    monkeypatch.setattr(yamledit.Document, "render", lambda self: SAMPLE)          # 고치지 않은 글을 돌려주는 고장
    monkeypatch.setattr(roundtrip, "edit", lambda text, mutate: text)
    with pytest.raises(store.StoreError) as exc:
        edit(SAMPLE, [{"op": "tab.move", "key": "two", "index": 0}])
    assert (exc.value.status, exc.value.detail) == (500, "cannot_write")


@pytest.mark.parametrize("text", ["a: [1, 2\n", "- just\n- a list\n", "plain text\n", ""])
def test_unreadable_yaml_is_reported_not_rewritten(text):
    with pytest.raises(store.StoreError) as exc:
        edit(text, [{"op": "tab.move", "key": "x", "index": 0}])
    assert exc.value.detail == "invalid_config"


def test_only_roundtrip_module_imports_ruamel():
    """ruamel.yaml은 studio 이미지에만 있다. dashkit/studio/roundtrip.py 밖에서는 import하지 않는다."""
    hits = []
    for path in (ROOT / "dashkit").rglob("*.py"):
        if "ruamel" in path.read_text(encoding="utf-8") and "import" in path.read_text(encoding="utf-8"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("import ruamel", "from ruamel")):
                    hits.append(str(path.relative_to(ROOT)))
    assert set(hits) == {"dashkit/studio/roundtrip.py"}
