"""Studio가 YAML을 다시 쓸 때 손으로 쓴 주석·키 순서·모양이 남는다(보강설계서 8절 5a 완료 조건). 스택 없이 돈다.

세 예시 프로젝트의 설정 파일은 **읽기만 한다**(글을 읽어 메모리에서 고친다). 저장소의 projects/는 바뀌지 않는다.
"""
from __future__ import annotations

import copy
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
    # gantt 섹션의 줄들. 예시 파일이 바뀌어도 맞게 줄 번호 대신 내용으로 찾는다
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == "- type: gantt")
    moved = lines[start:start + 10]
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


# ---------------------------------------------------------------- 5b: 배치·필터를 고쳐도 다른 줄은 그대로

def all_rows(layout: list) -> list[list]:
    """layout의 모든 행(최상위와 탭 속)."""
    out = []
    for entry in layout:
        out += [entry] if isinstance(entry, list) else [row for tab in entry["tabs"] for row in tab["rows"]]
    return out


def item_chart(item) -> str | None:
    return item if isinstance(item, str) else item.get("chart")


def dashboards_of(text: str) -> dict:
    return yaml.safe_load(text)["dashboards"]


def block_of(text: str, name: str) -> tuple[int, int]:
    """대시보드 name이 차지하는 줄 범위 [시작, 끝). 내용으로 찾는다(줄 번호에 기대지 않는다)."""
    lines = text.split("\n")
    start = lines.index(f"  {name}:", lines.index("dashboards:") if "dashboards:" in lines else
                        next(i for i, line in enumerate(lines) if line.startswith("dashboards:")))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith("   ")
                and not lines[i].startswith("  #")), len(lines))
    return start, end


def outside(text: str, name: str) -> str:
    start, end = block_of(text, name)
    lines = text.split("\n")
    return "\n".join(lines[:start] + lines[end:])


def set_layout(name: str, layout: list) -> list[dict]:
    return [{"op": "layout.set", "dashboard": name, "layout": layout}]


def movable(d: dict) -> tuple[int, int] | None:
    """차트가 둘 이상인 최상위 행과 다른 최상위 행(차트 행)의 자리."""
    rows = [i for i, e in enumerate(d["layout"]) if isinstance(e, list) and all(item_chart(x) for x in e)]
    wide = next((i for i in rows if len(d["layout"][i]) >= 2), None)
    other = next((i for i in rows if i != wide), None)
    return (wide, other) if wide is not None and other is not None else None


@pytest.mark.parametrize("name", EXAMPLES)
def test_moving_one_chart_changes_only_the_two_rows(name):
    text = DASHBOARDS[name]
    moved_any = False
    for dkey, d in dashboards_of(text).items():
        pair = movable(d)
        if pair is None:
            continue
        moved_any = True
        src, dst = pair
        layout = yaml.safe_load(yaml.safe_dump(d["layout"]))          # 깊은 복사
        item = layout[src].pop(0)
        chart = item_chart(item)
        layout[dst] = [x if isinstance(x, str) else {k: v for k, v in x.items() if k != "width"} for x in layout[dst]]
        layout[dst].append(chart)
        layout[src] = [x if isinstance(x, str) else {k: v for k, v in x.items() if k != "width"} for x in layout[src]]
        new, how = edit(text, set_layout(dkey, layout))
        assert how == "exact"
        assert dashboards_of(new)[dkey]["layout"] == layout
        removed, added = line_changes(text, new)
        # 두 행의 줄만 바뀐다. 차트 정의, 필터, 다른 행, 다른 대시보드는 한 글자도 바뀌지 않는다
        assert len(removed) == 2 and len(added) == 2, (removed, added)
        assert all(line.lstrip().startswith("- [") for line in removed + added)
        assert comments(new) == comments(text)
        assert outside(new, dkey) == outside(text, dkey)
        # 되돌리면 값이 같다(너비 표기는 요청이 준 대로라 글자는 다를 수 있다)
        back, how = edit(new, set_layout(dkey, d["layout"]))
        assert how == "exact" and back == text
    assert moved_any


