"""운용 화면 행 조회의 값 표현(contracts/manage.md "행 조회"): 브라우저의 수로 정확히 나타나지 않는 값은 문자열이다."""
from __future__ import annotations

import json
from decimal import Decimal

from dashkit.manage.routes.rows import _to_json


def test_values_the_browser_cannot_hold_exactly_are_strings():
    assert _to_json(9007199254740993, "integer") == "9007199254740993"
    assert _to_json(-9007199254740993, "integer") == "-9007199254740993"
    assert _to_json(9007199254740991, "integer") == 9007199254740991
    assert _to_json(Decimal("12345678901234567.5"), "number") == "12345678901234567.5"
    assert _to_json(Decimal("9007199254740993"), "number") == "9007199254740993"
    assert _to_json(Decimal("20.50"), "number") == 20.5 and _to_json(Decimal("7"), "number") == 7
    assert _to_json(Decimal("0.1000000000000000000001"), "number") == "0.1000000000000000000001"
    # JSON으로 갔다가 돌아와도 같은 값(문자열은 그대로, 수는 같은 수)
    for v in (9007199254740993, Decimal("12345678901234567.5")):
        out = json.loads(json.dumps(_to_json(v, "integer" if isinstance(v, int) else "number")))
        assert Decimal(str(out)) == v
