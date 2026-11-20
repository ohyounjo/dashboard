"""/api/admin/* 운용 API(contracts/auth.md "운용 API"). 운용자(admin·operator)만 쓴다(require_operator).

권한 검사는 여기(서버)에서 한다. 화면에서 버튼을 숨기는 것은 편의일 뿐이다.
- operator는 user 등급 계정만 다룬다. operator·admin 계정이 대상에 하나라도 있으면 요청 전체가 403 forbidden이고
  아무것도 바꾸지 않는다.
- 목록 응답은 {"items": [...], "total": n}이고 ?page=&size=&sort=를 받는다(sort 앞의 -는 내림차순).
"""
from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, StrictBool, StrictStr

from .. import config
from ..api.errors import ApiError
from . import emails, store
from .routes import Principal, client_ip, require_operator

router = APIRouter(prefix="/api/admin")

_project_provider: Callable[[], config.Project] | None = None
_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TAB_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
MAX_PAGE_SIZE = 200


def set_project_provider(fn: Callable[[], config.Project]) -> None:
    """backend(app.py)가 현재 프로젝트를 돌려주는 함수를 넣는다."""
    global _project_provider
    _project_provider = fn


def current_project() -> config.Project:
    if _project_provider is None:
        raise RuntimeError("현재 프로젝트를 알 수 없습니다")
    return _project_provider()


def audit(request: Request | None, actor: Principal, action: str, **kw: Any) -> None:
    store.record_audit(action, actor_id=actor.id, actor_email=actor.email,
                       ip=client_ip(request) if request is not None else None, **kw)


def page(items: list[dict[str, Any]], page_no: int, size: int, sort: str | None,
         sort_keys: dict[str, Callable[[dict[str, Any]], Any]], default: str) -> dict[str, Any]:
    """정렬과 페이지 나눔. 모르는 sort는 422 invalid_request. None 값은 오름차순에서 뒤로 간다."""
    key = sort or default
    desc = key.startswith("-")
    name = key.lstrip("-")
    if name not in sort_keys:
        raise ApiError(422, "invalid_request", f"정렬할 수 없는 열입니다: {name}")
    fn = sort_keys[name]
    present = [i for i in items if fn(i) is not None]
    missing = [i for i in items if fn(i) is None]
    present.sort(key=fn, reverse=desc)
    ordered = present + missing
    start = (page_no - 1) * size
    return {"items": ordered[start:start + size], "total": len(items)}


PageNo = Query(1, ge=1, alias="page")
PageSize = Query(50, ge=1, le=MAX_PAGE_SIZE, alias="size")
SortKey = Query(None, alias="sort", max_length=40)


def ensure_can_manage(actor: Principal, targets: list[dict[str, Any]]) -> None:
    """operator는 user 등급만 다룬다(3절)."""
    if actor.role != "admin" and any(t["role"] != "user" for t in targets):
        raise ApiError(403, "forbidden", "운용진은 일반사용자 계정만 다룰 수 있습니다")


# ---------------------------------------------------------------- 탭

def _tab_labels(project: config.Project) -> dict[str, str]:
    return {t["key"]: t["label"] for t in project.dashboard["tabs"]}


def _other_project_tabs(name: str) -> dict[str, str] | None:
    """다른 프로젝트의 탭(키 → 라벨). 폴더가 없거나 설정이 유효하지 않으면 None."""
    if not _PROJECT_RE.fullmatch(name):
        return None
    try:
        return _tab_labels(config.load_project(name, config.default_projects_dir()))
    except (config.ConfigError, OSError):
        return None


