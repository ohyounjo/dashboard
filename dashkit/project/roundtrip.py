"""ruamel.yaml round-trip으로 YAML을 고쳐 쓴다(주석·키 순서 보존). yamledit이 다룰 수 없을 때만 쓴다.

ruamel.yaml은 studio 이미지에만 있다(보강설계서 7절 D2). 이 모듈 밖에서는 import하지 않는다.
파일 전체를 다시 내보내므로 주석과 키 순서는 남지만 글자 단위로 같지는 않다
(flow 매핑의 안쪽 공백 `{ a: b }` → `{a: b}`, 키 뒤의 줄 맞춤 공백, 겹친 시퀀스의 들여쓰기가 표준 꼴로 바뀐다).
"""
from __future__ import annotations

import io
from typing import Any, Callable, MutableMapping

import yaml as pyyaml


class RoundTripError(Exception):
    pass


def edit(text: str, mutate: Callable[[MutableMapping], None]) -> str:
    """text를 읽어 mutate로 고친 뒤 다시 쓴 글."""
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    except ImportError as exc:      # studio 이미지가 아닌 곳
        raise RoundTripError(f"ruamel.yaml이 없음({exc})") from None

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 1 << 20            # 긴 줄을 접지 않는다
    yaml.indent(mapping=2, sequence=4, offset=2)
    try:
        doc = yaml.load(text)
    except YAMLError as exc:
        raise RoundTripError(f"YAML을 읽을 수 없음: {exc}") from None
    if not isinstance(doc, dict):
        raise RoundTripError("최상위가 매핑이 아님")
    known = set()
    _collect(doc, known)
    mutate(doc)
    _quote_new_text(doc, known, DoubleQuotedScalarString)
    out = io.StringIO()
    try:
        yaml.dump(doc, out)
    except YAMLError as exc:
        raise RoundTripError(f"YAML을 쓸 수 없음: {exc}") from None
    return out.getvalue()


def _collect(node: Any, known: set[int]) -> None:
    """읽은 그대로의 문자열 객체들. 이것들은 원문의 표기를 그대로 다시 쓴다."""
    values = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for v in values:
        if isinstance(v, str):
            known.add(id(v))
        else:
            _collect(v, known)


def _reads_back(value: str) -> bool:
    """PyYAML(config.py가 쓰는 YAML 1.1 규칙)이 인용 부호 없는 이 글을 같은 문자열로 읽는가."""
    try:
        return pyyaml.safe_load(value) == value and isinstance(pyyaml.safe_load(value), str)
    except pyyaml.YAMLError:
        return False


def _quote_new_text(node: Any, known: set[int], quoted: type) -> None:
    """새로 넣은 문자열 가운데 PyYAML이 다른 값으로 읽을 것(yes, on, 010 등)에 인용 부호를 붙인다.
    ruamel은 YAML 1.2 규칙으로 쓰므로 이런 글을 그대로 내보낸다."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                if id(v) not in known and type(v) is str and not _reads_back(v):
                    node[k] = quoted(v)
            else:
                _quote_new_text(v, known, quoted)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                if id(v) not in known and type(v) is str and not _reads_back(v):
                    node[i] = quoted(v)
            else:
                _quote_new_text(v, known, quoted)
