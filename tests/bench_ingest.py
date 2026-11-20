"""누적 테이블 실측(설계서 15절, 13절 D8). pytest가 모으지 않는 스크립트다(이름이 test_로 시작하지 않는다).

    python -m tests.bench_ingest [--rows 1000000] [--batches 200]

tools 컨테이너(데이터 DB 접속 환경변수)에서 실행한다. 임시 DB와 임시 프로젝트(tests/fixtures/ledger 사본)를 만들고
끝나면 지운다. 만든 데이터 파일은 임시 폴더에만 있다(커밋하지 않는다). 재는 것:

1. 행 N개를 첫 배치 하나로 둔 테이블의 apply(load) 시간
2. 그 테이블에 배치 하나를 더할 때 잠금 구간의 시간(1행, 1만 행, update 정책으로 1만 행 고치기)
3. 같은 N개를 배치 B개로 나눈 테이블의 apply 시간
4. (--upload-mb M) 그 테이블에 M MB짜리 파일 하나를 미리보기(dry_run)한 뒤 같은 디스크의 파일로 넣기(관리 서비스가
   upload_id로 확정할 때와 같은 길. 다시 올리지 않는다)
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from psycopg import sql

from dashkit import db
from dashkit.config import load_project
from dashkit.ingest import Item, ingest
from dashkit.loader import load

FIXTURE = Path(__file__).parent / "fixtures" / "ledger"
HEADER = ["id", "group_id", "state", "amount", "on_day", "flag", "qty", "memo"]


def row(i: int) -> list[str]:
    return [f"E{i}", "g1" if i % 2 else "g2", "open" if i % 3 else "done", f"{i % 1000}.{i % 100:02d}",
            f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", "true" if i % 5 else "false", str(i % 97),
            f"메모 {i}"]


def write(path: Path, start: int, stop: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for i in range(start, stop):
            w.writerow(row(i))


def project(root: Path, n: int, batches: int) -> Path:
    shutil.copytree(FIXTURE, root / "ledger")
    data = root / "ledger" / "data"
    for t in ("entries", "events", "marks"):
        shutil.rmtree(data / t, ignore_errors=True)
    (data / "entries.csv").unlink()
    if batches <= 1:
        write(data / "entries.csv", 1, n + 1)
    else:
        step = n // batches
        for b in range(batches):
            write(data / "entries" / f"{b + 1:06d}-bench.csv", 1 + b * step, 1 + (b + 1) * step if b < batches - 1 else n + 1)
    return root


def timed_load(root: Path, dbname: str) -> float:
    t0 = time.perf_counter()
    load(load_project("ledger", root), dbname=dbname)
    return time.perf_counter() - t0


def lock_section(root: Path, dbname: str, rows: list[dict], **kw) -> tuple[float, float]:
    """(전체 시간, 잠금 구간 시간). 잠금 구간 = 잠금을 잡으러 간 때부터 커밋해 돌아올 때까지."""
    marks: dict[str, float] = {}

    def progress(phase: str, _info) -> None:
        marks.setdefault(phase, time.perf_counter())

    t0 = time.perf_counter()
    ingest("ledger", [Item("entries", {"kind": "rows", "rows": rows})], projects_dir=root, dbname=dbname,
           progress=progress, **kw)
    t1 = time.perf_counter()
    return t1 - t0, t1 - marks["lock"]


def measure_paging(dbname: str, n: int) -> dict:
    """키 방식 커서로 첫 페이지·한가운데·마지막 페이지(200행)를 읽는 시간과 행 수(설계서 14절 3단계 완료 조건)."""
    from dashkit import paging

    cols = [paging.Column(c, t) for c, t in (("id", "text"), ("group_id", "text"), ("state", "text"),
                                              ("amount", "number"), ("on_day", "date"), ("flag", "boolean"),
                                              ("qty", "integer"), ("memo", "text"))]
    rel = sql.SQL("raw.entries")
    out: dict = {}
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        def page(body, label):
            q = paging.parse_query(body, cols)
            t0 = time.perf_counter()
            p = paging.fetch_page(cur, rel, cols, q, key_columns=["id"], version="v", total_rows=n)
            out[label] = {"rows": len(p.rows), "ms": round((time.perf_counter() - t0) * 1000, 1)}
            return p
        first = page({"limit": 200}, "first_200")
        mid_id = conn.execute("SELECT id FROM raw.entries ORDER BY id OFFSET %s LIMIT 1", (n // 2,)).fetchone()[0]
        doc = paging.decode_cursor(first.next_cursor)
        doc["last"] = [mid_id]
        page({"limit": 200, "cursor": paging.encode_cursor(doc)}, "middle_200")
        last_id = conn.execute("SELECT id FROM raw.entries ORDER BY id DESC OFFSET 150 LIMIT 1").fetchone()[0]
        doc["last"] = [last_id]
        page({"limit": 200, "cursor": paging.encode_cursor(doc)}, "last_page")
        page({"limit": 200, "sort": [{"column": "amount", "dir": "desc"}]}, "sorted_by_amount_200")
        page({"limit": 200, "filters": [{"column": "state", "op": "eq", "value": "done"}]}, "filtered_200")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batches", type=int, default=200)
    ap.add_argument("--upload-mb", type=int, default=0, help="이 크기(MB)의 파일 하나를 미리보기한 뒤 넣는다")
    ap.add_argument("--paging", action="store_true", help="행 조회의 페이지 시간(첫·깊은 페이지, 정렬·필터)을 잰다")
    args = ap.parse_args()
    dbname = f"dash_bench_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    out: dict = {"rows": args.rows, "batches": args.batches}
    try:
        with tempfile.TemporaryDirectory(prefix="dashkit-bench-") as tmp:
            root = project(Path(tmp) / "one", args.rows, 1)
            out["apply_one_batch_s"] = round(timed_load(root, dbname), 2)
            if args.paging:
                out["paging"] = measure_paging(dbname, args.rows)
            n = args.rows
            total, lock = lock_section(root, dbname, [dict(zip(HEADER, row(n + 1)))])
            out["add_1_row"] = {"total_s": round(total, 3), "lock_s": round(lock, 3)}
            new = [dict(zip(HEADER, row(i))) for i in range(n + 2, n + 10_002)]
            total, lock = lock_section(root, dbname, new)
            out["add_10k_rows"] = {"total_s": round(total, 3), "lock_s": round(lock, 3)}
            upd = [{"id": f"E{i}", "memo": f"고침 {i}"} for i in range(1, 10_001)]
            total, lock = lock_section(root, dbname, upd)
            out["update_10k_rows"] = {"total_s": round(total, 3), "lock_s": round(lock, 3)}
            out["apply_after_adds_s"] = round(timed_load(root, dbname), 2)
            if args.upload_mb:
                big = Path(tmp) / "big.csv"
                start, written = n + 20_000, 0
                with open(big, "w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f, lineterminator="\n")
                    w.writerow(HEADER)
                    i = start
                    while f.tell() < args.upload_mb * 1024 * 1024:
                        r = row(i)
                        r[-1] = r[-1] + " " + "가" * 20
                        w.writerow(r)
                        i += 1
                    written = i - start
                item = [Item("entries", {"kind": "csv", "path": big, "name": "big.csv"})]
                t0 = time.perf_counter()
                pre = ingest("ledger", item, projects_dir=root, dbname=dbname, dry_run=True)
                t1 = time.perf_counter()
                marks: dict[str, float] = {}
                ingest("ledger", item, projects_dir=root, dbname=dbname,
                       progress=lambda phase, _i: marks.setdefault(phase, time.perf_counter()))
                t2 = time.perf_counter()
                out["upload"] = {"mb": round(big.stat().st_size / 1e6, 1), "rows": written,
                                 "preview_s": round(t1 - t0, 2), "insert_s": round(t2 - t1, 2),
                                 "insert_lock_s": round(t2 - marks["lock"], 2),
                                 "preview_inserted": pre["batches"][0]["rows"]["inserted"]}
            root2 = project(Path(tmp) / "many", args.rows, args.batches)
            out["apply_many_batches_s"] = round(timed_load(root2, dbname), 2)
            files = sorted((root / "ledger" / "data").rglob("*.csv*"))
            out["source_mb"] = round(sum(f.stat().st_size for f in files) / 1e6, 1)
    finally:
        with db.owner_conn(autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
