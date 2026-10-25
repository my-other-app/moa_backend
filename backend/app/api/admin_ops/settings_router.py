"""The Settings page: runtime platform settings (app.api.admin_ops.runtime),
the secrets' health, and what changed. Owners change them (MANAGE_ADMINS:
the owner-only capability); every admin may read them."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.admin_ops import runtime, two_factor
from app.api.admin_ops.models import PlatformSettings
from app.api.audit import service as audit
from app.api.audit.models import AdminAuditLogs
from app.api.users.models import Users
from app.core.auth.dependencies import AdminAuth, DependsAuth
from app.core.auth.permissions import ManageAdminsAuth, role_of
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])
ops_router = APIRouter(tags=["admin-ops"])

#: What the portal can show from this backend; it hides the rest.
FEATURES = (
    "payouts", "disputes", "refunds", "admins", "settings", "jobs", "push", "suppressions",
    "whatsapp_templates", "club_reject", "two_factor_policy",
)
HISTORY = 30


class SettingIn(BaseModel):
    value: Any = None


def _iso(at: datetime | None) -> str | None:
    if at is None:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()


async def _rows(session) -> dict[str, PlatformSettings]:
    return {r.key: r for r in (await session.scalars(select(PlatformSettings))).all()}


async def _names(session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = (await session.execute(select(Users.id, Users.full_name).where(Users.id.in_(ids)).execution_options(include_deleted=True))).all()
    return {i: n for i, n in rows}


def _setting_out(spec: runtime.Spec, row: PlatformSettings | None, names: dict[int, str]) -> dict:
    stored = row is not None
    if spec.env:
        server = runtime.env_value(spec.key)
        value = runtime.coerce(spec, row.value) if stored else server
    else:
        server = spec.default
        value = runtime.coerce(spec, row.value) if stored else spec.default
    return {
        "key": spec.key,
        "label": spec.label,
        "help": spec.help,
        "kind": spec.kind,
        "choices": list(spec.choices),
        "group": spec.group,
        "env": spec.env,
        "env_name": f"APP_{spec.key}" if spec.env else None,
        "value": value,
        "server_value": server,
        "overridden": stored,
        "restart_required": spec.restart,
        "note": spec.note,
        "updated_at": _iso(row.updated_at) if stored else None,
        "updated_by": ({"id": row.updated_by, "name": names.get(row.updated_by)} if stored and row.updated_by else None),
    }


async def _history(session) -> list[dict]:
    rows = (await session.scalars(
        select(AdminAuditLogs)
        .where(AdminAuditLogs.entity_type == "platform_setting")
        .order_by(AdminAuditLogs.created_at.desc(), AdminAuditLogs.id.desc())
        .limit(HISTORY)
    )).all()
    return [{
        "at": _iso(r.created_at),
        "key": r.entity_id,
        "action": r.action,
        "actor": r.actor_name or r.actor_email,
        "from": (r.details or {}).get("from"),
        "to": (r.details or {}).get("to"),
    } for r in rows]


@router.get("", summary="Runtime platform settings, the secrets' health, recent changes (admin)")
async def list_settings(session: SessionDep, user: AdminAuth) -> dict:
    rows = await _rows(session)
    names = await _names(session, {r.updated_by for r in rows.values() if r.updated_by})
    return {
        "settings": [_setting_out(spec, rows.get(key), names) for key, spec in runtime.SPECS.items()],
        "secrets": runtime.secrets_health(),
        "history": await _history(session),
        "refresh_seconds": runtime.REFRESH_SECONDS,
        "can_edit": role_of(user).value == "owner",
    }


def _spec(key: str) -> runtime.Spec:
    spec = runtime.SPECS.get(key)
    if spec is None:
        raise CustomHTTPException(404, message="No such setting", error_code="UNKNOWN_SETTING")
    return spec


async def _current(session, spec: runtime.Spec) -> Any:
    row = await session.get(PlatformSettings, spec.key)
    if row is not None:
        try:
            return runtime.coerce(spec, row.value)
        except ValueError:
            return row.value
    return runtime.env_value(spec.key) if spec.env else spec.default


@router.put("/{key}", summary="Change a runtime setting (owner)")
async def update_setting(key: str, body: SettingIn, session: SessionDep, user: ManageAdminsAuth, request: Request) -> dict:
    spec = _spec(key)
    try:
        value = runtime.coerce(spec, body.value)
    except ValueError as exc:
        raise CustomHTTPException(422, message=f"{spec.label} {exc}", error_code="INVALID_SETTING")
    before = await _current(session, spec)
    await runtime.write(session, key, value, actor_id=user.id)
    if key == "admin_2fa_required" and value and not before:
        # Turned (back) on: the admins without two-factor get their days from now.
        await runtime.write(session, runtime.GRACE_STARTED_AT, datetime.now(timezone.utc).isoformat(), actor_id=user.id)
    await audit.record(
        session, actor=user, action="platform_setting.update", entity_type="platform_setting", entity_id=key,
        summary=f"Set {spec.label} to {value!r}"[:255], details={"from": before, "to": value}, request=request,
    )
    await session.commit()
    await runtime.load(session)
    rows = await _rows(session)
    names = await _names(session, {user.id})
    return _setting_out(spec, rows.get(key), names)


@router.delete("/{key}", summary="Put a runtime setting back to the server's own value (owner)")
async def reset_setting(key: str, session: SessionDep, user: ManageAdminsAuth, request: Request) -> dict:
    spec = _spec(key)
    before = await _current(session, spec)
    await runtime.remove(session, key)
    after = runtime.env_value(key) if spec.env else spec.default
    if key == "admin_2fa_required" and after and not before:
        await runtime.write(session, runtime.GRACE_STARTED_AT, datetime.now(timezone.utc).isoformat(), actor_id=user.id)
    await audit.record(
        session, actor=user, action="platform_setting.reset", entity_type="platform_setting", entity_id=key,
        summary=f"Put {spec.label} back to the server's value"[:255], details={"from": before, "to": after}, request=request,
    )
    await session.commit()
    await runtime.load(session)
    return _setting_out(spec, None, {})


# ─── The portal's own probe, and the public banner ───────────────────────


@ops_router.get("/admin/ops/me", summary="What this backend's operator tools offer, and the admin's two-factor standing")
async def ops_me(session: SessionDep, user: DependsAuth) -> dict:
    """Open to an admin who must still set up two-factor (any signed-in
    account may call it; anyone but an admin is refused): the portal reads
    it to send them to the set-up page, or to show the grace banner."""
    if getattr(user.user_type, "value", user.user_type) != "admin":
        raise CustomHTTPException(403, message="Not Authorized", error_code="INSUFFICIENT_PERMISSIONS")
    banner = await runtime.read(session, "maintenance_banner", "")
    return {
        "features": list(FEATURES),
        "role": role_of(user).value,
        "two_factor": (await two_factor.standing(session, user)).as_dict(),
        "maintenance_banner": (banner or None),
    }


@ops_router.get("/platform/banner", summary="The maintenance banner, if any (public)")
async def platform_banner(session: SessionDep, response: Response) -> dict:
    banner = await runtime.read(session, "maintenance_banner", "")
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"message": banner or None}