@pytest.mark.parametrize("name", EXAMPLES)
def test_changing_one_width_changes_only_that_row(name):
    text = DASHBOARDS[name]
    for dkey, d in dashboards_of(text).items():
        rows = all_rows(d["layout"])
        target = next((r for r in rows if len(r) >= 2 and all(item_chart(x) for x in r)), None)
        if target is None:
            continue
        layout = yaml.safe_load(yaml.safe_dump(d["layout"]))
        row = next(r for r in all_rows(layout) if r == target)
        first = item_chart(row[0])
        row[0] = {"chart": first, "width": 3}
        row[1:] = [x if isinstance(x, str) else {k: v for k, v in x.items() if k != "width"} for x in row[1:]]
        new, how = edit(text, set_layout(dkey, layout))
        assert how == "exact" and dashboards_of(new)[dkey]["layout"] == layout
        removed, added = line_changes(text, new)
        assert len(removed) == 1 and len(added) == 1 and "width: 3" in added[0] and first in removed[0], (removed, added)
        # 같은 줄의 주석과 줄 머리는 남는다
        assert added[0][:len(added[0]) - len(added[0].lstrip())] == removed[0][:len(removed[0]) - len(removed[0].lstrip())]
        assert comments(new) == comments(text)
        assert outside(new, dkey) == outside(text, dkey)
        # 되돌리면 값이 같다. 지웠다 다시 넣은 키(width)는 항목의 맨 뒤에 붙으므로 그 줄의 키 순서만 다를 수 있다
        back, how = edit(new, set_layout(dkey, d["layout"]))
        assert how == "exact" and yaml.safe_load(back) == yaml.safe_load(text)
        removed, added = line_changes(text, back)
        assert len(removed) <= 1 and len(added) == len(removed)


@pytest.mark.parametrize("name", EXAMPLES)
def test_reordering_rows_moves_their_lines_untouched(name):
    text = DASHBOARDS[name]
    for dkey, d in dashboards_of(text).items():
        if len(d["layout"]) < 2:
            continue
        layout = list(reversed(d["layout"]))
        new, how = edit(text, set_layout(dkey, layout))
        assert how == "exact" and dashboards_of(new)[dkey]["layout"] == layout
        assert sorted(new.split("\n")) == sorted(text.split("\n"))          # 줄의 집합이 같다(자리만 바뀜)
        assert outside(new, dkey) == outside(text, dkey)
        back, how = edit(new, set_layout(dkey, d["layout"]))
        assert how == "exact" and back == text


@pytest.mark.parametrize("name", EXAMPLES)
def test_same_layout_writes_nothing(name):
    text = DASHBOARDS[name]
    op_list = [op for dkey, d in dashboards_of(text).items() for op in set_layout(dkey, d["layout"])]
    assert edit(text, op_list) == (text, "exact")


@pytest.mark.parametrize("name", EXAMPLES)
def test_chart_and_filter_edits_touch_only_their_lines(name):
    text = DASHBOARDS[name]
    boards = dashboards_of(text)
    dkey = next((k for k, d in boards.items() if d.get("filters")), next(iter(boards)))
    d = boards[dkey]
    chart = d["charts"][-1]
    # 차트 제목: 그 줄만
    new, how = edit(text, [{"op": "chart.update", "key": chart["key"], "values": {"title": "고친 차트 제목"}}])
    removed, added = line_changes(text, new)
    assert how == "exact" and len(removed) == 1 and len(added) == 1 and "고친 차트 제목" in added[0]
    assert comments(new) == comments(text)
    # 대시보드 제목: 그 줄만
    new, how = edit(text, [{"op": "dashboard.update", "dashboard": dkey, "values": {"title": "고친 대시보드"}}])
    removed, added = line_changes(text, new)
    assert how == "exact" and (removed, added) == ([f"    title: {d['title']}"], ["    title: 고친 대시보드"])
    if not d.get("filters"):
        return
    # 필터 라벨: 그 줄만. 필터 이동: 줄의 집합이 같다
    flt = d["filters"][0]
    new, how = edit(text, [{"op": "filter.update", "dashboard": dkey, "key": flt["key"], "values": {"label": "고친 라벨"}}])
    removed, added = line_changes(text, new)
    assert how == "exact" and len(removed) == 1 and len(added) == 1 and "고친 라벨" in added[0]
    if len(d["filters"]) > 1:
        new, how = edit(text, [{"op": "filter.move", "dashboard": dkey, "key": flt["key"], "index": len(d["filters"]) - 1}])
        assert how == "exact" and sorted(new.split("\n")) == sorted(text.split("\n")) and new != text
        assert outside(new, dkey) == outside(text, dkey)


