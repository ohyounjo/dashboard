# sync가 만든 객체의 export (Superset 6.1.0)

`make apply`(devtracker)로 만든 대시보드 3개를 `GET /api/v1/dashboard/export/`로 내보낸 원본이다(2026-11-20).
위 폴더의 probe export에는 네이티브 필터가 있는 대시보드가 없어 `metadata` 키가 빠져 있다. 이 export가 그 키를 채운다.
`tests/test_superset_keys.py`의 키 대조 기준으로 쓴다. 손으로 고치지 않는다. 다시 받는 방법은 `contracts/superset-upgrade.md` 3번.
