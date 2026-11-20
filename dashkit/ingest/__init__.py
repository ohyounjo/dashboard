"""데이터 추가 적재(설계서 reference/data-design.md 5절, 계약 contracts/ingest.md).

로더·관리 서비스·CLI·Studio가 함께 쓰는 한 벌의 로직이다.

모듈:
- intake.py     접수: 파서(PARSERS), 머리글 맞추기, 한 줄씩 검증
- normalize.py  값 다듬기(NORMALIZERS)
- batches.py    배치 폴더, 원본 파일 목록, 내용 해시, 정규형 쓰기
- apply.py      stage_batch(임시 테이블) → apply_batch(반영), run_checks. load와 추가 적재가 같은 함수를 쓴다
- policies.py   충돌 정책(POLICIES)
- ledger.py     배치 기록(dashkit_meta.batch)과 적재 스냅샷
- pending.py    묶음 기록(data/_pending/)
- recover.py    끊김 복구(RECOVERERS). 파일을 지워도 되는지를 판정하는 유일한 곳
- operation.py  폴더를 바꾸는 작업의 공통 순서(잠금 → 확인 → 복구 → 대조 → 작업 → 규칙 검사 → 마무리)
- add.py        데이터 더하기 ingest()
"""
from .add import Item, ingest
from .operation import IngestError

__all__ = ["Item", "IngestError", "ingest"]
