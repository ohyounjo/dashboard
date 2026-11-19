# sync가 만든 객체의 export (Superset 6.1.0)

`make apply`(devtracker)로 만든 대시보드 3개를 `GET /api/v1/dashboard/export/`로 내보낸 원본이다(2026-11-20).
위 폴더의 probe export에는 네이티브 필터가 있는 대시보드가 없어 `metadata` 키가 빠져 있다. 이 export가 그 키를 채운다.
`tests/test_superset_keys.py`의 키 대조 기준으로 쓴다. 손으로 고치지 않는다. 다시 받는 방법은 `contracts/superset-upgrade.md` 3번.

`import-only-keys.yaml`은 export가 아니다. export에는 나오지 않지만 import가 받는 키(표식 `is_managed_externally`, `external_url`) 가운데 번들이 쓰는 것을 근거와 함께 적은 목록이고, 같은 테스트가 export 키와 합쳐 쓴다. Superset을 올릴 때 파일 머리말의 방법으로 다시 확인한다.
