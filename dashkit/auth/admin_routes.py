"""/api/admin/* 운용 API(contracts/auth.md "운용 API"). 운용자(admin·operator)만 쓴다(require_operator).

권한 검사는 여기(서버)에서 한다. 화면에서 버튼을 숨기는 것은 편의일 뿐이다.
- operator는 user 등급 계정만 다룬다. operator·admin 계정이 대상에 하나라도 있으면 요청 전체가 403 forbidden이고
  아무것도 바꾸지 않는다.
- 목록 응답은 {"items": [...], "total": n}이고 ?page=&size=&sort=를 받는다(sort 앞의 -는 내림차순).
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, StrictBool, StrictStr

from .. import config
from ..api.errors import ApiError
from . import emails, mailer, passwords, store
from .routes import Principal, check_new_email, client_ip, issue_token, require_operator

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


def manage_roles(actor: Principal) -> store.Roles:
    """저장 함수가 대상 행을 잠근 뒤 다시 볼 등급. admin은 모든 등급(검사 없음), operator는 user만."""
    return None if actor.role == "admin" else ("user",)


@contextmanager
def store_checks():
    """저장 함수가 잠금 안에서 다시 본 결과를 API 오류로 바꾼다(검사 뒤 동시 변경에 대비)."""
    try:
        yield
    except store.Forbidden:
        raise ApiError(403, "forbidden", "운용진은 일반사용자 계정만 다룰 수 있습니다") from None
    except store.InvalidStatus:
        raise ApiError(409, "invalid_status", "지금 계정 상태에서는 할 수 없습니다") from None
    except store.NotFound:
        raise ApiError(404, "unknown_user", "없는 사용자입니다") from None
    except store.LastAdmin:
        raise ApiError(409, "last_admin", "마지막 활성 admin입니다") from None


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
    with store_checks():
        added, removed = store.change_group_members(group_id, [u["id"] for u in targets_add],
                                                    [u["id"] for u in targets_remove], actor.id,
                                                    roles=manage_roles(actor))
    if added or removed:
        emails_of = {u["id"]: u["email"] for u in targets_add + targets_remove}
        audit(request, actor, "group.members", target_type="group", target_id=group_id, target_label=group["name"],
              detail={"added": [emails_of[i] for i in added], "removed": [emails_of[i] for i in removed]})
    return {"not_found": not_found}


# ---------------------------------------------------------------- 테스트 메일함

@router.get("/outbox")
def get_outbox(page_no: int = PageNo, size: int = PageSize, actor: Principal = Depends(require_operator)):
    """테스트 모드 전용(그 밖에는 404). operator에게는 받는 계정의 **지금** 등급이 user인 메일만 돌려준다.

    비밀번호 찾기는 로그인 없이 아무 주소로나 요청할 수 있으므로, 이 제한이 없으면 operator가 admin 주소로 찾기를
    요청하고 여기서 링크를 가져가 admin 계정을 차지할 수 있다(3절의 제한 우회).
    """
    from . import settings as auth_settings

    if auth_settings.get().mail_mode != "test":
        raise ApiError(404, "not_found", "테스트 메일함은 메일 테스트 모드에서만 있습니다")
    rows = store.outbox_list(None if actor.role == "admin" else "user")
    items = [{"id": r["id"], "to_email": r["to_email"], "kind": r["kind"], "link": r["link"],
              "created_at": r["created_at"], "role": r["role"]} for r in rows]
    start = (page_no - 1) * size
    return {"items": items[start:start + size], "total": len(items)}


# ---------------------------------------------------------------- 사용자


BULK_INVITE_MAX = 200


def _user_not_found() -> ApiError:
    return ApiError(404, "unknown_user", "없는 사용자입니다")


def _load_target(user_id: str) -> dict[str, Any]:
    user = store.user_detail(user_id)
    if user is None:
        raise _user_not_found()
    return user


def _public_user(u: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "email", "role", "status", "locked", "groups", "created_at", "login_count", "last_login_at", "source")
    return {k: u[k] for k in keys}


def _detail(u: dict[str, Any]) -> dict[str, Any]:
    return {**_public_user(u), "created_by": u.get("created_by"), "created_by_email": u.get("created_by_email"),
            "password_changed_at": u["password_changed_at"], "failed_count": u["failed_count"],
            "locked_until": u["locked_until"], "email_verified_at": u["email_verified_at"],
            "must_change_password": u["must_change_password"]}


def _check_groups(group_ids: list[str]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(group_ids))
    found = store.groups_by_ids(ids)
    if len(found) != len(ids):
        raise ApiError(404, "unknown_group", "없는 그룹이 들어 있습니다")
    return found


def _mail_unavailable() -> ApiError:
    return ApiError(503, "mail_unavailable", "메일을 보낼 수 없습니다(AUTH_MAIL_MODE=smtp는 구현되지 않음)")


def _ensure_mail() -> None:
    try:
        mailer.ensure_available()
    except mailer.MailUnavailable:
        raise _mail_unavailable() from None


def _send_invite(user_id: str, email: str, token: str | None = None) -> None:
    """초대 메일. token이 없으면 새로 발급한다(이전 초대 링크 무효)."""
    try:
        mailer.get().send(email, "invite", mailer.link("reset.html", token or issue_token(user_id, "invite")))
    except mailer.MailUnavailable:
        raise _mail_unavailable() from None


def _create_invited(email: str, role: str, actor: Principal, group_ids: list[str]) -> store.User:
    """계정·그룹·초대 토큰을 한 트랜잭션으로 만들고 초대 메일을 보낸다. store.Conflict: 같은 이메일."""
    from datetime import timedelta

    from . import sessions
    from .routes import TOKEN_TTL

    token = sessions.new_token()
    user = store.create_invited_user(email, role, actor.id, group_ids, sessions.token_hash(token),
                                     store.now() + timedelta(minutes=TOKEN_TTL["invite"]))
    _send_invite(user.id, email, token)
    return user


def _last_admin() -> ApiError:
    return ApiError(409, "last_admin", "마지막 활성 admin입니다")


def _protect_admins(actor: Principal, target: dict[str, Any], *, removes_admin: bool) -> None:
    """마지막 admin·자기 자신 보호(3절). removes_admin: 이 변경으로 target이 활성 admin이 아니게 되는지.

    마지막 활성 admin을 먼저 본다(활성 admin이 하나뿐이면 행위자는 그 자신이므로, 그 경우 사유는 last_admin이다).
    """
    if removes_admin and target["role"] == "admin" and target["status"] == "active":
        if store.active_admins() == [target["id"]]:
            raise _last_admin()
    if target["id"] == actor.id:
        raise ApiError(409, "self_target", "자기 자신에게는 할 수 없습니다")


@router.get("/users")
def list_users(page_no: int = PageNo, size: int = PageSize, sort: str | None = SortKey,
               _actor: Principal = Depends(require_operator)):
    items = [_public_user(u) for u in store.list_users()]
    return page(items, page_no, size, sort, {
        "email": lambda u: u["email"], "role": lambda u: ("admin", "operator", "user").index(u["role"]),
        "status": lambda u: u["status"], "created_at": lambda u: u["created_at"],
        "login_count": lambda u: u["login_count"], "last_login_at": lambda u: u["last_login_at"],
        "source": lambda u: u["source"], "groups": lambda u: len(u["groups"]),
    }, "email")


@router.get("/users/{user_id}")
def get_user(user_id: str, _actor: Principal = Depends(require_operator)):
    return _detail(_load_target(user_id))


class UserCreate(BaseModel):
    email: StrictStr = Field(max_length=320)
    role: StrictStr = "user"
    group_ids: list[StrictStr] = Field(default_factory=list, max_length=200)


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, actor: Principal = Depends(require_operator)):
    """임의 가입·초대. 상태는 invited이고 비밀번호는 없다. 초대 메일(72시간)의 링크에서 비밀번호를 정하면 active."""
    if body.role not in ("admin", "operator", "user"):
        raise ApiError(422, "invalid_request", "등급은 admin · operator · user 중 하나입니다")
    if body.role != "user" and actor.role != "admin":
        raise ApiError(403, "forbidden", "운용진은 일반사용자만 만들 수 있습니다")
    email = check_new_email(body.email)
    groups = _check_groups(body.group_ids)
    _ensure_mail()
    try:
        user = _create_invited(email, body.role, actor, [g["id"] for g in groups])
    except store.Conflict:
        raise ApiError(409, "user_exists", "이미 있는 이메일입니다") from None
    audit(request, actor, "user.create", target_type="user", target_id=user.id, target_label=email,
          detail={"role": body.role, "groups": [g["name"] for g in groups]})
    return _detail(_load_target(user.id))


class BulkInvite(BaseModel):
    emails: list[StrictStr] = Field(max_length=1000)
    group_ids: list[StrictStr] = Field(default_factory=list, max_length=200)


@router.post("/users/bulk-invite")
def bulk_invite(body: BulkInvite, request: Request, actor: Principal = Depends(require_operator)):
    """주소마다 created · exists · invalid_email · email_domain_not_allowed. 일부가 실패해도 나머지는 만든다. 등급은 user."""
    raw = [e.strip() for e in body.emails if e.strip()]
    seen: dict[str, None] = {}
    for e in raw:
        seen.setdefault(e.lower(), None)
    if not seen:
        raise ApiError(422, "invalid_request", "이메일이 없습니다")
    if len(seen) > BULK_INVITE_MAX:
        raise ApiError(422, "too_many_emails", f"한 번에 {BULK_INVITE_MAX}개까지 초대할 수 있습니다")
    groups = _check_groups(body.group_ids)
    _ensure_mail()
    results = []
    for e in seen:
        try:
            email = check_new_email(e)
        except ApiError as exc:
            results.append({"email": e, "result": exc.detail})
            continue
        try:
            user = _create_invited(email, "user", actor, [g["id"] for g in groups])
        except store.Conflict:
            results.append({"email": email, "result": "exists"})
            continue
        audit(request, actor, "user.create", target_type="user", target_id=user.id, target_label=email,
              detail={"role": "user", "groups": [g["name"] for g in groups], "bulk": True})
        results.append({"email": email, "result": "created"})
    return {"results": results}


class UserPatch(BaseModel):
    role: StrictStr | None = None
    status: StrictStr | None = None
    group_ids: list[StrictStr] | None = Field(None, max_length=200)


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request, actor: Principal = Depends(require_operator)):
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    if body.role is not None and body.role != target["role"]:
        if actor.role != "admin":
            raise ApiError(403, "forbidden", "등급은 admin만 바꿀 수 있습니다")
        if body.role not in ("admin", "operator", "user"):
            raise ApiError(422, "invalid_request", "등급은 admin · operator · user 중 하나입니다")
        _protect_admins(actor, target, removes_admin=target["role"] == "admin")
    if body.status is not None and body.status != target["status"]:
        if body.status not in ("active", "suspended") or target["status"] not in ("active", "suspended"):
            raise ApiError(409, "invalid_status", "정지와 해제는 활성·정지 계정 사이에서만 됩니다")
        if body.status == "suspended":
            _protect_admins(actor, target, removes_admin=True)
    groups = _check_groups(body.group_ids) if body.group_ids is not None else None

    if body.role is not None and body.role != target["role"]:
        with store_checks():   # 마지막 활성 admin인지는 같은 트랜잭션에서 활성 admin 행을 잠그고 다시 본다
            store.set_role(user_id, body.role, guard_admin=target["role"] == "admin")
        audit(request, actor, "user.role", target_type="user", target_id=user_id, target_label=target["email"],
              detail={"from": target["role"], "to": body.role})
    if body.status is not None and body.status != target["status"]:
        with store_checks():
            store.set_status(user_id, body.status, guard_admin=body.status == "suspended" and target["role"] == "admin",
                             roles=manage_roles(actor))
        audit(request, actor, "user.suspend" if body.status == "suspended" else "user.unsuspend",
              target_type="user", target_id=user_id, target_label=target["email"])
    if groups is not None:
        with store_checks():
            added, removed = store.set_user_groups(user_id, [g["id"] for g in groups], actor.id,
                                                   roles=manage_roles(actor))
        if added or removed:
            names = {g["id"]: g["name"] for g in groups + target["groups"]}
            audit(request, actor, "user.groups", target_type="user", target_id=user_id, target_label=target["email"],
                  detail={"added": [names.get(i, i) for i in added], "removed": [names.get(i, i) for i in removed]})
    return _detail(_load_target(user_id))


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, request: Request, actor: Principal = Depends(require_operator)):
    """계정 행, 그룹 구성원 연결, 세션, 토큰, 테스트 메일을 지운다(FK). 접속 통계의 행은 사용자 연결만 끊긴다."""
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    _protect_admins(actor, target, removes_admin=True)
    with store_checks():
        deleted = store.delete_user(user_id, guard_admin=target["role"] == "admin", roles=manage_roles(actor))
    if not deleted:
        raise _user_not_found()
    audit(request, actor, "user.delete", target_type="user", target_id=user_id, target_label=target["email"],
          detail={"role": target["role"], "status": target["status"]})
    return Response(status_code=204)


class ResetPassword(BaseModel):
    method: StrictStr


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: str, body: ResetPassword, request: Request,
                        actor: Principal = Depends(require_operator)):
    """mail: 재설정 메일(5-3과 같다) → 204. temporary: 임시 비밀번호(16자)를 응답에 한 번만 → 200 {password}.
    어느 쪽이든 그 사용자의 세션을 즉시 지운다. 임시 비밀번호는 감사 로그·서버 로그에 남기지 않는다."""
    if body.method not in ("mail", "temporary"):
        raise ApiError(422, "invalid_request", "method는 mail 또는 temporary입니다")
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    if body.method == "mail":
        if target["status"] != "active":
            raise ApiError(409, "invalid_status", "활성 계정만 재설정 메일을 받습니다")
        _ensure_mail()
        # 토큰 발급과 세션 삭제를 한 트랜잭션으로, 대상 행을 잠근 뒤 지금 등급·상태를 다시 보고 한다
        with store_checks():
            token = issue_token(user_id, "reset_password", roles=manage_roles(actor), statuses=("active",),
                                end_sessions=True)
        try:
            mailer.get().send(target["email"], "reset_password", mailer.link("reset.html", token))
        except mailer.MailUnavailable:
            raise _mail_unavailable() from None
        audit(request, actor, "user.reset_password", target_type="user", target_id=user_id,
              target_label=target["email"], detail={"method": "mail"})
        return Response(status_code=204)
    if target["status"] not in ("active", "suspended"):
        raise ApiError(409, "invalid_status", "초대·인증 전 계정은 임시 비밀번호를 받을 수 없습니다")
    temporary = passwords.temporary_password()
    # 임시 비밀번호와 세션 삭제를 한 트랜잭션으로. 대상 행을 잠근 뒤 지금 등급·상태를 다시 본다
    # (검사 뒤에 대상이 승급됐으면 operator의 요청은 403이고 아무것도 바뀌지 않는다)
    with store_checks():
        store.set_password(user_id, passwords.hash_password(temporary), must_change=True, end_sessions=True,
                           roles=manage_roles(actor), statuses=("active", "suspended"))
    audit(request, actor, "user.reset_password", target_type="user", target_id=user_id,
          target_label=target["email"], detail={"method": "temporary"})
    return {"password": temporary}


@router.post("/users/{user_id}/resend-invite", status_code=204)
def resend_invite(user_id: str, request: Request, actor: Principal = Depends(require_operator)):
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    if target["status"] != "invited":
        raise ApiError(409, "invalid_status", "초대 상태인 계정만 다시 보낼 수 있습니다")
    _ensure_mail()
    with store_checks():
        token = issue_token(user_id, "invite", roles=manage_roles(actor), statuses=("invited",))
    _send_invite(user_id, target["email"], token)
    audit(request, actor, "user.invite_resent", target_type="user", target_id=user_id, target_label=target["email"])
    return Response(status_code=204)


@router.post("/users/{user_id}/unlock", status_code=204)
def unlock_user(user_id: str, request: Request, actor: Principal = Depends(require_operator)):
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    with store_checks():
        store.unlock_user(user_id, roles=manage_roles(actor))
    audit(request, actor, "user.unlock", target_type="user", target_id=user_id, target_label=target["email"])
    return Response(status_code=204)


@router.post("/users/{user_id}/logout-all", status_code=204)
def logout_all(user_id: str, request: Request, actor: Principal = Depends(require_operator)):
    target = _load_target(user_id)
    ensure_can_manage(actor, [target])
    with store_checks():
        n = store.delete_user_sessions(user_id, roles=manage_roles(actor))
    audit(request, actor, "user.logout_all", target_type="user", target_id=user_id, target_label=target["email"],
          detail={"sessions": n})
    return Response(status_code=204)


class BulkGroups(BaseModel):
    user_ids: list[StrictStr] = Field(max_length=1000)
    add: list[StrictStr] = Field(default_factory=list, max_length=200)
    remove: list[StrictStr] = Field(default_factory=list, max_length=200)


@router.post("/users/bulk-groups", status_code=204)
def bulk_groups(body: BulkGroups, request: Request, actor: Principal = Depends(require_operator)):
    """고른 사용자들을 그룹에 더하고 뺀다. 없는 사용자·그룹이 있거나 operator가 user 아닌 계정을 고르면 아무것도 바꾸지 않는다."""
    ids = list(dict.fromkeys(body.user_ids))
    targets = store.users_by_ids(ids)
    if len(targets) != len(ids):
        raise _user_not_found()
    ensure_can_manage(actor, targets)
    add = _check_groups(body.add)
    remove = _check_groups(body.remove)
    if {g["id"] for g in add} & {g["id"] for g in remove}:
        raise ApiError(422, "invalid_request", "같은 그룹을 더하고 뺄 수 없습니다")
    for g in add:
        with store_checks():
            added, _ = store.change_group_members(g["id"], [t["id"] for t in targets], [], actor.id,
                                                  roles=manage_roles(actor))
        if added:
            emails_of = {t["id"]: t["email"] for t in targets}
            audit(request, actor, "group.members", target_type="group", target_id=g["id"], target_label=g["name"],
                  detail={"added": [emails_of[i] for i in added], "removed": [], "bulk": True})
    for g in remove:
        with store_checks():
            _, removed = store.change_group_members(g["id"], [], [t["id"] for t in targets], actor.id,
                                                    roles=manage_roles(actor))
        if removed:
            emails_of = {t["id"]: t["email"] for t in targets}
            audit(request, actor, "group.members", target_type="group", target_id=g["id"], target_label=g["name"],
                  detail={"added": [], "removed": [emails_of[i] for i in removed], "bulk": True})
    return Response(status_code=204)