def _tab_entries(project: config.Project, refs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    labels = _tab_labels(project)
    others: dict[str, dict[str, str] | None] = {}
    out = []
    for proj, key in refs:
        if proj == project.name:
            label = labels.get(key)
        else:
            if proj not in others:
                others[proj] = _other_project_tabs(proj)
            label = (others[proj] or {}).get(key)
        out.append({"project": proj, "tab_key": key, "label": label, "current": proj == project.name,
                    "exists": label is not None})
    return out


@router.get("/tabs")
def get_tabs(_actor: Principal = Depends(require_operator)):
    project = current_project()
    keys = {t["key"] for t in project.dashboard["tabs"]}
    others = [r for r in store.group_tab_refs() if r["project"] != project.name or r["tab_key"] not in keys]
    entries = _tab_entries(project, [(r["project"], r["tab_key"]) for r in others])
    for e, r in zip(entries, others):
        e["groups"] = r["groups"]
    return {
        "project": project.name,
        "tabs": [{"key": t["key"], "label": t["label"], "icon": t.get("icon")} for t in project.dashboard["tabs"]],
        "others": entries,
    }


# ---------------------------------------------------------------- 그룹

def _group_not_found() -> ApiError:
    return ApiError(404, "unknown_group", "없는 그룹입니다")


def _group_detail(group: dict[str, Any], project: config.Project) -> dict[str, Any]:
    tabs = _tab_entries(project, store.group_tabs(group["id"]))
    members = store.group_members(group["id"])
    return {**group, "tabs": tabs, "members": members}


def _list_item(group: dict[str, Any], project: config.Project) -> dict[str, Any]:
    keys = {t["key"] for t in project.dashboard["tabs"]}
    tab_keys = group.pop("tab_keys")
    return {**group, "tab_count": len([k for k in tab_keys if k in keys])}


@router.get("/groups")
def list_groups(page_no: int = PageNo, size: int = PageSize, sort: str | None = SortKey,
                _actor: Principal = Depends(require_operator)):
    project = current_project()
    items = [_list_item(g, project) for g in store.list_groups(project.name)]
    return page(items, page_no, size, sort, {
        "name": lambda g: g["name"].lower(), "member_count": lambda g: g["member_count"],
        "tab_count": lambda g: g["tab_count"], "is_default": lambda g: g["is_default"],
        "created_at": lambda g: g["created_at"], "updated_at": lambda g: g["updated_at"],
    }, "name")


class GroupCreate(BaseModel):
    name: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr = Field("", max_length=500)
    is_default: StrictBool = False


class GroupPatch(BaseModel):
    name: StrictStr | None = Field(None, min_length=1, max_length=100)
    description: StrictStr | None = Field(None, max_length=500)
    is_default: StrictBool | None = None


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ApiError(422, "invalid_request", "그룹 이름이 비어 있습니다")
    return name


@router.post("/groups", status_code=201)
def create_group(body: GroupCreate, request: Request, actor: Principal = Depends(require_operator)):
    name = _clean_name(body.name)
    try:
        group = store.create_group(name, body.description.strip(), body.is_default)
    except store.Conflict:
        raise ApiError(409, "group_exists", "같은 이름의 그룹이 있습니다") from None
    audit(request, actor, "group.create", target_type="group", target_id=group["id"], target_label=name,
          detail={"description": group["description"], "is_default": group["is_default"]})
    return _group_detail(group, current_project())


@router.get("/groups/{group_id}")
def get_group(group_id: str, _actor: Principal = Depends(require_operator)):
    group = store.group_by_id(group_id)
    if group is None:
        raise _group_not_found()
    return _group_detail(group, current_project())


@router.patch("/groups/{group_id}")
def patch_group(group_id: str, body: GroupPatch, request: Request, actor: Principal = Depends(require_operator)):
    before = store.group_by_id(group_id)
    if before is None:
        raise _group_not_found()
    name = _clean_name(body.name) if body.name is not None else None
    description = body.description.strip() if body.description is not None else None
    try:
        group = store.update_group(group_id, name=name, description=description, is_default=body.is_default)
    except store.Conflict:
        raise ApiError(409, "group_exists", "같은 이름의 그룹이 있습니다") from None
    if group is None:
        raise _group_not_found()
    changes = {k: {"from": before[k], "to": group[k]} for k in ("name", "description", "is_default") if before[k] != group[k]}
    if changes:
        audit(request, actor, "group.update", target_type="group", target_id=group_id, target_label=group["name"],
              detail=changes)
    return _group_detail(group, current_project())


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: str, request: Request, actor: Principal = Depends(require_operator)):
    group = store.group_by_id(group_id)
    if group is None:
        raise _group_not_found()
    members = len(store.group_members(group_id))
    if store.delete_group(group_id) is None:
        raise _group_not_found()
    audit(request, actor, "group.delete", target_type="group", target_id=group_id, target_label=group["name"],
          detail={"members": members})
    return Response(status_code=204)


