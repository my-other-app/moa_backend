"""GET /events/{event_id}/people/{ref}: one person, everything about them."""
import re

from fastapi import APIRouter, Request

from app.api.club_access import door, service as club_access
from app.api.cohosts import access as cohosts
from app.api.deliveries import service as deliveries
from app.api.events.access import is_admin_user
from app.api.kiosk import print_requests
from app.api.people import service, view
from app.api.people.schemas import PersonProfile
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/people")

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _mask_words(out: dict) -> None:
    """Door staff: an address a provider or a form wrote into a sentence is masked like the contacts."""
    hide = lambda text: _ADDRESS.sub(lambda m: door.mask_email(m.group(0)), text) if isinstance(text, str) else text  # noqa: E731
    for row in out["timeline"]:
        row["detail"], row["title"] = hide(row["detail"]), hide(row["title"])
    for row in out["deliveries"]:
        row["reason"] = hide(row["reason"])


@router.get("/{ref}", summary="One person on an event: their details, their ticket, and a log of everything that happened")
async def get_person(request: Request, session: SessionDep, user: ClubAuth, event_id: int, ref: str) -> PersonProfile:
    """`ref` is a registration's id, a ticket code, or a registration request's id
    (Review rows have no registration until the club confirms them). A request
    that became a registration is the same person: one history from the first
    request on.

    Read with the rights of the attendee list and the Review page: the event's
    club, a co-host, platform admins, and the team by role. Door staff read it
    with contacts and the ticket code masked, and without the ticket's file."""
    event = await deliveries.event_for_club(session, user, event_id)
    reg, req, requests = await service.resolve(session, event, ref)
    d = await service.gather(session, event, reg, req, requests)
    online = await print_requests.printers_online(session, event.id)
    door_view = door.viewer(request)
    out = view.build(
        d, role=club_access.role_of(request), platform_admin=is_admin_user(user),
        cohost=cohosts.granted(event.id, user.id), door_view=door_view, printers_online=online,
    )
    # Whether an app notification can reach them (app/api/users/app_use): the "Message" dialog offers it only then.
    uid = getattr(reg, "user_id", None) if reg is not None else getattr(req, "user_id", None)
    if uid is not None:
        from sqlalchemy import select

        from app.api.users.app_use import on_app

        out["status"]["on_app"] = bool(await session.scalar(select(on_app(uid))))
    if door_view and out["ticket"] is not None:
        # The ticket code is the entry credential itself: the last four characters only.
        out["ticket"]["code"] = door.mask_ticket(out["ticket"]["code"])
    if door_view:
        _mask_words(out)
    return out
