"""운용 화면(web/admin/)과 공용 모듈(web/shared/)의 정적 검사. 브라우저 없이 볼 수 있는 것만 본다.

- CSP(`default-src 'self'`)에 걸리는 것(인라인 스크립트·핸들러·style)과 값을 HTML로 넣는 API(innerHTML 등)가 없는지
- import 대상이 있는지(web/shared/는 그 폴더 안만: studio 컨테이너는 web/studio/와 web/shared/만 서빙한다)
- 문구가 web/strings.js에만 있는지(코드의 한글은 주석에만), 데이터 메뉴가 쓰는 문구 키(S.admin.data)가 모두 있고 남는 것이 없는지
- 메뉴가 모두 MENUS에 등록돼 있는지, 데이터 메뉴는 관리 서비스가 떠 있을 때만 보이는지
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import ROOT

WEB = ROOT / "web"
ADMIN = WEB / "admin"
SHARED = WEB / "shared"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _admin_js() -> list[Path]:
    return sorted(ADMIN.glob("*.js")) + sorted((ADMIN / "menus").glob("*.js"))


def _all_js() -> list[Path]:
    return _admin_js() + sorted(SHARED.glob("*.js"))


def _without_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in js.splitlines())


def _imports(js: str) -> list[str]:
    return re.findall(r"""^\s*import\s[^;]*?from\s+["']([^"']+)["']""", js, flags=re.M)


def test_new_modules_exist():
    assert (ADMIN / "menus" / "tables.js").is_file()
    assert (SHARED / "fields.js").is_file()
    assert (SHARED / "ingest.js").is_file()
    assert (SHARED / "query.js").is_file()
    assert (ADMIN / "rows.js").is_file()
    assert (ADMIN / "menus" / "datasets.js").is_file()
    for name in ("datakit.js", "structure.js", "menus/history.js"):
        assert (ADMIN / name).is_file(), name
    assert (SHARED / "structure.js").is_file()


def test_html_has_no_inline_code():
    html = _read(ADMIN / "index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S | re.I)
    assert scripts
    for attrs, body in scripts:
        assert "src=" in attrs and body.strip() == "", "인라인 스크립트는 CSP에 막힌다"
    assert not re.search(r"<style\b", html, flags=re.I)
    assert not re.search(r"\sstyle\s*=", html, flags=re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I)
    assert not re.search(r"javascript:", html, flags=re.I)


def test_js_does_not_build_html_or_eval():
    banned = [r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function\b",
              r"""setAttribute\(\s*["']style["']""", r"\.cssText\b", r"""setAttribute\(\s*["']on""", r"DOMParser", r"srcdoc",
              r"\.style\b", r"javascript:"]
    for f in _all_js():
        code = _without_comments(_read(f))
        for pat in banned:
            assert not re.search(pat, code), f"{f.relative_to(ROOT)}: {pat} 사용"


def test_imports_exist():
    for f in _all_js():
        js = _read(f)
        for spec in _imports(js):
            assert spec.startswith("./") or spec.startswith("../"), f"{f.name}: 상대 경로로 import한다({spec})"
            target = (f.parent / spec).resolve()
            assert target.is_file(), f"{f.relative_to(ROOT)}: 없는 모듈 {spec}"
            assert target.is_relative_to(WEB), f"{f.relative_to(ROOT)}: web/ 밖의 모듈 {spec}"
            if f.parent == SHARED:
                assert target.parent == SHARED, f"{f.name}: web/shared/ 밖의 모듈을 import하지 않는다({spec})"
        assert not re.search(r"\bimport\s*\(", _without_comments(js)), f"{f.name}: 동적 import 없음"


def test_texts_live_in_strings_js():
    for f in _all_js():
        hangul = [line.strip() for line in _without_comments(_read(f)).splitlines() if re.search(r"[가-힣]", line)]
        assert hangul == [], f"{f.relative_to(ROOT)}: 문구는 web/strings.js에 둔다: {hangul[:3]}"


def _data_block_keys() -> set[str]:
    text = _read(WEB / "strings.js")
    start = text.index("\n    data: {\n")
    end = text.index("\n    },\n", start)
    return set(re.findall(r"^      ([A-Za-z][A-Za-z0-9_]*):", text[start:end], flags=re.M))


def test_data_menu_strings_match():
    keys = _data_block_keys()
    used: set[str] = set()
    # 데이터 쪽 화면(테이블, 행 표, 데이터셋)은 모두 S.admin.data(D)의 문구를 쓴다
    for f in (ADMIN / "menus" / "tables.js", ADMIN / "rows.js", ADMIN / "menus" / "datasets.js", ADMIN / "datakit.js",
              ADMIN / "structure.js", ADMIN / "menus" / "history.js"):
        code = _read(f)
        assert "const D = T.data;" in code, f"{f.name}: const D = T.data;"
        used |= set(re.findall(r"\bD\.([A-Za-z][A-Za-z0-9_]*)", code))
    assert used - keys == set(), f"strings.js의 S.admin.data에 없는 문구 키: {sorted(used - keys)}"
    assert keys - used == set(), f"쓰이지 않는 문구 키: {sorted(keys - used)}"
    assert re.search(r"menu: \{[^}]*\btables: ", _read(WEB / "strings.js")), "메뉴 이름(S.admin.menu.tables)이 없음"


def test_field_check_codes_have_texts():
    # fields.js가 돌려주는 검사 코드마다 문구가 있다
    codes = set(re.findall(r"""code: "([a-zA-Z]+)\"""", _read(SHARED / "fields.js")))
    assert {"required", "integer", "date", "enum", "pattern"} <= codes
    text = _read(WEB / "strings.js")
    start = text.index("      check: {")
    block = text[start:text.index("\n      },\n", start)]
    defined = set(re.findall(r"^        ([A-Za-z]+):", block, flags=re.M))
    assert codes <= defined, f"검사 코드의 문구가 없음: {sorted(codes - defined)}"


