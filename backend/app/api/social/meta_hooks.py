"""What Meta requires of an app that connects Pages and Instagram accounts:

- the deauthorise callback (POST /api/v1/social/meta/deauthorize): a person
  removed MyOtherApp in their Facebook settings. Every Page and Instagram
  account connected through them is disconnected (tokens deleted, posts
  held for a reconnect) and their clubs are told;
- the data deletion callback (POST /api/v1/social/meta/data-deletion): the
  same, and what we kept of those accounts (names, handles, pictures, the
  person's id) is wiped. Answered, as Meta asks, with
  `{url, confirmation_code}`; the url is the public status page
  (myotherapp.in/data-deletion?code=...), which reads
  GET /api/v1/social/data-deletion/{code}.

Both carry Meta's `signed_request`, checked against the app secret
(adapters.meta.parse_signed_request); anything else is refused, and nothing
is ever deleted on an unsigned word.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update

from app.api.social import accounts as accounts_mod
from app.api.social import alerts
from app.api.social.adapters.meta import parse_signed_request
from app.api.social.models import DISCONNECTED, SocialAccounts, SocialDataDeletions
from app.config import settings
from app.response import CustomHTTPException

META = ("facebook", "instagram")


def _refuse() -> CustomHTTPException:
    return CustomHTTPException(400, message="Not a request signed by Meta for this app.", error_code="SIGNED_REQUEST_INVALID")


def _subject(signed: Optional[str]) -> str:
    payload = parse_signed_request(signed)
    if payload is None:
        raise _refuse()
    return str(payload["user_id"])


async def _disconnect(session, user_id: str) -> int:
    rows = list(await session.scalars(select(SocialAccounts).where(
        SocialAccounts.connector_external_id == user_id, SocialAccounts.platform.in_(META), SocialAccounts.status != DISCONNECTED,
    )))
    for account in rows:
        waiting = (await accounts_mod.waiting_counts(session, [account.id])).get(account.id, 0)
        await accounts_mod.disconnect(session, account, revoke=False)
        await alerts.account_needs_reconnect(session, account, waiting)
        await alerts.record(session, actor=None, action="disconnect", club_id=account.club_id,
                            summary=f"{alerts.LABELS.get(account.platform)} disconnected from Meta's side",
                            details={"account_id": str(account.id), "platform": account.platform, "by": "meta"})
    return len(rows)


async def deauthorize(session, signed: Optional[str]) -> int:
    n = await _disconnect(session, _subject(signed))
    await session.commit()
    return n


def status_url(code: str) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/data-deletion?code={code}"


async def data_deletion(session, signed: Optional[str]) -> dict:
    user_id = _subject(signed)
    n = await _disconnect(session, user_id)
    await session.execute(
        update(SocialAccounts).where(SocialAccounts.connector_external_id == user_id, SocialAccounts.platform.in_(META))
        .values(name=None, handle=None, picture_url=None, connector_external_id=None, scopes=None, last_error=None)
        .execution_options(synchronize_session=False)
    )
    code = secrets.token_hex(10)
    now = datetime.now(timezone.utc)
    session.add(SocialDataDeletions(code=code, platform="meta", subject_hash=hashlib.sha256(user_id.encode()).hexdigest(),
                                    accounts=n, requested_at=now, completed_at=now))
    await session.commit()
    return {"url": status_url(code), "confirmation_code": code}


async def deletion_status(session, code: str) -> dict:
    row = await session.get(SocialDataDeletions, code) if code and len(code) <= 40 else None
    if row is None:
        raise CustomHTTPException(404, message="No deletion request with that code.", error_code="DELETION_NOT_FOUND")
    return {
        "code": row.code, "status": "completed" if row.completed_at else "pending", "requested_at": row.requested_at,
        "completed_at": row.completed_at, "accounts": row.accounts,
        "what": "Connected Facebook Pages and Instagram accounts: their access tokens, names, handles, pictures and your Facebook id.",
    }