@pytest.mark.parametrize("name", EXAMPLES)
def test_chart_add_then_remove_restores_the_file(name):
    text = DASHBOARDS[name]
    for dkey, d in dashboards_of(text).items():
        chart = {**d["charts"][0], "key": "zz_added", "title": "더한 차트"}
        new, how = edit(text, [{"op": "chart.add", "dashboard": dkey, "chart": chart}])
        assert how == "exact"
        removed, added = line_changes(text, new)
        assert removed == [] and added[-1] == "      - [zz_added]"
        assert outside(new, dkey) == outside(text, dkey) and comments(new) == comments(text)
        back, how = edit(new, [{"op": "chart.remove", "key": "zz_added"}])
        assert how == "exact" and back == text


def test_new_chart_and_filter_are_written_like_their_neighbours():
    """이웃이 모두 한 줄(`- { … }`)이면 새 항목도 한 줄로(가장 긴 이웃의 길이까지), 여러 줄이 섞여 있으면 여러 줄로 쓴다."""
    text = DASHBOARDS["chart-gallery"]
    boards = dashboards_of(text)
    lines = text.split("\n")

    def chart_lines(d: dict) -> list[str]:
        return [next(line for line in lines if line.lstrip().startswith(("- { key: " + c["key"] + ",", "- key: " + c["key"])))
                for c in d["charts"]]

    flow = next(k for k, d in boards.items() if all(line.lstrip().startswith("- {") for line in chart_lines(d)))
    mixed = next(k for k, d in boards.items() if not all(line.lstrip().startswith("- {") for line in chart_lines(d)))
    base = boards[flow]["charts"][0]
    new, how = edit(text, [{"op": "chart.add", "dashboard": flow, "chart": {**base, "key": "zz_flow", "title": "한 줄"}},
                           {"op": "chart.add", "dashboard": mixed, "chart": {**base, "key": "zz_block", "title": "여러 줄"}}])
    assert how == "exact"
    _removed, added = line_changes(text, new)
    one = next(line for line in added if "zz_flow," in line)
    assert one.startswith("      - { key: zz_flow, title: 한 줄, ") and one.endswith(" }") and len(one) > yamledit.INLINE_LIMIT
    assert "      - key: zz_block" in added and "        title: 여러 줄" in added
    # 필터도 같다
    with_filters = next(k for k, d in boards.items() if d.get("filters"))
    flt = {"key": "f_zz", "label": "더한 필터", "type": "select", "multi": True, "dataset": base["dataset"],
           "column": boards[with_filters]["filters"][1]["column"], "charts": [boards[with_filters]["charts"][0]["key"]]}
    new, how = edit(text, [{"op": "filter.add", "dashboard": with_filters, "filter": flt, "index": 1}])
    _removed, added = line_changes(text, new)
    assert how == "exact" and len(added) == 1 and added[0].startswith("      - { key: f_zz, label: 더한 필터, type: select,")


def gallery_blocks() -> tuple[str, str, list]:
    text = DASHBOARDS["chart-gallery"]
    dkey, d = next((k, d) for k, d in dashboards_of(text).items() if any(isinstance(e, dict) for e in d["layout"]))
    return text, dkey, d["layout"]


def find(layout: list, need) -> list:
    return next(row for row in all_rows(layout) if need(row))