def test_menus_are_registered():
    shell = _read(ADMIN / "admin.js")
    listed = re.search(r"const MENUS = \[([^\]]*)\]", shell).group(1)
    for f in sorted((ADMIN / "menus").glob("*.js")):
        assert f'from "./menus/{f.name}"' in shell, f"admin.js가 menus/{f.name}을 import해야 함"
        name = re.search(rf'import (\w+) from "\./menus/{re.escape(f.name)}"', shell).group(1)
        assert re.search(rf"\b{name}\b", listed), f"MENUS에 {name}이 없음"
        code = _read(f)
        for key in ("key:", "label:", "match:", "render"):
            assert key in code, f"menus/{f.name}: {key}가 없음"


def test_data_menu_only_with_manager():
    # 관리 서비스가 떠 있지 않으면(502·404·연결 실패) 데이터 메뉴를 숨긴다(contracts/manage.md "서비스")
    shell = _without_comments(_read(ADMIN / "admin.js"))
    assert '"/api/manage/config"' in shell and "session.manage" in shell
    menu = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    assert re.search(r"when:\s*\(\)\s*=>\s*!!session\.manage", menu)


def test_network_calls_only_in_shell_and_kit():
    for f in _all_js():
        code = _without_comments(_read(f))
        uses = re.search(r"\bfetch\s*\(|XMLHttpRequest|EventSource|WebSocket|sendBeacon", code)
        if f.parent == ADMIN and f.name in ("admin.js", "kit.js"):
            continue
        assert not uses, f"{f.relative_to(ROOT)}: 네트워크 호출은 kit.js(api·apiForm·waitJob)로 한다"


def test_no_live_region_attribute():
    for f in [*_all_js(), ADMIN / "index.html"]:
        assert "aria-" + "live" not in _read(f), f"{f.name}: role=\"status\"를 쓴다"


def test_admin_css_uses_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", _read(ADMIN / "admin.css"), flags=re.S)
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b", css) == [], "admin.css에 색 값을 직접 쓰지 않는다(토큰 변수 사용)"
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", _read(WEB / "styles.css")))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    assert used <= defined, f"정의되지 않은 토큰: {sorted(used - defined)}"


def test_bulk_screen_uses_backend_paths():
    # 2b: 여러 파일 한 트랜잭션, 오류 행 빼고 넣기·빠진 행, 빈 양식, 머리글 맞춤 저장, 진행률(progress.step·bytes)
    code = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    for needle in ('`${BASE}/batches`', 'form.append("files"', 'form.append("map"', '"skip_invalid"', "skip_invalid: true",
                   "/rejects`", "/template`", "/aliases`", "upload_id: prev.upload_id"):
        assert needle in code, f"tables.js에 {needle}가 없음"
    shared = _read(SHARED / "ingest.js")
    assert "total_bytes" in shared and "bytes" in shared
    assert ".step" in _without_comments(_read(ADMIN / "datakit.js")), "진행 표시는 progress.step을 쓴다"
    # 짝짓기·진행률 같은 판단은 DOM 없는 shared/ingest.js에 두고 node --test로 시험한다
    assert not re.search(r"\bdocument\b|\bwindow\b", _without_comments(shared))
    assert (ROOT / "tests" / "js" / "ingest.test.mjs").is_file()


def test_aliases_saving_is_admin_only_in_screen():
    # "다음에도 이렇게"는 admin에게만 보인다(서버도 require_admin)
    code = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    assert re.search(r"isAdmin\(\)\s*\?\s*checkbox\(D\.rememberMatch", code)


