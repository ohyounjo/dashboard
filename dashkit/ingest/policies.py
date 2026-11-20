"""충돌 정책(설계서 5-5, contracts/ingest.md "충돌 정책"). 같은 기본 키가 이미 있을 때 무엇을 하는가.

테이블의 속성(`schema.yaml`의 `on_conflict`)이다. 정책은 등록 표 `POLICIES`의 함수 하나씩이고, apply_batch가 부른다.
정책 함수는 **이미 있는 키의 행**만 다룬다. 새 키의 행을 넣는 일은 apply_batch가 정책과 상관없이 한다.

| 정책 | 이미 있는 키의 행 |
|---|---|
| error(기본) | 하나라도 있으면 전체 거절(key_conflict, 앞 20개 키) |
| skip | 그대로 둔다(skipped) |
| update | 배치의 머리글에 있는 열만 바꾼다. 값이 모두 같으면 unchanged |

정책을 더하려면 함수 하나를 등록하고 contracts/schema.schema.json의 on_conflict enum에 값 하나를 더한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg
from psycopg import sql

from ..loader.record import canonical

MAX_KEYS = 20


class BatchError(Exception):
    """배치를 넣을 수 없음. detail은 API 오류 코드, errors는 위치가 붙은 오류, extra는 응답에 더할 값."""

    def __init__(self, detail: str, message: str, errors: list[str] | None = None, **extra: Any):
        self.detail = detail
        self.message = message
        self.errors = list(errors or [])
        self.extra = extra
        super().__init__(message if not self.errors else f"{message}: " + "; ".join(self.errors[:5]))


@dataclass
class ApplyContext:
    cur: psycopg.Cursor
    table: str
    target: sql.Composable          # raw.<테이블>
    temp: sql.Identifier            # 임시 테이블(배치의 행)
    pk: list[str]
    header: list[str]               # 배치의 머리글(정규형, 스키마 순서)
    matches: int                    # 이미 있는 키의 행 수
    counts: dict[str, int]
    changes_limit: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def set_columns(self) -> list[str]:
        return [c for c in self.header if c not in self.pk]

    def join(self, left: str = "t", right: str = "b") -> sql.Composed:
        return sql.SQL(" AND ").join(
            sql.SQL("{}.{} = {}.{}").format(sql.Identifier(left), sql.Identifier(c), sql.Identifier(right), sql.Identifier(c))
            for c in self.pk)


def key_label(values: list[Any]) -> str:
    return ", ".join("" if v is None else str(canonical(v)) for v in values)


def _error(ctx: ApplyContext) -> None:
    if not ctx.matches:
        return
    pk = sql.SQL(", ").join(sql.SQL("b.{}").format(sql.Identifier(c)) for c in ctx.pk)
    ctx.cur.execute(sql.SQL("SELECT {} FROM {} b JOIN {} t ON {} ORDER BY b._at LIMIT %s").format(
        pk, ctx.temp, ctx.target, ctx.join()), (MAX_KEYS,))
    keys = [key_label(list(r)) for r in ctx.cur.fetchall()]
    more = f" 외 {ctx.matches - len(keys)}건" if ctx.matches > len(keys) else ""
    raise BatchError("key_conflict", f"이미 있는 키가 {ctx.matches}개 있습니다(충돌 정책 error)",
                     [f"{ctx.table}: 이미 있는 키 {', '.join(keys)}{more}"], keys=keys)


def _skip(ctx: ApplyContext) -> None:
    ctx.counts["skipped"] += ctx.matches


def _update(ctx: ApplyContext) -> None:
    cols = ctx.set_columns
    if not ctx.matches:
        return
    if not cols:
        ctx.counts["unchanged"] += ctx.matches
        return
    left = sql.SQL("({})").format(sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in cols))
    right = sql.SQL("({})").format(sql.SQL(", ").join(sql.SQL("b.{}").format(sql.Identifier(c)) for c in cols))
    if len(cols) == 1:
        left = sql.SQL("t.{}").format(sql.Identifier(cols[0]))
        right = sql.SQL("b.{}").format(sql.Identifier(cols[0]))
    differs = sql.SQL("{} IS DISTINCT FROM {}").format(left, right)
    if ctx.changes_limit:
        pk = sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in ctx.pk)
        before = sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in cols)
        after = sql.SQL(", ").join(sql.SQL("b.{}").format(sql.Identifier(c)) for c in cols)
        ctx.cur.execute(sql.SQL("SELECT {}, {}, {} FROM {} t JOIN {} b ON {} WHERE {} ORDER BY b._at LIMIT %s").format(
            pk, before, after, ctx.target, ctx.temp, ctx.join(), differs), (ctx.changes_limit,))
        n = len(ctx.pk)
        for r in ctx.cur.fetchall():
            old, new = r[n:n + len(cols)], r[n + len(cols):]
            ctx.changes.append({
                "key": key_label(list(r[:n])),
                "before": {c: canonical(o) for c, o, w in zip(cols, old, new) if o != w},
                "after": {c: canonical(w) for c, o, w in zip(cols, old, new) if o != w},
            })
    assign = sql.SQL(", ").join(sql.SQL("{} = b.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in cols)
    ctx.cur.execute(sql.SQL("UPDATE {} AS t SET {} FROM {} b WHERE {} AND {}").format(
        ctx.target, assign, ctx.temp, ctx.join(), differs))
    updated = ctx.cur.rowcount
    ctx.counts["updated"] += updated
    ctx.counts["unchanged"] += ctx.matches - updated


POLICIES: dict[str, Callable[[ApplyContext], None]] = {
    "error": _error,
    "skip": _skip,
    "update": _update,
}
DEFAULT_POLICY = "error"