def test_header_and_markdown_text_edits_keep_their_comments():
    text, dkey, old = gallery_blocks()
    layout = yaml.safe_load(yaml.safe_dump(old, allow_unicode=True))
    header = find(layout, lambda r: "header" in r[0] and r in layout)[0]
    was = header["header"]
    header["header"] = "고친 제목"
    note = next(x for row in all_rows(layout) for x in row if isinstance(x, dict) and "markdown" in x)
    note["markdown"] = "**고친 글**\n\n- 첫째: 값\n- 둘째\n"
    note["width"] = 5
    new, how = edit(text, set_layout(dkey, layout))
    assert how == "exact" and dashboards_of(new)[dkey]["layout"] == layout
    removed, added = line_changes(text, new)
    assert any(f"header: {was}" in line for line in removed) and any("header: 고친 제목" in line for line in added)
    assert comments(new) == comments(text)                      # 같은 줄의 주석이 모두 남는다
    # 여러 줄 글은 literal block으로 다시 쓴다(따옴표·\n 표기가 아니다)
    assert any(line.strip() == "**고친 글**" for line in added) and any(line.strip() == "- 첫째: 값" for line in added)
    assert not any("\\n" in line for line in added)
    assert outside(new, dkey) == outside(text, dkey)


def test_rows_move_in_and_out_of_dashboard_tabs():
    text, dkey, old = gallery_blocks()
    group_at = next(i for i, e in enumerate(old) if isinstance(e, dict))
    # 최상위의 차트 행을 첫 탭 안으로, 둘째 탭의 마지막 행을 최상위로
    layout = yaml.safe_load(yaml.safe_dump(old, allow_unicode=True))
    row_at = next(i for i, e in enumerate(layout) if isinstance(e, list) and all(isinstance(x, str) for x in e))
    moved_in = layout[row_at]
    moved_out = layout[group_at]["tabs"][-1]["rows"].pop()
    layout[group_at]["tabs"][0]["rows"].append(moved_in)
    layout = [e for e in layout if e is not moved_in] + [moved_out]
    new, how = edit(text, set_layout(dkey, layout))
    assert how == "exact" and dashboards_of(new)[dkey]["layout"] == layout
    assert comments(new) == comments(text)
    assert outside(new, dkey) == outside(text, dkey)
    back, how = edit(new, set_layout(dkey, old))
    assert how == "exact" and yaml.safe_load(back) == yaml.safe_load(text)
    assert comments(back) == comments(text)


def test_block_item_moved_into_one_line_row_opens_only_that_row():
    """여러 줄로 쓴 항목(글)을 한 줄 행·한 줄 탭 안으로 옮기면 그 행(탭)만 여러 줄로 풀린다. 파일 전체를 다시 쓰지 않는다."""
    text, dkey, old = gallery_blocks()
    for into_tab in (False, True):
        layout = yaml.safe_load(yaml.safe_dump(old, allow_unicode=True))
        source = find(layout, lambda r: any(isinstance(x, dict) and "markdown" in x for x in r))
        note = next(x for x in source if isinstance(x, dict) and "markdown" in x)
        source.remove(note)
        if into_tab:
            target = next(e for e in layout if isinstance(e, dict))["tabs"][0]["rows"][0]
        else:
            target = next(e for e in layout if isinstance(e, list) and e and all(isinstance(x, str) for x in e))
        target[:] = [{"chart": item_chart(x), "width": 2} for x in target] + [note]
        new, how = edit(text, set_layout(dkey, layout))
        assert how == "exact" and dashboards_of(new)[dkey]["layout"] == layout
        assert comments(new) == comments(text)
        # 글의 줄들은 들여쓰기만 바뀐다
        body = [line.strip() for line in note["markdown"].split("\n") if line.strip()]
        assert all(any(line.strip() == b for line in new.split("\n")) for b in body)
        assert outside(new, dkey) == outside(text, dkey)