def test_rows_query_screens():
    # 3단계: 미리보기 탭과 데이터셋 상세는 같은 행 표(rows.js)를 쓰고, 계약의 응답 키를 다룬다
    rows = _without_comments(_read(ADMIN / "rows.js"))
    for needle in ("next_cursor", "res.changed", "truncated_cells", "res.total", "count = true", '"invalid_filter"'):
        assert needle in rows, f"rows.js에 {needle}가 없음"
    # 페이지 이동은 응답이 온 뒤에만 확정하고(pager = at), 읽는 동안 이동 버튼은 받지 않는다(같은 커서가 두 번 쌓이지 않게)
    assert "pager = at;" in rows and rows.count("if (!busy) load({ at:") == 2
    assert "pager = Q.pagerNext" not in rows and "pager = Q.pagerPrev" not in rows
    tables = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    assert "/rows/query`" in tables and "{ key }" in tables, "고치기 폼은 한 행을 key로 따로 읽는다"
    assert 't.on_conflict === "update"' in tables, "이 행 고치기는 update 테이블에만"
    assert 'method: "form"' in tables
    datasets = _without_comments(_read(ADMIN / "menus" / "datasets.js"))
    assert "/rows/query`" in datasets and "`${BASE}/datasets`" in datasets
    assert re.search(r"when:\s*\(\)\s*=>\s*!!session\.manage", datasets)
    listed = re.search(r"const MENUS = \[([^\]]*)\]", _read(ADMIN / "admin.js")).group(1)
    assert re.search(r"\btables,\s*datasets\b", listed), "데이터셋 메뉴는 데이터 다음"
    shared = _without_comments(_read(SHARED / "query.js"))
    assert not re.search(r"\bdocument\b|\bwindow\b", shared)
    assert (ROOT / "tests" / "js" / "query.test.mjs").is_file()


# 관리 서비스의 PATCH /config가 받는 연산(contracts/manage.md "구조 변경")
STRUCT_OPS = {"table.add", "table.update", "table.remove", "column.add", "column.update",
              "dataset.add", "dataset.update", "dataset.remove"}


def test_structure_screens_use_contract():
    # 4단계: 구조 변경 화면은 계약의 경로와 연산만 쓴다
    files = [ADMIN / "structure.js", ADMIN / "menus" / "tables.js", ADMIN / "menus" / "datasets.js", ADMIN / "menus" / "history.js",
             SHARED / "structure.js"]
    code = "\n".join(_without_comments(_read(f)) for f in files)
    for needle in ('api("PATCH", `${BASE}/config`', "/tables/infer`", 'api("POST", `${BASE}/tables`', "/compact`", "/used_by`",
                   "`${BASE}/history`", "/restore`", "`${BASE}/export`", "revision"):
        assert needle in code, f"구조 변경 화면에 {needle}가 없음"
    # 연산 이름: 화면의 `op: "…"`와 공용 모듈의 OPS 상수(Studio 검사가 web/shared의 `op: "…"`를 Studio 연산으로 세므로 상수로 둔다)
    ops = set(re.findall(r"""op:\s*["']([a-z]+\.[a-z]+)["']""", code))
    ops |= set(re.findall(r"""["']((?:table|column|dataset)\.[a-z]+)["']""", _without_comments(_read(SHARED / "structure.js"))))
    assert {"column.add", "column.update", "table.remove", "dataset.add"} <= ops
    assert ops <= STRUCT_OPS, f"계약에 없는 연산: {sorted(ops - STRUCT_OPS)}"
    contract = _read(ROOT / "contracts" / "manage.md")
    for op in sorted(ops):
        assert f"`{op}`" in contract, f"contracts/manage.md에 없는 연산: {op}"
    # revision은 폼을 연 때의 것을 보낸다(보내기 직전에 읽으면 그 사이 다른 사람의 변경을 덮어써도 409가 나지 않는다)
    assert "revision: await currentRevision(" not in code
    assert code.count("revision: t.revision") == 3 and code.count("revision: await revAtOpen") == 2


def test_structure_buttons_are_admin_only():
    # 바꾸는 버튼은 admin에게만 보이고(서버도 require_admin), 쓰이는 곳은 모두 본다
    tables = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    assert re.search(r"if \(isAdmin\(\)\) \{\s*const colBtn", tables), "열 추가·합치기·삭제는 admin에게만"
    assert re.search(r"if \(isAdmin\(\)\) \{\s*newBtn", tables), "새 테이블은 admin에게만"
    assert "isAdmin() ? (col) => openAction(columnRuleCard" in tables, "규칙 고치기는 admin에게만"
    assert 'key === "usage"' in tables and "usagePanel(t, alive)" in tables, "쓰이는 곳은 등급과 상관없이 보인다"
    datasets = _without_comments(_read(ADMIN / "menus" / "datasets.js"))
    assert re.search(r"if \(isAdmin\(\)\) \{\s*createBtn", datasets), "데이터셋 만들기는 admin에게만"
    history = _without_comments(_read(ADMIN / "menus" / "history.js"))
    assert re.search(r"when:\s*\(\)\s*=>\s*!!session\.manage && isAdmin\(\)", history), "설정 이력 메뉴는 admin에게만"
    listed = re.search(r"const MENUS = \[([^\]]*)\]", _read(ADMIN / "admin.js")).group(1)
    assert re.search(r"\bdatasets,\s*history\b", listed)
    shared = _without_comments(_read(SHARED / "structure.js"))
    assert not re.search(r"\bdocument\b|\bwindow\b", shared)
    assert (ROOT / "tests" / "js" / "structure.test.mjs").is_file()
