"""A kiosk is a device, not a person: it carries a token bound to one
event, rotated per shift, revocable from the dashboard."""
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.checkins.gate_auth import GateActor
from app.api.events.models import Events
from app.api.kiosk.models import KioskDevices
from app.db.core import SessionDep
from app.response import CustomHTTPException

KIOSK_TOKEN_HEADER = "X-Kiosk-Token"

logger = logging.getLogger(__name__)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str, str]:
    token = "kiosk_" + secrets.token_urlsafe(24)
    # The prefix an organiser tells links apart by: the six characters after "kiosk_".
    return token, hash_token(token), token[6:12]


async def device_by_token(session, token: str) -> KioskDevices | None:
    if not token:
        return None
    device = await session.scalar(select(KioskDevices).options(joinedload(KioskDevices.event).joinedload(Events.club), joinedload(KioskDevices.door)).where(KioskDevices.token_hash == hash_token(token)))
    if device is None or device.revoked_at is not None or device.expires_at < datetime.now(timezone.utc):
        return None
    return device


async def kiosk_device(request: Request, session: SessionDep) -> KioskDevices:
    """The token from the X-Kiosk-Token header only: in the query string it
    lands in every access log between the tablet and here."""
    return await _device(request, session, request.headers.get(KIOSK_TOKEN_HEADER) or "")


async def kiosk_device_for_frame(request: Request, session: SessionDep) -> KioskDevices:
    """For a card PDF the kiosk loads into a frame, which cannot send a
    header: the header, else `?token=` -- deprecated, kept only while the
    deployed kiosk prints that way (moa_web kioskCardUrl, kioskSelfCardUrl).
    Fetching the PDF with the header and framing the blob ends it."""
    token = request.headers.get(KIOSK_TOKEN_HEADER) or ""
    if not token and request.query_params.get("token"):
        token = request.query_params.get("token") or ""
        logger.info("kiosk token read from the query string (deprecated) for %s", request.url.path)
    return await _device(request, session, token)


async def _device(request: Request, session, token: str) -> KioskDevices:
    device = await device_by_token(session, token)
    if device is None:
        raise CustomHTTPException(401, message="This kiosk is not paired, or its shift has ended. Pair it again from Team & doors.", error_code="KIOSK_UNPAIRED")
    now = datetime.now(timezone.utc)
    # A heartbeat, but not a write on every poll.
    if device.last_seen_at is None or (now - device.last_seen_at).total_seconds() > 60:
        device.last_seen_at = now
        await session.commit()
    request.state.gate_event = device.event
    return device


KioskAuth = Annotated[KioskDevices, Depends(kiosk_device)]
KioskFrameAuth = Annotated[KioskDevices, Depends(kiosk_device_for_frame)]


def kiosk_actor(device: KioskDevices) -> GateActor:
    """What a kiosk may do at the door: check in, nothing else."""
    return GateActor(kind="kiosk", name=device.name, door=device.door, can_check_in=True)
