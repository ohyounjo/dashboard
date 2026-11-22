"""이슈 #42 단위 테스트: xlsx 파서, CursorParser/ingest-db 보안, 감시 폴더 필터/마스킹, 이어 올리기 저장소, 정규형 동등성."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashkit import config, scaffold
from dashkit.ingest import batches, newtable
from dashkit.ingest.add import Item, make_parser
from dashkit.ingest.db_import import CursorParser, build_source_query, sanitize_db_error, validate_dsn_env
from dashkit.ingest.intake import PARSERS, CsvParser, Intake, IntakeError, RowError, RowsParser, redact_sensitive_errors
from dashkit.ingest.operation import IngestError
from dashkit.ingest.xlsx import XlsxLimits, XlsxParser
from dashkit.manage.uploads import Uploads
from dashkit.project import upload

FIXTURES = Path(__file__).parent / "fixtures"


def _col_letter(idx: int) -> str:
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def make_xlsx_bytes(
    sheets: list[dict],
    *,
    shared_strings: list[str] | None = None,
    shared_strings_xml: str | None = None,
    num_fmts: dict[int, str] | None = None,
    cell_xfs: list[int] | None = None,
    date1904: bool = False,
    extra_entries: dict[str, bytes] | None = None,
    prolog_inject: tuple[str, str] | None = None,
) -> bytes:
    """테스트용 최소 OOXML(.xlsx) 생성기."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )

        wb_sheets = []
        wb_rels = []
        for idx, sh in enumerate(sheets, 1):
            rid = f"rId{idx}"
            state_attr = f' state="{sh["state"]}"' if sh.get("state") else ""
            wb_sheets.append(f'<sheet name="{sh["name"]}" sheetId="{idx}"{state_attr} r:id="{rid}"/>')
            wb_rels.append(
                f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            )

        wb_pr = '<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>"
        wb_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"{wb_pr}<sheets>{''.join(wb_sheets)}</sheets></workbook>"
        )
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(wb_rels)}</Relationships>",
        )

        if shared_strings_xml is not None:
            zf.writestr("xl/sharedStrings.xml", shared_strings_xml)
        elif shared_strings is not None:
            sis = "".join(f"<si><t>{_xml_esc(s)}</t></si>" for s in shared_strings)
            zf.writestr(
                "xl/sharedStrings.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}">{sis}</sst>',
            )

        if num_fmts is not None or cell_xfs is not None:
            fmts_xml = ""
            if num_fmts:
                items = "".join(
                    f'<numFmt numFmtId="{fid}" formatCode="{_xml_esc(code)}"/>' for fid, code in num_fmts.items()
                )
                fmts_xml = f'<numFmts count="{len(num_fmts)}">{items}</numFmts>'
            xfs = cell_xfs or [0]
            xf_items = "".join(f'<xf numFmtId="{fid}" applyNumberFormat="1"/>' for fid in xfs)
            styles_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'{fmts_xml}<cellXfs count="{len(xfs)}">{xf_items}</cellXfs></styleSheet>'
            )
            zf.writestr("xl/styles.xml", styles_xml)

        for idx, sh in enumerate(sheets, 1):
            if "raw_xml" in sh:
                sheet_xml = sh["raw_xml"]
            else:
                row_xmls = []
                for r_idx, row in enumerate(sh.get("rows", []), 1):
                    if isinstance(row, tuple):
                        r_num, cells = row
                    else:
                        r_num, cells = r_idx, row
                    c_xmls = []
                    for c_pos, cell in enumerate(cells):
                        if cell is None:
                            continue
                        if isinstance(cell, dict):
                            ref = cell.get("r", f"{_col_letter(c_pos)}{r_num}")
                            ref_attr = f' r="{ref}"' if ref else ""
                            t_attr = f' t="{cell["t"]}"' if cell.get("t") else ""
                            s_attr = f' s="{cell["s"]}"' if cell.get("s") is not None else ""
                            if cell.get("t") == "inlineStr":
                                inner = f"<is><t>{_xml_esc(str(cell.get('v', '')))}</t></is>"
                            elif "v" in cell and cell["v"] is not None:
                                inner = f"<v>{_xml_esc(str(cell['v']))}</v>"
                            else:
                                inner = ""
                            c_xmls.append(f"<c{ref_attr}{t_attr}{s_attr}>{inner}</c>")
                        else:
                            ref_attr = f' r="{_col_letter(c_pos)}{r_num}"'
                            c_xmls.append(f'<c{ref_attr} t="inlineStr"><is><t>{_xml_esc(str(cell))}</t></is></c>')
                    row_xmls.append(f'<row r="{r_num}">{"".join(c_xmls)}</row>')
                sheet_xml = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f"<sheetData>{''.join(row_xmls)}</sheetData></worksheet>"
                )
            if prolog_inject and prolog_inject[0] == f"xl/worksheets/sheet{idx}.xml":
                sheet_xml = sheet_xml.replace("<worksheet", f"{prolog_inject[1]}<worksheet", 1)
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml)

        for k, v in (extra_entries or {}).items():
            zf.writestr(k, v)
    return buf.getvalue()


def _xml_esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------- 1. 동일 정규형 보장 (csv vs rows vs xlsx vs cursor)