def test_layout_set_through_the_ruamel_path_gives_the_same_value():
    """예비 경로(ruamel)에서도 layout.set의 결과 값은 같다(행을 다시 쓰는 판단이 문서 유형에 기대지 않는다)."""
    text, dkey, old = gallery_blocks()
    layout = yaml.safe_load(yaml.safe_dump(old, allow_unicode=True))
    layout.reverse()
    find(layout, lambda r: len(r) >= 2 and all(item_chart(x) for x in r))[0] = \
        {"width": 3, "chart": item_chart(find(layout, lambda r: len(r) >= 2 and all(item_chart(x) for x in r))[0])}
    op_list = set_layout(dkey, layout) + [{"op": "chart.remove", "key": dashboards_of(text)[dkey]["charts"][0]["key"]}]
    expected = yaml.safe_load(text)
    ops.apply_ops(expected, op_list)
    new = roundtrip.edit(text, lambda doc: ops.apply_ops(doc, op_list))
    assert yamledit.strict_equal(store._dates_to_str(yaml.safe_load(new)), store._dates_to_str(expected))
    assert len(comments(new)) == len(comments(text))
    exact, how = edit(text, op_list)
    assert how == "exact" and yaml.safe_load(exact) == yaml.safe_load(new)


def test_5b_ops_on_a_file_the_exact_writer_cannot_handle():
    text = ANCHORED.replace("tabs:\n", """dashboards:
  main:
    title: 본판          # 대시보드 주석
    charts:
      - { key: one, title: 하나, viz: table, dataset: rows, columns: [a] }
      - { key: two, title: 둘, viz: table, dataset: rows, columns: [a] }
    layout:
      - [one]            # 첫 행
      - [two]
tabs:
""", 1)
    new, how = edit(text, [
        {"op": "layout.set", "dashboard": "main", "layout": [[{"chart": "two", "width": 4}, {"chart": "one", "width": 8}]]},
        {"op": "chart.add", "dashboard": "main", "chart": {"key": "three", "title": "yes", "viz": "table", "dataset": "rows",
                                                           "columns": ["a"]}},
        {"op": "filter.add", "dashboard": "main", "filter": {"key": "f", "label": "기간", "type": "time_range"}}])
    assert how == "roundtrip"
    main = yaml.safe_load(new)["dashboards"]["main"]
    assert main["layout"] == [[{"chart": "two", "width": 4}, {"chart": "one", "width": 8}], ["three"]]
    assert main["charts"][2]["title"] == "yes" and main["filters"] == [{"key": "f", "label": "기간", "type": "time_range"}]
    assert "# 대시보드 주석" in new and "# 앵커가 있는 파일" in new and "&sec" in new


def _strip_widths(row: list) -> list:
    return [x if isinstance(x, str) or "chart" not in x else x["chart"] for x in row]


def _random_layout(rng: random.Random, layout: list) -> list:
    """차트 하나를 다른 행·새 행으로 옮기거나, 행 순서를 바꾸거나, 너비를 고친 layout(설정 검사를 통과하는 것만)."""
    layout = yaml.safe_load(yaml.safe_dump(layout, allow_unicode=True))
    rows = [r for r in all_rows(layout) if all(item_chart(x) for x in r)]          # 제목·글이 없는 행
    choice = rng.randrange(4)
    if choice == 0 and len(layout) > 1:
        layout.insert(rng.randrange(len(layout)), layout.pop(rng.randrange(len(layout))))
    elif choice == 1:
        row = rng.choice(rows)
        at = rng.randrange(len(row))
        chart = item_chart(row[at])
        row[:] = _strip_widths(row)
        row[at] = {"chart": chart, "width": rng.randint(1, 12 - (len(row) - 1))}
        if rng.random() < 0.3:
            row[at]["height"] = rng.choice([30, 60, 90])
    else:
        source = rng.choice(rows)
        item = source.pop(rng.randrange(len(source)))
        source[:] = _strip_widths(source)
        targets = [r for r in rows if r is not source and r and len(r) < 6]
        if choice == 2 and targets:
            target = rng.choice(targets)
            target[:] = _strip_widths(target)
            target.insert(rng.randrange(len(target) + 1), item_chart(item))
        else:
            layout.insert(rng.randrange(len(layout) + 1), [item])
    # 비게 된 행·탭·묶음을 지운다(화면이 하는 일)
    out = []
    for entry in layout:
        if isinstance(entry, list):
            if entry:
                out.append(entry)
            continue
        tabs = [{**t, "rows": [r for r in t["rows"] if r]} for t in entry["tabs"]]
        tabs = [t for t in tabs if t["rows"]]
        if tabs:
            out.append({"tabs": tabs})
    return out


