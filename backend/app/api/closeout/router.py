from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.closeout import service
from app.api.club_access import service as club_access
from app.api.cohosts import access as cohost_access
from app.api.cohosts.policy import HOST_ONLY_MESSAGE
from app.api.closeout.schemas import ReportOut, RetentionStatusOut, RunBody, RunResult, SettingsOut, SettingsPatch
from app.api.deliveries import service as deliveries
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/closeout")


@router.get("/figures", summary="The numbers the report would print now")
async def figures(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    return await service.figures(session, event)


@router.get("/reports", summary="Every closeout report generated")
async def reports(session: SessionDep, user: ClubAuth, event_id: int) -> list[ReportOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [ReportOut(**r) for r in await service.list_reports(session, event)]


@router.post("/reports", summary="Generate the report as of now", status_code=201)
async def generate(session: SessionDep, user: ClubAuth, event_id: int) -> ReportOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "report", actor=user, event=event)
    row = await service.generate(session, event, user)
    return ReportOut(**service.report_out(row, user.full_name or user.email))


@router.get("/reports/{report_id}.pdf", summary="A report's PDF")
async def report_pdf(session: SessionDep, user: ClubAuth, event_id: int, report_id: UUID) -> Response:
    event = await deliveries.event_for_club(session, user, event_id)
    pdf, filename = await service.report_file(session, event, report_id)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/settings", summary="The status clock and the retention settings")
async def get_settings(session: SessionDep, user: ClubAuth, event_id: int) -> SettingsOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SettingsOut(**service.settings_of(event))


#: What happens to the guests' data, and who is sent the report of it: the
#: retention mode and hours, whether contacts are kept, and the report's
#: recipients. Deleting is forward only, and a short delay is the same as
#: running retention now (host-only, owners and admins); the recipients get
#: the attendee report. So changing any of them is the host's owner's or
#: admins' alone -- never a co-host's, never an editor's -- whatever else the
#: request changes. The status and the clock stay with every role that edits
#: the event (publishing a draft is one of them).
RETENTION_FIELDS = ("retention_mode", "retention_hours", "keep_contacts", "report_recipients")


def _changes_retention(event, body: SettingsPatch) -> list[str]:
    """The retention fields this request would change. A field sent with the
    value it already has is no change: the dashboard sends the whole form."""
    now = service.settings_of(event)
    sent = body.model_dump(exclude_unset=True)
    return [f for f in RETENTION_FIELDS if sent.get(f) is not None and sent[f] != now[f]]


def _may_set_retention(request: Request, event, user) -> None:
    from app.api.events.access import is_admin_user

    if is_admin_user(user):
        return  # an admin acting for the club; ClubAuth already held them to MANAGE_CONTENT
    if cohost_access.granted(event.id, user.id) or event.club_id != getattr(getattr(user, "club", None), "id", None):
        host = getattr(getattr(event, "club", None), "name", None) or "the host"
        raise CustomHTTPException(403, message=HOST_ONLY_MESSAGE.format(host=host), error_code="COHOST_HOST_ONLY")
    if club_access.role_of(request) not in ("owner", "admin"):
        raise CustomHTTPException(
            403,
            message=f"Your role ({club_access.role_label(club_access.role_of(request))}) can't change what happens to guests' data or who gets the report. Ask the club's owner.",
            error_code="ROLE_FORBIDDEN",
        )


@router.patch("/settings", summary="Change the status, the clock, or retention")
async def patch_settings(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> SettingsOut:
    """The status itself stays editable on a closed event: that is how it is
    reopened. Retention, kept contacts and the report's recipients
    (RETENTION_FIELDS) are the host's owner's and admins' alone: 403
    COHOST_HOST_ONLY for a co-host, 403 ROLE_FORBIDDEN for an editor."""
    event = await deliveries.event_for_club(session, user, event_id)
    if _changes_retention(event, body):
        _may_set_retention(request, event, user)
    recipients = body.report_recipients
    if recipients and list(recipients) != list(event.report_recipients or []):
        # Naming people to send the report to is sending it; the plan's.
        await gates.require_feature(session, event.club_id, "report", actor=user, event=event)
    return SettingsOut(**await service.update_settings(session, event, body))


@router.get("/retention", summary="Where retention stands")
async def retention(session: SessionDep, user: ClubAuth, event_id: int) -> RetentionStatusOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return RetentionStatusOut(**await service.retention_status(session, event))


@router.post("/retention/run", summary="Run retention now")
async def run(session: SessionDep, user: ClubAuth, event_id: int, body: RunBody) -> RunResult:
    """Report first, then the scrub, forward only; the slug typed back confirms it."""
    event = await deliveries.event_for_club(session, user, event_id)
    return RunResult(**await service.run_retention(session, event, manual=True, confirm_slug=body.confirm_slug, user=user))