def test_equivalence_across_csv_rows_xlsx_and_cursor(tmp_path):
    """동일 데이터(문자열, 쉼표 숫자, 날짜, 마이크로초 시각, 참거짓, NULL, 빠진 열)를
    csv, rows, xlsx, cursor로 접수하면 정규형 .csv.gz 바이트와 sha256이 100% 일치한다.
    """
    table_spec = {
        "mode": "append",
        "primary_key": ["id"],
        "columns": {
            "id": {"type": "text", "required": True},
            "label": {"type": "text"},
            "amount": {"type": "number"},
            "qty": {"type": "integer"},
            "on_day": {"type": "date"},
            "created_at": {"type": "timestamp"},
            "flag": {"type": "boolean"},
            "optional_note": {"type": "text"},
        },
    }

    # 1) CSV (쉼표 숫자, YYYY/MM/DD, 마이크로초 시각, Y/N, NULL)
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "id,label,amount,qty,on_day,created_at,flag\n"
        'R1,"hello, world","1,234.50",10,2026/11/20,2026-11-20T10:30:45.123000,Y\n'
        "R2,,0,20,2026-11-21,2026-11-21T08:00:00,false\n",
        encoding="utf-8",
    )

    # 2) JSON rows
    json_rows = [
        {
            "id": "R1",
            "label": "hello, world",
            "amount": "1,234.50",
            "qty": 10,
            "on_day": "2026/11/20",
            "created_at": "2026-11-20T10:30:45.123000",
            "flag": True,
        },
        {
            "id": "R2",
            "label": None,
            "amount": "0",
            "qty": 20,
            "on_day": "2026-11-21",
            "created_at": "2026-11-21T08:00:00",
            "flag": False,
        },
    ]

    # 3) XLSX (공유 문자열, 숫자 서식 + 날짜/마이크로초 시각 일련번호, 부울, 빈 셀)
    # 2026-11-20 = 46346; 2026-11-20T10:30:45.123000 = 46346 + (10*3600 + 30*60 + 45.123)/86400
    ts_frac = (Decimal("10") * 3600 + Decimal("30") * 60 + Decimal("45.123")) / Decimal("86400")
    ts1 = str(Decimal("46346") + ts_frac)
    ts2 = str(Decimal("46347") + Decimal("8") * 3600 / Decimal("86400"))
    xlsx_path = tmp_path / "data.xlsx"
    xlsx_path.write_bytes(
        make_xlsx_bytes(
            [
                {
                    "name": "Sheet1",
                    "rows": [
                        ["id", "label", "amount", "qty", "on_day", "created_at", "flag"],
                        [
                            {"t": "s", "v": 0},
                            {"t": "s", "v": 1},
                            {"t": "s", "v": 2},
                            {"t": "n", "v": "10"},
                            {"t": "n", "s": 1, "v": "46346"},
                            {"t": "n", "s": 2, "v": ts1},
                            {"t": "b", "v": "1"},
                        ],
                        [
                            {"t": "s", "v": 3},
                            None,
                            {"t": "n", "v": "0"},
                            {"t": "n", "v": "20"},
                            {"t": "n", "s": 1, "v": "46347"},
                            {"t": "n", "s": 2, "v": ts2},
                            {"t": "b", "v": "0"},
                        ],
                    ],
                }
            ],
            shared_strings=["R1", "hello, world", "1,234.50", "R2"],
            num_fmts={164: "yyyy-mm-dd", 165: "yyyy-mm-dd hh:mm:ss.000"},
            cell_xfs=[0, 164, 165],
        )
    )

    # 4) Cursor
    class FakeCursor:
        description = [(c,) for c in ("id", "label", "amount", "qty", "on_day", "created_at", "flag")]

        def __init__(self):
            self._batches = [
                [
                    ("R1", "hello, world", Decimal("1234.50"), 10, date(2026, 11, 20), datetime(2026, 11, 20, 10, 30, 45, 123000), True),
                    ("R2", None, Decimal("0"), 20, date(2026, 11, 21), datetime(2026, 11, 21, 8, 0, 0), False),
                ],
                [],
            ]

        def fetchmany(self, _n):
            return self._batches.pop(0) if self._batches else []

    parsers = [
        ("csv", CsvParser(csv_path, "data.csv")),
        ("rows", RowsParser(json_rows)),
        ("xlsx", XlsxParser(xlsx_path, "data.xlsx")),
        ("cursor", CursorParser(FakeCursor(), "db:src")),
    ]
    hashes: dict[str, str] = {}
    gz_bytes: dict[str, bytes] = {}
    for label, parser in parsers:
        intake = Intake(parser, "t", table_spec)
        out_path = tmp_path / f"out-{label}.csv.gz"
        writer = batches.CanonicalWriter(out_path)
        writer.header(intake.header)
        for _at, values, typed in intake.rows():
            assert typed is not None, f"{label}: {intake.report.errors}"
            writer.row(values)
        hashes[label] = writer.close()
        gz_bytes[label] = out_path.read_bytes()
        parser.close()

    assert len(set(hashes.values())) == 1, hashes
    assert len(set(gz_bytes.values())) == 1


# ---------------------------------------------------------------- 2. xlsx 파서 상세·보안 테스트

