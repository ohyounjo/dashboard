"""배치 폴더와 원본 데이터 파일(설계서 4-2, contracts/ingest.md "배치 폴더").

- `replace` 테이블의 원본은 `data/<테이블>.csv` 하나다.
- `append` 테이블의 원본은 `data/<테이블>.csv`(있으면 첫 배치) + `data/<테이블>/`의 배치 파일들이다.
  배치 파일의 이름은 `<번호>[-<덧붙임>].csv` 또는 `….csv.gz`이고 **번호순**(숫자로 비교)으로 읽는다.
  `_`나 `.`으로 시작하는 이름은 읽지 않는다(`_removed/`, `_merged/`, `_pending/`). 그 밖의 이름이나 같은 번호가
  둘이면 오류다.
- 파일의 **내용 해시**는 압축을 푼 내용의 SHA-256이다(`.csv`와 `.csv.gz`가 같은 내용이면 해시가 같다).
  크기와 수정 시각이 지난번과 같으면 다시 구하지 않는다(프로세스 안의 캐시).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

BATCH_RE = re.compile(r"(\d{1,18})(?:-([^/\\]*))?\.csv(\.gz)?")
STAMP_RE = re.compile(r"(\d{8}T\d{4}(?:\d{2})?Z)")
REMOVED_DIR = "_removed"
MERGED_DIR = "_merged"
PENDING_DIR = "_pending"
INCOMING_DIR = "incoming"          # _pending/incoming: 확정 전의 정규형 파일(같은 파일 시스템이라 이름 바꾸기가 원자적이다)
NUMBER_WIDTH = 6
STAMP_FORMAT = "%Y%m%dT%H%MZ"
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class SourceFile:
    """테이블 하나의 원본 데이터 파일 하나. name은 배치 기록(dashkit_meta.batch)의 file 값이다."""
    table: str
    name: str                 # "<테이블>.csv"(첫 파일) 또는 배치 폴더 안의 이름
    path: Path
    number: int | None        # 배치 번호. data/<테이블>.csv는 None(언제나 맨 앞)

    @property
    def added_at(self) -> datetime | None:
        return stamp_of(self.name)


def stamp_of(name: str) -> datetime | None:
    """배치 이름의 UTC 시각(`<번호>-<YYYYMMDDTHHMMZ>…`). 없으면 None."""
    m = BATCH_RE.fullmatch(name)
    if not m or not m.group(2):
        return None
    s = STAMP_RE.match(m.group(2))
    if not s:
        return None
    text = s.group(1)
    fmt = "%Y%m%dT%H%M%SZ" if len(text) == 16 else STAMP_FORMAT
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def table_dir(data_dir: Path, table: str) -> Path:
    return data_dir / table


def first_file(data_dir: Path, table: str) -> Path:
    return data_dir / f"{table}.csv"


def list_batches(data_dir: Path, table: str) -> tuple[list[SourceFile], list[str]]:
    """배치 폴더 바로 아래의 배치 파일(번호순)과 이름 오류. 폴더가 없으면 ([], [])."""
    folder = table_dir(data_dir, table)
    if not folder.is_dir():
        return [], []
    found: list[SourceFile] = []
    errors: list[str] = []
    by_number: dict[int, str] = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name.startswith(("_", ".")):
            continue
        m = BATCH_RE.fullmatch(name)
        if not m or not entry.is_file() or entry.is_symlink():
            errors.append(f"data/{table}/{name}:1:-: 배치 파일 이름 규칙(<번호>[-<덧붙임>].csv 또는 .csv.gz)에 맞지 않음")
            continue
        number = int(m.group(1))
        if number in by_number:
            errors.append(f"data/{table}/{name}:1:-: 배치 번호 {number}가 '{by_number[number]}'와 겹침")
            continue
        by_number[number] = name
        found.append(SourceFile(table, name, entry, number))
    found.sort(key=lambda f: f.number or 0)
    return found, errors


def sources(data_dir: Path, table: str, mode: str) -> tuple[list[SourceFile], list[str]]:
    """테이블의 원본 데이터 파일(읽는 순서)과 이름 오류. replace는 data/<테이블>.csv 하나다(없으면 빈 목록)."""
    first = first_file(data_dir, table)
    out = [SourceFile(table, first.name, first, None)] if first.is_file() else []
    if mode != "append":
        return out, []
    batches, errors = list_batches(data_dir, table)
    return out + batches, errors


def all_numbers(data_dir: Path, table: str) -> list[int]:
    """폴더와 _removed/·_merged/(아래 폴더 포함)에 있는 모든 배치 번호. 다음 번호를 정할 때 쓴다."""
    folder = table_dir(data_dir, table)
    numbers: list[int] = []
    if not folder.is_dir():
        return numbers
    for root in (folder, folder / REMOVED_DIR, folder / MERGED_DIR):
        if not root.is_dir():
            continue
        walk = root.rglob("*") if root != folder else root.iterdir()
        for p in walk:
            m = BATCH_RE.fullmatch(p.name)
            if m and p.is_file():
                numbers.append(int(m.group(1)))
    return numbers


def next_name(data_dir: Path, table: str, now: datetime | None = None) -> str:
    """`<6자리 번호>-<UTC 시각>.csv.gz`. 번호는 지금까지 쓴 가장 큰 번호 + 1이다(되돌리고 합친 배치 포함)."""
    number = max(all_numbers(data_dir, table), default=0) + 1
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(STAMP_FORMAT)
    return f"{number:0{NUMBER_WIDTH}d}-{stamp}.csv.gz"


# ---------------------------------------------------------------- 읽기와 해시

class HashingReader(io.RawIOBase):
    """읽은 바이트의 SHA-256을 함께 구한다(압축을 푼 내용을 한 번만 읽기 위해)."""

    def __init__(self, raw: IO[bytes]):
        self._raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:   # type: ignore[override]
        data = self._raw.read(len(b))
        n = len(data)
        b[:n] = data
        self.digest.update(data)
        self.size += n
        return n

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def open_binary(path: Path) -> IO[bytes]:
    """압축을 푼 내용의 바이너리 스트림(.gz면 풀면서 읽는다)."""
    if path.name.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


_hash_cache: dict[str, tuple[int, int, str]] = {}
_hash_lock = threading.Lock()


def content_hash(path: Path) -> str:
    """압축을 푼 내용의 SHA-256. 크기·수정 시각이 지난번과 같으면 캐시를 쓴다."""
    st = path.stat()
    key = str(path)
    with _hash_lock:
        cached = _hash_cache.get(key)
    if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached[2]
    digest = hashlib.sha256()
    with open_binary(path) as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _hash_lock:
        _hash_cache[key] = (st.st_size, st.st_mtime_ns, value)
    return value


def remember_hash(path: Path, value: str) -> None:
    """이미 아는 해시를 캐시에 넣는다(방금 쓴 파일을 다시 읽지 않게)."""
    st = path.stat()
    with _hash_lock:
        _hash_cache[str(path)] = (st.st_size, st.st_mtime_ns, value)


# ---------------------------------------------------------------- 정규형 쓰기

class CanonicalWriter:
    """정규형 배치 파일(gzip. 머리말에 시각·이름을 넣지 않아 같은 내용이면 같은 바이트)과 내용 해시.

    CSV는 쉼표 구분, 줄 끝 `\n`, 필요할 때만 따옴표(csv.QUOTE_MINIMAL)다.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "wb")
        self._gz = gzip.GzipFile(filename="", mode="wb", fileobj=self._file, mtime=0, compresslevel=6)
        self._line = io.StringIO()
        self._writer = csv.writer(self._line, lineterminator="\n")
        self.digest = hashlib.sha256()
        self.rows = 0

    def _write(self, values: list[str]) -> None:
        self._line.seek(0)
        self._line.truncate()
        self._writer.writerow(values)
        data = self._line.getvalue().encode("utf-8")
        self.digest.update(data)
        self._gz.write(data)

    def header(self, columns: list[str]) -> None:
        self._write(columns)

    def row(self, values: list[str]) -> None:
        self._write(values)
        self.rows += 1

    def close(self) -> str:
        """파일을 닫고 내용 해시를 돌려준다."""
        self._gz.close()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        return self.digest.hexdigest()

    def abort(self) -> None:
        try:
            self._gz.close()
            self._file.close()
        except Exception:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


def rewrite_without(path: Path, written: Any, dropped: set[int], out: Path) -> str:
    """정규형 파일에서 dropped 위치의 행을 뺀 새 정규형 파일(out)을 쓰고 내용 해시를 돌려준다.

    written[i]는 파일의 i번째 데이터 행의 원래 위치다(stage_batch가 적는다). 오류 행을 빼고 넣을 때 잠금 안에서
    참조·새 행의 필수 열 오류로 뺀 행을 배치 파일에서도 빼는 데 쓴다(배치 파일에는 들어간 행만 있다).
    """
    writer = CanonicalWriter(out)
    try:
        with open_binary(path) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8", newline=""))
            writer.header(next(reader))
            for i, row in enumerate(reader):
                if written[i] not in dropped:
                    writer.row(row)
        return writer.close()
    except BaseException:
        writer.abort()
        raise