class GroupTabsPut(BaseModel):
    project: StrictStr = Field(min_length=1, max_length=100)
    tab_keys: list[StrictStr] = Field(max_length=500)


@router.put("/groups/{group_id}/tabs")
def put_group_tabs(group_id: str, body: GroupTabsPut, request: Request, actor: Principal = Depends(require_operator)):
    group = store.group_by_id(group_id)
    if group is None:
        raise _group_not_found()
    project = current_project()
    keys = list(dict.fromkeys(body.tab_keys))
    if body.project == project.name:
        valid: dict[str, str] | None = _tab_labels(project)
    else:
        valid = _other_project_tabs(body.project)
    # 설정에 없는 키는 받지 않는다. 설정에 없는 탭의 기존 기록은 건드리지 않는다(하나씩 지우는 DELETE를 쓴다)
    unknown = [k for k in keys if not _TAB_KEY_RE.fullmatch(k) or valid is None or k not in valid]
    if unknown:
        raise ApiError(422, "unknown_tab", f"설정에 없는 탭입니다: {', '.join(unknown[:5])}")
    added, removed = store.set_group_tabs(group_id, body.project, keys, set(valid or {}))
    if added or removed:
        audit(request, actor, "group.tabs", target_type="group", target_id=group_id, target_label=group["name"],
              detail={"project": body.project, "added": added, "removed": removed})
    return _group_detail(store.group_by_id(group_id) or group, project)


@router.delete("/groups/{group_id}/tabs/{project_name}/{tab_key}")
def delete_group_tab(group_id: str, project_name: str, tab_key: str, request: Request,
                     actor: Principal = Depends(require_operator)):
    """허용 탭 기록 하나를 지운다(설정에서 없어진 탭·다른 프로젝트의 탭 정리). 없는 기록이면 아무것도 하지 않는다."""
    group = store.group_by_id(group_id)
    if group is None:
        raise _group_not_found()
    if store.remove_group_tab(group_id, project_name, tab_key):
        audit(request, actor, "group.tabs", target_type="group", target_id=group_id, target_label=group["name"],
              detail={"project": project_name, "added": [], "removed": [tab_key]})
    return _group_detail(store.group_by_id(group_id) or group, current_project())


class GroupMembersPut(BaseModel):
    add: list[StrictStr] = Field(default_factory=list, max_length=500)
    remove: list[StrictStr] = Field(default_factory=list, max_length=500)


@router.put("/groups/{group_id}/members")
def put_group_members(group_id: str, body: GroupMembersPut, request: Request,
                      actor: Principal = Depends(require_operator)):
    """add는 이메일 또는 id, remove는 id. 찾지 못한 것은 not_found로 돌려준다."""
    group = store.group_by_id(group_id)
    if group is None:
        raise _group_not_found()
    add_refs = list(dict.fromkeys(a.strip() for a in body.add if a.strip()))
    remove_refs = list(dict.fromkeys(r.strip() for r in body.remove if r.strip()))
    add_emails = [emails.normalize(a) for a in add_refs if "@" in a]
    add_ids = [a for a in add_refs if "@" not in a]
    found_add = store.users_by_refs(add_emails, add_ids)
    found_remove = store.users_by_refs([], remove_refs)
    by_email = {u["email"]: u for u in found_add}
    by_id = {u["id"]: u for u in found_add + found_remove}
    not_found = [a for a in add_refs if ("@" in a and emails.normalize(a) not in by_email)
                 or ("@" not in a and a not in by_id)]
    not_found += [r for r in remove_refs if r not in by_id]
    targets_add = list({u["id"]: u for u in found_add}.values())
    targets_remove = [by_id[r] for r in remove_refs if r in by_id]
    ensure_can_manage(actor, targets_add + targets_remove)
    added, removed = store.change_group_members(group_id, [u["id"] for u in targets_add],
                                                [u["id"] for u in targets_remove], actor.id)
    if added or removed:
        emails_of = {u["id"]: u["email"] for u in targets_add + targets_remove}
        audit(request, actor, "group.members", target_type="group", target_id=group_id, target_label=group["name"],
              detail={"added": [emails_of[i] for i in added], "removed": [emails_of[i] for i in removed]})
    return {"not_found": not_found}
