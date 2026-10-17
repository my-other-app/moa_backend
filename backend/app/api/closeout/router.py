from uuid import UUID

from fastapi import APIRouter, Response

from app.api.closeout import service
from app.api.closeout.schemas import ReportOut, RetentionStatusOut, RunBody, RunResult, SettingsOut, SettingsPatch
from app.api.deliveries import service as deliveries
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

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
    await gates.require_feature(session, event.club_id, "report", actor=user)
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


@router.patch("/settings", summary="Change the status, the clock, or retention")
async def patch_settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> SettingsOut:
    """The status itself stays editable on a closed event: that is how it is reopened."""
    event = await deliveries.event_for_club(session, user, event_id)
    recipients = body.report_recipients
    if recipients and list(recipients) != list(event.report_recipients or []):
        # Naming people to send the report to is sending it; the plan's.
        await gates.require_feature(session, event.club_id, "report", actor=user)
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