def _random_5b_ops(rng: random.Random, doc: dict, step: int) -> list[dict]:
    boards = doc["dashboards"]
    dkey = rng.choice(list(boards))
    d = boards[dkey]
    filters = d.get("filters") or []
    choice = rng.randrange(12)
    if choice <= 3:
        return set_layout(dkey, _random_layout(rng, d["layout"]))
    if choice == 4:
        base = rng.choice(d["charts"])
        op = {"op": "chart.add", "dashboard": dkey, "chart": {**base, "key": f"zz_{step}", "title": f"차트 {step}: 새"}}
        if rng.random() < 0.5:
            op["width"] = rng.randint(1, 12)
        return [op]
    if choice == 5 and len(d["charts"]) > 1:
        return [{"op": "chart.remove", "key": rng.choice(d["charts"])["key"]}]
    if choice == 6:
        return [{"op": "chart.update", "key": rng.choice(d["charts"])["key"],
                 "values": {"title": f"제목 {step}", "description": rng.choice([None, "설명: 예", "yes"])}}]
    if choice == 7:
        flt = {"key": f"f_zz_{step}", "label": f"필터 {step}", "type": "time_range", "default": rng.choice([None, "Last year"])}
        if rng.random() < 0.5:
            flt["charts"] = [rng.choice(d["charts"])["key"]]
        return [{"op": "filter.add", "dashboard": dkey, "filter": flt, "index": rng.randrange(len(filters) + 1)}]
    if choice == 8 and filters:
        return [{"op": "filter.move", "dashboard": dkey, "key": rng.choice(filters)["key"], "index": rng.randrange(len(filters))}]
    if choice == 9 and filters:
        return [{"op": "filter.update", "dashboard": dkey, "key": rng.choice(filters)["key"], "values": {"label": f"라벨 {step}"}}]
    if choice == 10 and filters:
        parents = {f.get("parent") for f in filters}
        free = [f for f in filters if f["key"] not in parents]
        if free:
            return [{"op": "filter.remove", "dashboard": dkey, "key": rng.choice(free)["key"]}]
    if choice == 11:
        return [{"op": "tab.move", "key": rng.choice(doc["tabs"])["key"], "index": rng.randrange(len(doc["tabs"]))},
                {"op": "app.update", "values": {"title": f"앱 {step}"}}]
    return [{"op": "dashboard.update", "dashboard": dkey,
             "values": {"title": f"대시보드 {step}", "cross_filters": rng.choice([None, True, False])}}]


@pytest.mark.parametrize("name", EXAMPLES)
@pytest.mark.parametrize("seed", range(2))
def test_random_5b_ops_stay_exact_and_valid(name, seed, tmp_path, monkeypatch):
    """배치·필터 연산(과 5a 연산)을 무작위로 이어 보내도 매번 저장되고(설정 검사 통과), "고친 곳만 다시 쓰기"로 처리된다."""
    from studio_client import fresh_client

    from dashkit import config

    client = fresh_client(tmp_path, monkeypatch, projects=[name])
    rng = random.Random(f"5b-{name}-{seed}")
    path = client.projects / name / "dashboard.yaml"
    original = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(original)
    for step in range(24):
        op_list = _random_5b_ops(rng, doc, step)
        expected = yaml.safe_load(path.read_text(encoding="utf-8"))
        ops.apply_ops(expected, op_list)
        r = client.patch(f"/studio/api/projects/{name}/config", json={"ops": op_list})
        assert r.status == 200, (step, op_list, r.text)
        assert r.json["written"] == {"dashboard.yaml": "exact"}, (step, op_list)
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc == expected, (step, op_list)
        assert r.json["dashboard"] == store._dates_to_str(doc)
    config.load_project(name, client.projects)                       # make check와 같은 검사
    # 데이터셋 정의(SQL, 라벨, 지표)는 한 글자도 바뀌지 않는다
    final = path.read_text(encoding="utf-8")
    cut = lambda text: text[text.index("\ndatasets:"):text.index("\ndashboards:")]      # noqa: E731
    assert cut(final) == cut(original)


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
    """ruamel.yaml은 studio 이미지에만 있다. dashkit/project/roundtrip.py(공용 코어로 옮김) 밖에서는 import하지 않는다."""
    hits = []
    for path in (ROOT / "dashkit").rglob("*.py"):
        if "ruamel" in path.read_text(encoding="utf-8") and "import" in path.read_text(encoding="utf-8"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("import ruamel", "from ruamel")):
                    hits.append(str(path.relative_to(ROOT)))
    assert set(hits) == {"dashkit/project/roundtrip.py"}


