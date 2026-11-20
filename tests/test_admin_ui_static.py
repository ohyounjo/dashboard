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
    code = _read(ADMIN / "menus" / "tables.js")
    assert "const D = T.data;" in code
    used = set(re.findall(r"\bD\.([A-Za-z][A-Za-z0-9_]*)", code))
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
    assert ".step" in code
    # 짝짓기·진행률 같은 판단은 DOM 없는 shared/ingest.js에 두고 node --test로 시험한다
    assert not re.search(r"\bdocument\b|\bwindow\b", _without_comments(shared))
    assert (ROOT / "tests" / "js" / "ingest.test.mjs").is_file()


def test_aliases_saving_is_admin_only_in_screen():
    # "다음에도 이렇게"는 admin에게만 보인다(서버도 require_admin)
    code = _without_comments(_read(ADMIN / "menus" / "tables.js"))
    assert re.search(r"isAdmin\(\)\s*\?\s*checkbox\(D\.rememberMatch", code)
