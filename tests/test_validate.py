"""CSV 검증(dashkit/loader/validate.py). DB 없이 돈다."""
from __future__ import annotations

import pytest

from dashkit.config import load_project, table_order
from dashkit.loader.validate import validate_csv

from conftest import BROKEN_CSV_ERRORS, BROKEN_CSV_PROJECT, PROJECTS, broken_csv_copy, read_csv


@pytest.mark.parametrize("name", ["devtracker", "sales-sample"])
def test_example_csv_is_valid(name):
    project = load_project(name, PROJECTS)
    data, errors = validate_csv(project.schema, project.data_dir, table_order(project))
    assert errors == []
    for t, td in data.items():
        assert len(td.rows) == len(read_csv(project.data_dir / f"{t}.csv")[1])
        assert td.columns == list(project.schema["tables"][t]["columns"])


def test_broken_csv_reports_all_errors(tmp_path):
    project = load_project(BROKEN_CSV_PROJECT, broken_csv_copy(tmp_path))
    _data, errors = validate_csv(project.schema, project.data_dir, table_order(project))
    for prefix, value in BROKEN_CSV_ERRORS:
        assert any(e.startswith(prefix) and value in e for e in errors), (prefix, errors)
    assert len(errors) == len(BROKEN_CSV_ERRORS), errors


def test_missing_file_and_bad_header(tmp_path):
    root = broken_csv_copy(tmp_path)
    data = root / BROKEN_CSV_PROJECT / "data"
    (data / "docs.csv").unlink()
    text = (data / "members.csv").read_text(encoding="utf-8")
    header, rest = text.split("\n", 1)
    (data / "members.csv").write_text(header + ",zz_extra\n" + rest, encoding="utf-8")
    project = load_project(BROKEN_CSV_PROJECT, root)
    _data, errors = validate_csv(project.schema, project.data_dir, table_order(project))
    assert any(e.startswith("data/docs.csv:") for e in errors), errors
    assert any(e.startswith("data/members.csv:1:") for e in errors), errors
