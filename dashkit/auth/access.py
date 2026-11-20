"""탭 판정(contracts/auth.md "접근 제어"). 접근 단위는 dashboard.yaml의 탭(tabs[].key)이다.

- admin·operator는 현재 프로젝트의 모든 탭을 본다.
- user는 속한 그룹들이 (현재 프로젝트 이름, 탭 키)로 허용한 탭의 합집합을 본다. 설정에 없는 탭 키는 무시한다.
- 판정은 요청마다 DB에서 읽는다(그룹을 고치면 다음 요청부터 반영된다).
"""
from __future__ import annotations

from .. import config
from . import store
from .routes import Principal


def allowed_tab_keys(user: Principal, project: config.Project) -> set[str]:
    """그 사용자가 볼 수 있는 현재 프로젝트의 탭 키."""
    keys = {tab["key"] for tab in project.dashboard["tabs"]}
    if user.is_operator:
        return keys
    return keys & store.user_tab_keys(user.id, project.name)


def allowed_dashboards(user: Principal, project: config.Project) -> set[str]:
    """guest token을 받을 수 있는 대시보드 키.

    운용자는 설정의 모든 대시보드(지금과 같다), user는 허용된 탭의 superset 섹션이 참조하는 대시보드만.
    같은 대시보드를 두 탭이 쓰면 한 탭만 허용돼도 토큰이 나간다(탭이 아니라 대시보드에 대한 토큰이다).
    """
    if user.is_operator:
        return set(project.dashboard["dashboards"])
    return config.superset_dashboards(project, allowed_tab_keys(user, project))


def allowed_datasets(user: Principal, project: config.Project) -> set[str]:
    """/api/data로 읽을 수 있는 데이터셋: 허용된 탭의 위젯 섹션이 참조하는 것."""
    if user.is_operator:
        return config.widget_datasets(project)
    return config.widget_datasets(project, allowed_tab_keys(user, project))
