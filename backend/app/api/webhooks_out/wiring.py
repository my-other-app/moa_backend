"""Webhooks' seams (seams.py), filled with the partner API's real answers.
`install()` runs once at start-up, from doth (the API and the worker both
import it); tests/test_webhooks_e2e.py fails if it did not.

- club_allocated: the club's allocation (Admin -> Integrations, or a plan
  with api_access), and the platform's kill switch (PARTNER_API_ENABLED):
  off, nothing is queued for anyone.
- partner_scopes: what the partner's live connection to the club in that
  mode has -- its scopes within what the app may ever ask for -- while the
  app is active; nothing for a suspended app or a connection revoked.
- partner_contact: the app's name and its contact address.
- external_id: the id that client gave the object (app/api/partner/external_ids).
"""
from __future__ import annotations

from typing import Optional


async def club_allocated(session, club_id: int) -> bool:
    from app.api.partner import allocation
    from app.config import settings

    if not settings.PARTNER_API_ENABLED:
        return False
    return bool((await allocation.of(session, club_id)).allocated)


async def _app(session, partner_app_id, client_id):
    from app.api.partner import apps
    from app.api.partner.models import PartnerApps

    if partner_app_id is not None:
        found = await session.get(PartnerApps, partner_app_id)
        if found is not None:
            return found
    return await apps.by_client_id(session, client_id)


async def partner_scopes(session, club_id: int, partner_app_id, client_id, *, livemode: bool = True) -> frozenset:
    from app.api.partner import apps
    from app.api.partner import oauth
    from app.api.partner import scopes as catalogue

    app = await _app(session, partner_app_id, client_id)
    if app is None or app.status != "active":
        return frozenset()
    conn = await apps.live_connection(session, app.id, club_id, bool(livemode))
    if conn is None:
        return frozenset()
    granted = set(catalogue.parse(conn.scopes)) & set(catalogue.parse(app.allowed_scopes))
    return frozenset(granted - set(oauth.APP_TOKEN_SCOPES))


async def partner_contact(session, partner_app_id, client_id) -> Optional[tuple[str, str]]:
    app = await _app(session, partner_app_id, client_id)
    if app is None:
        return None
    return (app.name, app.contact_email) if app.contact_email else (app.name, "")


async def external_id(session, client_id, object_type, object_id) -> Optional[str]:
    from app.api.partner import external_ids

    if not client_id or object_id is None or object_type not in external_ids.RESOURCES:
        return None
    return await external_ids.of_object(session, client_id, object_type, object_id)


def install() -> None:
    from app.api.webhooks_out import seams

    seams.install(club_allocated=club_allocated, partner_scopes=partner_scopes,
                  partner_contact=partner_contact, external_id=external_id)
