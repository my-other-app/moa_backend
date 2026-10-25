"""The platform's "do not mail" list (app.core.mail.suppression): addresses
that hard-bounced or marked our mail as spam, with why and when, and a way
to take one off once the mailbox is known to work again. Taking one off is
a moderator's decision and is in the admin audit log; reading is for every
admin."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from sqlalchemy import delete, func, select

from app.api.audit import service as audit
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ModerateAuth
from app.core.mail.suppression import BOUNCE, COMPLAINT, EmailSuppressions, normal
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/mail/suppressions", tags=["admin-mail"])


def _out(r: EmailSuppressions) -> dict:
    return {"email": r.email, "reason": r.reason, "detail": r.detail, "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("", summary="Addresses we no longer mail, and why (admin)")
async def list_suppressions(
    session: SessionDep,
    user: AdminAuth,
    q: Optional[str] = Query(None, max_length=200, description="Part of the address"),
    reason: Optional[str] = Query(None, description="bounce or complaint"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    where = []
    if q and q.strip():
        term = q.strip().lower().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        where.append(EmailSuppressions.email.like(f"%{term}%"))
    if reason:
        if reason not in (BOUNCE, COMPLAINT):
            raise CustomHTTPException(422, message="reason is bounce or complaint")
        where.append(EmailSuppressions.reason == reason)
    total = int(await session.scalar(select(func.count()).select_from(EmailSuppressions).where(*where)) or 0)
    rows = (await session.scalars(
        select(EmailSuppressions).where(*where).order_by(EmailSuppressions.created_at.desc(), EmailSuppressions.email)
        .offset(offset).limit(limit)
    )).all()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    counts = dict((await session.execute(
        select(EmailSuppressions.reason, func.count()).group_by(EmailSuppressions.reason)
    )).all())
    recent = int(await session.scalar(select(func.count()).select_from(EmailSuppressions).where(EmailSuppressions.created_at >= since)) or 0)
    return {
        "items": [_out(r) for r in rows], "total": total, "offset": offset, "limit": limit,
        "counts": {"bounce": int(counts.get(BOUNCE, 0)), "complaint": int(counts.get(COMPLAINT, 0)), "last_30_days": recent},
    }


@router.delete("/{email}", summary="Take an address off the list: mail goes to it again (moderate)")
async def unblock(email: str, session: SessionDep, user: ModerateAuth, request: Request) -> dict:
    address = normal(email)
    row = await session.get(EmailSuppressions, address)
    if row is None:
        raise CustomHTTPException(404, message="That address is not on the list", error_code="NOT_SUPPRESSED")
    reason, detail, since = row.reason, row.detail, row.created_at
    await session.execute(delete(EmailSuppressions).where(EmailSuppressions.email == address))
    await audit.record(
        session, actor=user, action="mail.unsuppress", entity_type="email", entity_id=address[:64],
        summary=f"Took {address} off the do-not-mail list ({reason})"[:255],
        details={"email": address, "reason": reason, "detail": detail, "since": since.isoformat() if since else None},
        request=request,
    )
    await session.commit()
    return {"email": address, "removed": True}