def test_xlsx_rich_text_ruby_sparse_1900_1904_and_sheet_selection(tmp_path):
    assert "xlsx" in PARSERS
    # 공유 문자열: 서식 조각(<r><t>) 결합 + 루비 주석(<rPh>) 제외
    ss_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><r><t>Hello </t></r><r><t>World</t></r><rPh><t>루비</t></rPh></si>"
        "<si><t>A</t></si>"
        "</sst>"
    )
    # 1900 시트: 첫 시트는 hidden -> 기본 선택 시 두 번째 가시 시트가 열려야 함
    # 앞 빈 행(1행) 건너뛰고 2행이 머리글, 희소 셀(r="A2", r="C2", r="E2"), 1900 윤년 버그 serial 60 & 61
    xlsx_path = tmp_path / "multi.xlsx"
    xlsx_path.write_bytes(
        make_xlsx_bytes(
            [
                {"name": "HiddenSheet", "state": "hidden", "rows": [["x"]]},
                {
                    "name": "DataSheet",
                    "rows": [
                        (1, []),
                        (2, [{"r": "A2", "t": "inlineStr", "v": "id"}, {"r": "C2", "t": "inlineStr", "v": "d"}, {"r": "E2", "t": "inlineStr", "v": "note"}, {"r": "F2", "t": "inlineStr", "v": ""}]),
                        (4, [{"r": "A4", "t": "s", "v": "1"}, {"r": "B4", "t": "n", "v": ""}, {"r": "C4", "t": "n", "s": 1, "v": "60"}, {"r": "E4", "t": "s", "v": "0"}]),
                        (5, [{"r": "A5", "t": "str", "v": "B"}, {"r": "C5", "t": "d", "v": "1900-03-01"}, {"r": "E5", "t": "e", "v": "#DIV/0!"}]),
                        (6, [{"r": "A6", "t": "inlineStr", "v": "C"}, {"r": "C6", "t": "n", "s": 1, "v": "61"}, {"r": "G6", "t": "inlineStr", "v": "extra"}]),
                    ],
                },
            ],
            shared_strings_xml=ss_xml,
            num_fmts={164: "[Red]yyyy-mm-dd;@"},
            cell_xfs=[0, 164],
        )
    )
    p = XlsxParser(xlsx_path, "multi.xlsx")
    assert p.header() == ["id", "", "d", "", "note"]
    assert p.header_row == 2
    rows = list(p.rows())
    p.close()
    assert rows[0] == (4, ["A", "", "1900-02-29", "", "Hello World"])
    assert rows[1] == (5, ["B", "", "1900-03-01", "", "#DIV/0!"])
    assert rows[2][0] == 6 and isinstance(rows[2][1], RowError)
    assert rows[2][1].where == "multi.xlsx:6:-"

    # 시트 이름 및 번호 선택 + 없는 시트 오류
    p_hidden = XlsxParser(xlsx_path, "multi.xlsx", sheet=0)
    assert p_hidden.header() == ["x"]
    p_hidden.close()
    p_name = XlsxParser(xlsx_path, "multi.xlsx", sheet="DataSheet")
    assert p_name.header() == ["id", "", "d", "", "note"]
    p_name.close()
    with pytest.raises(IntakeError) as exc:
        XlsxParser(xlsx_path, "multi.xlsx", sheet="NoSuch").header()
    assert exc.value.detail == "invalid_csv"

    # 1904 날짜 체계 확인 (serial 1 == 1904-01-02)
    p1904 = tmp_path / "d1904.xlsx"
    p1904.write_bytes(
        make_xlsx_bytes(
            [{"name": "S1", "rows": [["dt"], [{"t": "n", "s": 1, "v": "1"}]]}],
            num_fmts={164: "yyyy-mm-dd"},
            cell_xfs=[0, 164],
            date1904=True,
        )
    )
    parser_1904 = XlsxParser(p1904, "d1904.xlsx")
    assert list(parser_1904.rows()) == [(2, ["1904-01-02"])]
    parser_1904.close()


