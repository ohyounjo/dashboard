"""Studio 화면(web/studio/)의 정적 검사. 브라우저 없이 볼 수 있는 것만 본다.

- 디자인 토큰·탭 아이콘이 웹앱 원본(web/styles.css, web/icons.js)의 사본과 어긋나지 않는지
- CSP(`default-src 'self'`)에 걸리는 것(인라인 스크립트·핸들러·style, 외부 주소)이 없는지
- 설정·데이터 값을 HTML로 넣는 API(innerHTML 등)를 쓰지 않는지
- 문구가 strings.js 한 파일에 모여 있는지, API 호출이 api.js 한 파일에 모여 있는지
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import ROOT

WEB = ROOT / "web"
STUDIO = WEB / "studio"
STATIC_PREFIX = "/studio/static/"          # contracts/studio.md: `/`가 index.html, 나머지는 /studio/static/<파일>


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _js_files() -> list[Path]:
    return sorted(STUDIO.glob("*.js"))


def _without_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    # 줄 주석: 줄 처음이나 공백 뒤의 `//`부터 줄 끝까지(문자열 안의 "http://…"는 앞이 공백이 아니라 남는다)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in js.splitlines())


def _token_block(css: str) -> str:
    """`:root {`부터 `[data-tone="neutral"]` 줄 끝까지."""
    start = css.index(":root {")
    end = css.index("\n", css.index('[data-tone="neutral"]')) + 1
    return css[start:end]


def _tab_icon_block(js: str) -> str:
    """도형 도우미(`const r =`)부터 TAB_ICONS 정의 끝까지."""
    start = js.index("const r = ")
    end = js.index("};", js.index("const TAB_ICONS")) + 2
    return js[start:end]


def test_files_are_flat():
    # 정적 파일 경로가 /studio/static/<파일> 한 단계라서 하위 폴더를 두지 않는다
    assert [p.name for p in STUDIO.iterdir() if p.is_dir()] == []
    assert (STUDIO / "index.html").is_file()


def test_tokens_match_webapp():
    block = _token_block(_read(WEB / "styles.css"))
    assert "--primary" in block and "prefers-color-scheme: dark" in block and '[data-theme="dark"]' in block
    assert block in _read(STUDIO / "tokens.css"), "web/studio/tokens.css가 web/styles.css의 토큰 블록과 다름(블록을 다시 복사)"


def test_tokens_file_has_nothing_else():
    rest = _read(STUDIO / "tokens.css").replace(_token_block(_read(WEB / "styles.css")), "")
    assert re.sub(r"/\*.*?\*/", "", rest, flags=re.S).strip() == "", "tokens.css에는 원본 토큰 블록만 둔다"


def test_studio_css_uses_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", _read(STUDIO / "studio.css"), flags=re.S)
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b", css) == [], "studio.css에 색 값을 직접 쓰지 않는다(토큰 변수 사용)"
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", _read(STUDIO / "tokens.css")))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    assert used <= defined, f"정의되지 않은 토큰: {sorted(used - defined)}"


def test_tab_icons_match_webapp():
    assert _tab_icon_block(_read(WEB / "icons.js")) == _tab_icon_block(_read(STUDIO / "icons.js")), \
        "web/studio/icons.js의 탭 아이콘 구간이 web/icons.js와 다름(구간을 다시 복사)"


def test_html_has_no_inline_code():
    html = _read(STUDIO / "index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S | re.I)
    assert scripts, "스크립트 태그가 없음"
    for attrs, body in scripts:
        assert "src=" in attrs and body.strip() == "", "인라인 스크립트는 CSP에 막힌다"
    assert not re.search(r"<style\b", html, flags=re.I)
    assert not re.search(r"\sstyle\s*=", html, flags=re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I), "인라인 이벤트 핸들러는 CSP에 막힌다"
    assert not re.search(r"javascript:", html, flags=re.I)


def test_html_references_only_static_files():
    html = _read(STUDIO / "index.html")
    refs = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html)
    assets = [r for r in refs if not r.startswith("#")]
    assert assets
    for ref in assets:
        assert ref.startswith(STATIC_PREFIX), f"정적 파일은 {STATIC_PREFIX} 아래여야 함: {ref}"
        assert (STUDIO / ref[len(STATIC_PREFIX):]).is_file(), f"없는 파일: {ref}"


def test_js_imports_are_relative_and_exist():
    for f in _js_files():
        for spec in re.findall(r"""^\s*import\s[^;]*?from\s+["']([^"']+)["']""", _read(f), flags=re.M):
            assert re.fullmatch(r"\./[a-z0-9-]+\.js", spec), f"{f.name}: 상대 경로 한 단계만 import한다({spec})"
            assert (STUDIO / spec[2:]).is_file(), f"{f.name}: 없는 모듈 {spec}"
        assert not re.search(r"\bimport\s*\(", _without_comments(_read(f))), f"{f.name}: 동적 import 없음"


def test_js_does_not_build_html_or_eval():
    banned = [r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function\b",
              r"""setAttribute\(\s*["']style["']""", r"\.cssText\b", r"""setAttribute\(\s*["']on""", r"DOMParser", r"srcdoc"]
    for f in _js_files():
        code = _without_comments(_read(f))
        for pat in banned:
            assert not re.search(pat, code), f"{f.name}: {pat} 사용"


def test_no_external_urls():
    allowed = {"http://www.w3.org/2000/svg"}
    for f in [*_js_files(), STUDIO / "studio.css", STUDIO / "tokens.css", STUDIO / "index.html"]:
        urls = set(re.findall(r"""(?:https?:)?//[A-Za-z0-9.-]+\.[a-z]{2,}[^\s"'`)]*""", _without_comments(_read(f)) if f.suffix == ".js" else _read(f)))
        assert urls <= allowed, f"{f.name}: 외부 주소 {sorted(urls - allowed)}"
    for name in ("studio.css", "tokens.css"):
        css = _read(STUDIO / name)
        assert "@import" not in css and "@font-face" not in css and "url(" not in css


def test_network_calls_only_in_api_module():
    for f in _js_files():
        code = _without_comments(_read(f))
        uses = re.search(r"\bfetch\s*\(|XMLHttpRequest|EventSource|WebSocket|sendBeacon", code)
        if f.name == "api.js":
            assert uses and "/studio/api" in code
        else:
            assert not uses, f"{f.name}: 네트워크 호출은 api.js에만 둔다"
            assert "/studio/api" not in code, f"{f.name}: API 경로는 api.js에만 둔다"


def test_strings_are_in_one_file():
    strings = _read(STUDIO / "strings.js")
    keys = set(re.findall(r"^  ([A-Za-z][A-Za-z0-9_]*):", strings, flags=re.M))
    assert len(keys) > 100
    used: set[str] = set()
    for f in _js_files():
        if f.name == "strings.js":
            continue
        code = _read(f)
        used |= set(re.findall(r"\bS\.([A-Za-z][A-Za-z0-9_]*)", code))
        # 한글 문구를 코드에 직접 쓰지 않는다(주석 제외)
        hangul = [line.strip() for line in _without_comments(code).splitlines() if re.search(r"[가-힣]", line)]
        assert hangul == [], f"{f.name}: 문구는 strings.js에 둔다: {hangul[:3]}"
    assert used - keys == set(), f"strings.js에 없는 문구 키: {sorted(used - keys)}"
    assert keys - used == set(), f"쓰이지 않는 문구 키: {sorted(keys - used)}"


def test_only_5a_screens():
    # 5b단계(대시보드 배치, 필터)의 연산은 아직 보내지 않는다
    code = "\n".join(_without_comments(_read(f)) for f in _js_files())
    ops = set(re.findall(r"""op:\s*["']([a-z]+\.[a-z]+)["']""", code))
    assert ops == {"app.update", "tab.add", "tab.update", "tab.move", "tab.remove",
                   "section.add", "section.update", "section.move", "section.remove"}