def test_layout_set_keeps_identical_blocks_in_other_rows_apart():
    """같은 내용의 글 블록이 여러 행에 있을 때 한 곳만 고치면 그곳만 바뀐다(리뷰 지적 회귀).

    예전에는 같은 내용의 항목을 한 객체로 묶어, 다른 행의 객체를 가져와 고치는 바람에 두 행이 함께 바뀌었다.
    """
    doc = {"dashboards": {"d": {"title": "t", "charts": [], "layout": [
        [{"markdown": "same", "width": 6}],
        [{"markdown": "same", "width": 12}],
    ]}}}
    wanted = [[{"markdown": "same", "width": 6}], [{"markdown": "same", "width": 10}]]
    ops.apply_ops(doc, [{"op": "layout.set", "dashboard": "d", "layout": wanted}])
    layout = doc["dashboards"]["d"]["layout"]
    assert layout == wanted
    assert layout[0][0] is not layout[1][0], "두 행의 항목이 같은 객체를 가리키면 안 됨"

    # 세 곳에 같은 블록이 있고 가운데만 고칠 때, 행을 서로 바꾸면서 하나만 고칠 때도 요청한 값 그대로다
    doc["dashboards"]["d"]["layout"] = [[{"markdown": "same", "width": 4}], [{"markdown": "same", "width": 4}],
                                        [{"markdown": "same", "width": 4}]]
    for wanted in (
        [[{"markdown": "same", "width": 4}], [{"markdown": "same", "width": 9}], [{"markdown": "same", "width": 4}]],
        [[{"markdown": "same", "width": 2}], [{"markdown": "same", "width": 4}], [{"markdown": "same", "width": 9}]],
        [[{"header": "h"}], [{"markdown": "same", "width": 4}, {"markdown": "same", "width": 3}]],
    ):
        ops.apply_ops(doc, [{"op": "layout.set", "dashboard": "d", "layout": copy.deepcopy(wanted)}])
        layout = doc["dashboards"]["d"]["layout"]
        assert layout == wanted
        items = [item for row in layout for item in row]
        assert len({id(item) for item in items}) == len(items)


def test_layout_set_with_identical_blocks_changes_only_that_line_in_the_file():
    text = (
        "dashboards:\n"
        "  d:\n"
        "    title: t\n"
        "    charts: []\n"
        "    layout:\n"
        "      - [{ markdown: same, width: 6 }]    # 첫째\n"
        "      - [{ markdown: same, width: 12 }]   # 둘째\n"
    )
    new, how = edit(text, [{"op": "layout.set", "dashboard": "d",
                            "layout": [[{"markdown": "same", "width": 6}], [{"markdown": "same", "width": 10}]]}])
    assert yaml.safe_load(new)["dashboards"]["d"]["layout"] == [[{"markdown": "same", "width": 6}], [{"markdown": "same", "width": 10}]]
    assert "      - [{ markdown: same, width: 6 }]    # 첫째\n" in new, "건드리지 않은 행은 글자 그대로여야 함"
    assert "width: 10" in new and "# 둘째" in new and how == "exact"