def test_xlsx_security_defenses_and_filename_checks(tmp_path):
    # 1) 비-ZIP
    bad_zip = tmp_path / "notzip.xlsx"
    bad_zip.write_bytes(b"id,name\n1,a\n")
    with pytest.raises(IntakeError) as exc:
        XlsxParser(bad_zip, "notzip.xlsx").header()
    assert exc.value.detail == "invalid_xlsx"

    # 2) OLE2 (.xls / 암호화 문서)
    ole = tmp_path / "legacy.xlsx"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(IntakeError) as exc:
        XlsxParser(ole, "legacy.xlsx").header()
    assert exc.value.detail == "invalid_xlsx"

    # 3) .xlsm / VBA 매크로
    xlsm = tmp_path / "macro.xlsx"
    xlsm.write_bytes(make_xlsx_bytes([{"name": "S1", "rows": [["id"]]}], extra_entries={"xl/vbaProject.bin": b"vba"}))
    with pytest.raises(IntakeError) as exc:
        XlsxParser(xlsm, "macro.xlsx").header()
    assert exc.value.detail == "invalid_xlsx"

    # 4) <!DOCTYPE / <!ENTITY 차단
    xxe = tmp_path / "xxe.xlsx"
    xxe.write_bytes(
        make_xlsx_bytes(
            [{"name": "S1", "rows": [["id"], ["1"]]}],
            prolog_inject=("xl/worksheets/sheet1.xml", '<!DOCTYPE foo [ <!ENTITY lol "lol"> ]>'),
        )
    )
    with pytest.raises(IntakeError) as exc:
        XlsxParser(xxe, "xxe.xlsx").header()
    assert exc.value.detail == "invalid_csv"

    # 5) 비압축 크기 한도 초과 (ZIP 폭탄 모사)
    bomb = tmp_path / "bomb.xlsx"
    big_text = "x" * 60_000
    bomb.write_bytes(make_xlsx_bytes([{"name": "S1", "rows": [["id"], [big_text], [big_text]]}]))
    with pytest.raises(IntakeError) as exc:
        XlsxParser(bomb, "bomb.xlsx", limits=XlsxLimits(max_uncompressed_bytes=10_000)).header()
    assert exc.value.detail == "invalid_csv"

    # 5-b) 희소 셀 열 참조 상한 초과(r="ZZZZZZ1" 등) → 거대한 배열 할당 없이 즉시 거절
    huge_header = tmp_path / "huge_col_header.xlsx"
    with zipfile.ZipFile(huge_header, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    '</Types>')
        zf.writestr("xl/workbook.xml",
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                    '</Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                    '<row r="1"><c r="ZZZZZZ1" t="inlineStr"><is><t>x</t></is></c></row>'
                    '</sheetData></worksheet>')
    with pytest.raises(IntakeError) as col_exc:
        XlsxParser(huge_header, "huge_col_header.xlsx").header()
    assert col_exc.value.detail == "invalid_csv"

    huge_row = tmp_path / "huge_col_row.xlsx"
    with zipfile.ZipFile(huge_row, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(huge_header, "r") as src_zf:
            for item in src_zf.infolist():
                if item.filename != "xl/worksheets/sheet1.xml":
                    zf.writestr(item.filename, src_zf.read(item.filename))
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                    '<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c></row>'
                    '<row r="2"><c r="ZZZZZZ2" t="inlineStr"><is><t>bad</t></is></c></row>'
                    '</sheetData></worksheet>')
    rp = XlsxParser(huge_row, "huge_col_row.xlsx")
    assert rp.header() == ["id"]
    bad_rows = list(rp.rows())
    rp.close()
    assert len(bad_rows) == 1 and isinstance(bad_rows[0][1], RowError) and bad_rows[0][1].fatal is True

    # 5-c) 지수 표기 숫자 확장 길이 상한 초과(1e200000000 등) → MemoryError 없이 즉시 거절
    from dashkit.ingest.xlsx import _format_number_cell
    from dashkit.ingest.intake import CsvParser
    assert _format_number_cell("0e200000000") == "0"
    for huge_num in ("1e200000000", "1e-200000000", "-1.5e200000000"):
        with pytest.raises(IntakeError) as num_exc:
            _format_number_cell(huge_num)
        assert num_exc.value.detail == "invalid_csv"

    huge_exp_xlsx = tmp_path / "huge_exp.xlsx"
    huge_exp_xlsx.write_bytes(make_xlsx_bytes([{"name": "S1", "rows": [["id", "n"], ["E1", {"t": "n", "v": "1e200000000"}]]}]))
    ep = XlsxParser(huge_exp_xlsx, "huge_exp.xlsx")
    assert ep.header() == ["id", "n"]
    exp_rows = list(ep.rows())
    ep.close()
    assert len(exp_rows) == 1 and isinstance(exp_rows[0][1], RowError) and exp_rows[0][1].fatal is True

    # 5-d) 손상된/잘린 .csv.gz → IntakeError('invalid_csv') 정규화
    bad_gz = tmp_path / "corrupt.csv.gz"
    bad_gz.write_bytes(b"not-a-gzip")
    with pytest.raises(IntakeError) as gz_exc:
        CsvParser(bad_gz, "corrupt.csv.gz").header()
    assert gz_exc.value.detail == "invalid_csv"
    valid_gz = gzip.compress(b"id,name\n1,a\n")
    trunc_gz = tmp_path / "trunc.csv.gz"
    trunc_gz.write_bytes(valid_gz[:-4])
    with pytest.raises(IntakeError) as gz_exc2:
        CsvParser(trunc_gz, "trunc.csv.gz").header()
    assert gz_exc2.value.detail == "invalid_csv"

    # 5-e) CRC/압축 손상 xlsx 시트 엔트리 → BadZipFile 대신 IntakeError('invalid_xlsx')
    good_bytes = bytearray(make_xlsx_bytes([{"name": "S1", "rows": [["id", "v"], ["1", "hello world"]]}]))
    with zipfile.ZipFile(io.BytesIO(good_bytes), "r") as zf_ok:
        sh_info = zf_ok.getinfo("xl/worksheets/sheet1.xml")
        corrupt_pos = sh_info.header_offset + 30 + len(sh_info.filename.encode("utf-8")) + max(sh_info.compress_size - 2, 0)
    good_bytes[corrupt_pos] ^= 0xFF
    bad_crc_xlsx = tmp_path / "bad_crc.xlsx"
    bad_crc_xlsx.write_bytes(bytes(good_bytes))
    with pytest.raises(IntakeError) as crc_exc:
        XlsxParser(bad_crc_xlsx, "bad_crc.xlsx").header()
    assert crc_exc.value.detail == "invalid_xlsx"

    # 5-f) 날짜 일련번호 범위 초과(1900/1904 + 반올림 넘침 포함) → OverflowError 대신 IntakeError / fatal RowError
    from dashkit.ingest.xlsx import _excel_serial_to_iso
    assert _excel_serial_to_iso("2958465", has_time=False, date1904=False) == "9999-12-31"
    assert _excel_serial_to_iso("2957003", has_time=False, date1904=True) == "9999-12-31"
    assert _excel_serial_to_iso("2958465.99999999", has_time=True, date1904=False) == "9999-12-31T23:59:59.999136"
    for bad_serial, d1904 in (("2958466", False), ("2958465.999999999999", False), ("2957004", True), ("2957003.999999999999", True)):
        with pytest.raises(IntakeError) as dt_exc:
            _excel_serial_to_iso(bad_serial, has_time=True, date1904=d1904)
        assert dt_exc.value.detail == "invalid_csv"
    bad_dt_xlsx = tmp_path / "bad_date.xlsx"
    bad_dt_xlsx.write_bytes(
        make_xlsx_bytes(
            [{"name": "S1", "rows": [["id", "d"], ["1", {"t": "n", "s": 1, "v": "2958466"}]]}],
            num_fmts={164: "yyyy-mm-dd"},
            cell_xfs=[0, 164],
        )
    )
    dp = XlsxParser(bad_dt_xlsx, "bad_date.xlsx")
    assert dp.header() == ["id", "d"]
    dt_rows = list(dp.rows())
    dp.close()
    assert len(dt_rows) == 1 and isinstance(dt_rows[0][1], RowError) and dt_rows[0][1].fatal is True

    # 5-g) UTF-16 XML 내 DOCTYPE/ENTITY 선언 차단
    utf16_xxe = tmp_path / "utf16_xxe.xlsx"
    u16_sheet = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE foo [ <!ENTITY lol "expanded"> ]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>&lol;</t></is></c></row>'
        '</sheetData></worksheet>'
    ).encode("utf-16")
    with zipfile.ZipFile(utf16_xxe, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(huge_header, "r") as src_zf:
            for item in src_zf.infolist():
                if item.filename != "xl/worksheets/sheet1.xml":
                    zf.writestr(item.filename, src_zf.read(item.filename))
        zf.writestr("xl/worksheets/sheet1.xml", u16_sheet)
    with pytest.raises(IntakeError) as u16_exc:
        XlsxParser(utf16_xxe, "utf16_xxe.xlsx").header()
    assert u16_exc.value.detail == "invalid_csv"

    # 5-h) 5,000자리 <row r="..."> / 최대 행 수 초과 → ValueError 대신 IntakeError / fatal RowError
    long_row_xlsx = tmp_path / "long_row_num.xlsx"
    with zipfile.ZipFile(long_row_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(huge_header, "r") as src_zf:
            for item in src_zf.infolist():
                if item.filename != "xl/worksheets/sheet1.xml":
                    zf.writestr(item.filename, src_zf.read(item.filename))
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                    f'<row r="{"9" * 5000}"><c r="A1" t="inlineStr"><is><t>id</t></is></c></row>'
                    '</sheetData></worksheet>')
    with pytest.raises(IntakeError) as row_exc:
        XlsxParser(long_row_xlsx, "long_row_num.xlsx").header()
    assert row_exc.value.detail == "invalid_csv"

    over_max_row_xlsx = tmp_path / "over_max_row.xlsx"
    with zipfile.ZipFile(over_max_row_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(huge_header, "r") as src_zf:
            for item in src_zf.infolist():
                if item.filename != "xl/worksheets/sheet1.xml":
                    zf.writestr(item.filename, src_zf.read(item.filename))
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                    '<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c></row>'
                    f'<row r="{"9" * 5000}"><c r="A2" t="inlineStr"><is><t>1</t></is></c></row>'
                    '</sheetData></worksheet>')
    orp = XlsxParser(over_max_row_xlsx, "over_max_row.xlsx")
    assert orp.header() == ["id"]
    orows = list(orp.rows())
    orp.close()
    assert len(orows) == 1 and isinstance(orows[0][1], RowError) and orows[0][1].fatal is True

    # 5-i) ZIP 엔트리 암호화 플래그(flag_bits & 0x1) 사전 차단 → RuntimeError 대신 invalid_xlsx
    zipenc_bytes = bytearray(make_xlsx_bytes([{"name": "S1", "rows": [["id"], ["1"]]}]))
    with zipfile.ZipFile(io.BytesIO(zipenc_bytes), "r") as zf_enc:
        rels_off = zf_enc.getinfo("xl/_rels/workbook.xml.rels").header_offset
    zipenc_bytes[rels_off + 6] |= 0x01
    cd_pos = zipenc_bytes.find(b"PK\x01\x02")
    while cd_pos != -1:
        zipenc_bytes[cd_pos + 8] |= 0x01
        cd_pos = zipenc_bytes.find(b"PK\x01\x02", cd_pos + 4)
    zipenc_xlsx = tmp_path / "zipenc.xlsx"
    zipenc_xlsx.write_bytes(bytes(zipenc_bytes))
    with pytest.raises(IntakeError) as zenc_exc:
        XlsxParser(zipenc_xlsx, "zipenc.xlsx").header()
    assert zenc_exc.value.detail == "invalid_xlsx"

    # 5-j) t="s" 공유 문자열 인덱스 범위 초과/음수/비숫자 거절 (빈 <c t="s"/>만 빈 값 허용)
    for bad_idx in ("-1", "999", "abc"):
        bad_sst = tmp_path / f"bad_sst_{bad_idx}.xlsx"
        bad_sst.write_bytes(
            make_xlsx_bytes(
                [{"name": "S1", "rows": [["id", "note"], ["E1", {"t": "s", "v": bad_idx}]]}],
                shared_strings=["alpha", "beta"],
            )
        )
        sp = XlsxParser(bad_sst, f"bad_sst_{bad_idx}.xlsx")
        assert sp.header() == ["id", "note"]
        srows = list(sp.rows())
        sp.close()
        assert len(srows) == 1 and isinstance(srows[0][1], RowError) and srows[0][1].fatal is True

    blank_sst = tmp_path / "blank_sst.xlsx"
    blank_sst.write_bytes(
        make_xlsx_bytes(
            [{"name": "S1", "rows": [["id", "note"], ["E1", {"t": "s", "v": ""}]]}],
            shared_strings=["alpha", "beta"],
        )
    )
    bp = XlsxParser(blank_sst, "blank_sst.xlsx")
    assert bp.header() == ["id", "note"]
    assert list(bp.rows()) == [(2, ["E1", ""])]
    bp.close()

    # 6) 파일 이름 검증 분리: Studio(check_filename)는 .csv만, 관리 서비스(check_ingest_filename)는 .csv·.xlsx 허용
    assert upload.check_ingest_filename("orders.xlsx") == "orders.xlsx"
    assert upload.check_ingest_filename("orders.CSV") == "orders.CSV"
    with pytest.raises(upload.UploadError) as uexc:
        upload.check_filename("orders.xlsx")
    assert uexc.value.detail == "invalid_filename"
    with pytest.raises(upload.UploadError) as uexc2:
        upload.check_ingest_filename("../evil.xlsx")
    assert uexc2.value.detail == "invalid_filename"

    # 7) make_parser strict=False는 .xlsx 자동 선택, strict=True는 CsvParser 유지
    p_auto = make_parser({"kind": "csv", "path": tmp_path / "x.xlsx", "name": "x.xlsx"}, strict=False)
    assert isinstance(p_auto, XlsxParser)


def test_scaffold_and_newtable_support_xlsx(tmp_path):
    xlsx_raw = make_xlsx_bytes(
        [
            {
                "name": "Items",
                "rows": [
                    ["Item ID", "Amount", "Active"],
                    ["I1", "100", {"t": "b", "v": "1"}],
                    ["I2", "250", {"t": "b", "v": "0"}],
                    ["I3", "300", {"t": "b", "v": "1"}],
                ],
            }
        ]
    )
    inf = scaffold.infer({"items.xlsx": xlsx_raw})
    assert inf["tables"][0]["name"] == "items" and inf["tables"][0]["rows"] == 3
    rendered = scaffold.render("demo", inf, {"items.xlsx": xlsx_raw})
    assert "data/items.csv" in rendered and rendered["data/items.csv"].startswith(b"item_id,amount,active\n")
    scaffold.verify("demo", rendered)

    # newtable.infer_one + build
    import shutil
    shutil.copytree(FIXTURES / "ledger", tmp_path / "ledger")
    proj = config.load_project("ledger", tmp_path)
    t_inf = newtable.infer_one(proj, "items.xlsx", xlsx_raw)
    ops, files = newtable.build(proj, t_inf, xlsx_raw, "items.xlsx", mode="append")
    assert ops[0]["op"] == "table.add" and any(k.endswith(".csv.gz") for k in files)


# ---------------------------------------------------------------- 3. CursorParser & DB 보안 단위 테스트

def test_cursor_parser_streaming_types_and_non_scalar_errors():
    class StreamCur:
        description = [("id",), ("flag",), ("dec",), ("flt",), ("dt",), ("ts",), ("uid",), ("bad",)]

        def __init__(self):
            self.calls = 0
            self.total = 2500

        def fetchmany(self, n):
            start = self.calls * n
            if start >= self.total:
                return []
            end = min(start + n, self.total)
            self.calls += 1
            rows = []
            for i in range(start + 1, end + 1):
                bad = b"\xff\xfe" if i == 2005 else (["a"] if i == 2006 else None)
                flt = float("nan") if i == 2007 else 1.5
                rows.append((
                    i,
                    i % 2 == 0,
                    Decimal("12.34"),
                    flt,
                    date(2026, 11, 20),
                    datetime(2026, 11, 20, 9, 0, 0),
                    uuid.UUID("12345678-1234-5678-1234-567812345678"),
                    bad,
                ))
            return rows

    p = CursorParser(StreamCur(), "db:orders.sql", itersize=500)
    assert p.header() == ["id", "flag", "dec", "flt", "dt", "ts", "uid", "bad"]
    out = list(p.rows())
    assert len(out) == 2500
    assert out[0] == (1, ["1", "false", "12.34", "1.5", "2026-11-20", "2026-11-20T09:00:00", "12345678-1234-5678-1234-567812345678", ""])
    assert isinstance(out[2004][1], RowError) and out[2004][1].where == "db:orders.sql:2005:bad"
    assert isinstance(out[2005][1], RowError) and out[2005][1].where == "db:orders.sql:2006:bad"
    assert out[2006][1][3] == "nan"

    # cursor_factory는 호출할 때마다 새 커서를 만들고 close()로 소유한 커서를 닫는다
    closed: list[int] = []

    class FactoryCur:
        description = [("id",)]

        def __init__(self, idx: int):
            self.idx = idx
            self.done = False

        def fetchmany(self, n):
            if self.done:
                return []
            self.done = True
            return [(self.idx,)]

        def close(self):
            closed.append(self.idx)

    seq = iter([1, 2])
    src = {"kind": "cursor", "cursor_factory": lambda: FactoryCur(next(seq)), "name": "db:re"}
    p1 = make_parser(src, strict=False)
    assert list(p1.rows()) == [(1, ["1"])]
    p1.close()
    p2 = make_parser(src, strict=False)
    assert list(p2.rows()) == [(1, ["2"])]
    p2.close()
    assert closed == [1, 2]


def test_dsn_env_validation_sql_guard_and_error_sanitization(monkeypatch, tmp_path):
    with pytest.raises(IngestError) as exc:
        validate_dsn_env("bad-env")
    assert exc.value.status == 400
    monkeypatch.delenv("MISSING_SRC_DSN", raising=False)
    with pytest.raises(IngestError) as exc2:
        validate_dsn_env("MISSING_SRC_DSN")
    assert exc2.value.status == 400

    secret_dsn = "postgresql://svc_user:s3cr3t_pw@internal-db.example:5432/prod"
    raw_err = RuntimeError(f"connection failed for {secret_dsn} password=s3cr3t_pw user=svc_user")
    sanitized = sanitize_db_error(raw_err, secret_dsn)
    assert "s3cr3t_pw" not in sanitized
    assert "svc_user" not in sanitized
    assert "internal-db.example" not in sanitized
    assert secret_dsn not in sanitized

    # SQLSTATE 및 서버 오류 메시지 내 SQL 리터럴/값 비노출 확인
    class FakePgErr(Exception):
        sqlstate = "22P02"
        diag = SimpleNamespace(message_primary='invalid input syntax for type integer: "customer-secret"')

    sanitized_pg = sanitize_db_error(FakePgErr("raw"))
    assert "customer-secret" not in sanitized_pg
    assert "22P02" in sanitized_pg
    sanitized_lit = sanitize_db_error(RuntimeError("bad value 'customer-secret' near \"col-secret\""))
    assert "customer-secret" not in sanitized_lit
    assert "col-secret" not in sanitized_lit

    qfile = tmp_path / "q.sql"
    qfile.write_text("SELECT 1; DROP TABLE users;", encoding="utf-8")
    with pytest.raises(IngestError) as exc3:
        build_source_query(query_file=qfile)
    assert exc3.value.detail == "invalid_query"

    qfile.write_text("DELETE FROM users", encoding="utf-8")
    with pytest.raises(IngestError) as exc4:
        build_source_query(query_file=qfile)
    assert exc4.value.detail == "invalid_query"


# ---------------------------------------------------------------- 4. 민감 열 오류 마스킹 단위 테스트

def test_redact_sensitive_errors_masks_values_and_raw_rows():
    table_spec = {
        "primary_key": ["id"],
        "columns": {
            "id": {"type": "text", "required": True},
            "phone": {"type": "text", "sensitive": True, "pattern": r"^\d+$"},
            "note": {"type": "text"},
        },
    }
    errors = [
        "bad.csv:2:phone: 형식 불일치 '010-secret-1234' (pattern ^\\d+$)",
        "bad.csv:3:phone: 정수가 아님 'secret'val'",
        "bad.csv:4:-: 값 개수 4개가 머리글 3개와 다름",
        "원래 칸: E1 | 010-secret-9999 | memo | extra",
    ]
    masked = redact_sensitive_errors(table_spec, errors)
    joined = "\n".join(masked)
    assert "010-secret-1234" not in joined
    assert "secret'val" not in joined
    assert "010-secret-9999" not in joined
    assert "bad.csv:2:phone: 형식 불일치 '***' (pattern ^\\d+$)" == masked[0]
    assert "bad.csv:3:phone: 정수가 아님 '***'" == masked[1]
    assert masked[3] == "원래 칸: ***"

    # 여러 줄을 포함한 민감 기본 키 중복 및 여러 줄 검증 오류도 누출 없이 마스킹
    pk_spec = {
        "primary_key": ["secret_pk"],
        "columns": {
            "secret_pk": {"type": "text", "sensitive": True, "pattern": r"^[a-z]+$"},
        },
    }
    multiline_errs = [
        "dup.csv:3:secret_pk: 기본 키 중복 (line1-secret\nline2-secret, 처음 나온 줄 dup.csv:2)",
        "dup.csv:4:secret_pk: 형식 불일치 'line1-secret\nline2-secret' (pattern ^[a-z]+$)",
    ]
    masked_ml = redact_sensitive_errors(pk_spec, multiline_errs)
    joined_ml = "\n".join(masked_ml)
    assert "line1-secret" not in joined_ml and "line2-secret" not in joined_ml
    assert masked_ml[0] == "dup.csv:3:secret_pk: 기본 키 중복 (***, 처음 나온 줄 dup.csv:2)"
    assert masked_ml[1] == "dup.csv:4:secret_pk: 형식 불일치 '***' (pattern ^[a-z]+$)"


# ---------------------------------------------------------------- 5. 이어 올리기(Uploads) 수명주기 단위 테스트

def test_resumable_upload_lifecycle_conflicts_checksum_and_cleanup(tmp_path, monkeypatch):
    up = Uploads()
    data = b"id,state\n" + b"E1,open\n" * 50
    total = len(data)
    sha = hashlib.sha256(data).hexdigest()

    # 크기 제한 초과 → 413 upload_too_large
    monkeypatch.setenv("INGEST_MAX_UPLOAD_MB", "1")
    with pytest.raises(IngestError) as exc:
        up.start_resumable("entries", "big.csv", 2 * 1024 * 1024, "u1")
    assert exc.value.detail == "upload_too_large"

    # 세션 생성
    s = up.start_resumable("entries", "batch.csv", total, "u1", sha256=sha)
    uid = s["upload_id"]
    assert s["status"] == "receiving" and s["offset"] == 0 and s["total_size"] == total

    # 미완료 세션 get() → 409 upload_incomplete
    with pytest.raises(IngestError) as exc_inc:
        up.get(uid, "entries", "u1")
    assert exc_inc.value.detail == "upload_incomplete"

    # 타 사용자 접근 → 410 upload_expired
    with pytest.raises(IngestError) as exc_other:
        up.status(uid, "u2")
    assert exc_other.value.detail == "upload_expired"

    # 1번째 조각 전송
    p1 = data[:100]
    held = up.begin_chunk(uid, "u1", 0, content_length=len(p1))
    try:
        res1 = up.commit_chunk(held, 0, p1)
    finally:
        up.end_chunk(held)
    assert res1["offset"] == 100 and res1["status"] == "receiving"

    # 건너뛴 오프셋(Gap) → 409 offset_mismatch
    with pytest.raises(IngestError) as exc_gap:
        up.begin_chunk(uid, "u1", 120, content_length=10)
    assert exc_gap.value.detail == "offset_mismatch" and exc_gap.value.extra["offset"] == 100

    # 완전 중복 조각(동일 바이트) → 200 멱등 응답
    held = up.begin_chunk(uid, "u1", 0, content_length=len(p1))
    try:
        dup_res = up.commit_chunk(held, 0, p1)
    finally:
        up.end_chunk(held)
    assert dup_res["offset"] == 100

    # 중복 위치 바이트 불일치 → 409 offset_mismatch
    held = up.begin_chunk(uid, "u1", 0, content_length=10)
    try:
        with pytest.raises(IngestError) as exc_bad_dup:
            up.commit_chunk(held, 0, b"Z" * 10)
        assert exc_bad_dup.value.detail == "offset_mismatch" and exc_bad_dup.value.extra["offset"] == 100
    finally:
        up.end_chunk(held)

    # 부분 겹침 조각 (50..150) → 앞 50바이트 일치, 꼬리 50바이트 이어 쓰기
    overlap_chunk = data[50:150]
    held = up.begin_chunk(uid, "u1", 50, content_length=len(overlap_chunk))
    try:
        res_ov = up.commit_chunk(held, 50, overlap_chunk)
    finally:
        up.end_chunk(held)
    assert res_ov["offset"] == 150

    # 동시 PATCH 잠금 충돌 → 409 upload_in_progress
    held_busy = up.begin_chunk(uid, "u1", 150)
    try:
        with pytest.raises(IngestError) as exc_busy:
            up.begin_chunk(uid, "u1", 150)
        assert exc_busy.value.detail == "upload_in_progress"
    finally:
        up.end_chunk(held_busy)

    # 나머지 조각 전송 → status: ready
    rest = data[150:]
    held = up.begin_chunk(uid, "u1", 150, content_length=len(rest))
    try:
        res_done = up.commit_chunk(held, 150, rest)
    finally:
        up.end_chunk(held)
    assert res_done["status"] == "ready" and res_done["offset"] == total
    got = up.get(uid, "entries", "u1")
    assert got.files[0][1].read_bytes() == data

    # 취소(cancel) 시 디스크 즉시 삭제
    folder_path = got.files[0][1].parent
    up.cancel(uid, "u1")
    assert not folder_path.exists()

    # sha256 불일치 시 422 checksum_mismatch + 디스크 파일 즉시 삭제
    s_bad = up.start_resumable("entries", "bad.csv", len(data), "u1", sha256="0" * 64)
    uid_bad = s_bad["upload_id"]
    path_bad = up._items[uid_bad].files[0][1]
    held = up.begin_chunk(uid_bad, "u1", 0, content_length=len(data))
    try:
        with pytest.raises(IngestError) as exc_sha:
            up.commit_chunk(held, 0, data)
        assert exc_sha.value.detail == "checksum_mismatch"
    finally:
        up.end_chunk(held)
    assert not path_bad.parent.exists()
    with pytest.raises(IngestError) as exc_gone:
        up.status(uid_bad, "u1")
    assert exc_gone.value.detail == "upload_expired"
