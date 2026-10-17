from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.finance import service
from app.api.finance.schemas import ExpenseCreate, ExpenseOut, ExpensePatch, FinanceSummary
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter()


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


def _csv(body: str, filename: str) -> Response:
    return Response(content=body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


# ─── The club's season ───────────────────────────────────────────────────


@router.get("/clubs/finance", summary="The club's money over a period: tickets, refunds, expenses, net — by month, by type, by event")
async def club_finance(session: SessionDep, user: ClubAuth, frm: Optional[date] = Query(None, alias="from"), to: Optional[date] = Query(None)) -> FinanceSummary:
    club = await _club_for(session, user)
    frm, to = service.resolve_period(frm, to)
    return FinanceSummary(**await service.club_summary(session, club, frm, to))


@router.get("/clubs/finance/statement.csv", summary="The same period as a statement file")
async def club_statement(session: SessionDep, user: ClubAuth, frm: Optional[date] = Query(None, alias="from"), to: Optional[date] = Query(None)) -> Response:
    club = await _club_for(session, user)
    frm, to = service.resolve_period(frm, to)
    return _csv(await service.club_statement(session, club, frm, to), f"{club.slug}-statement-{frm.isoformat()}-{to.isoformat()}.csv")


# ─── Expenses ────────────────────────────────────────────────────────────


@router.post("/clubs/finance/expenses", summary="Log an expense, against an event or against the club", status_code=201)
async def create_expense(session: SessionDep, user: ClubAuth, body: ExpenseCreate) -> ExpenseOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "finance", actor=user)
    return ExpenseOut(**await service.create_expense(session, club, body, user))


@router.patch("/clubs/finance/expenses/{expense_id}", summary="Correct an expense")
async def update_expense(session: SessionDep, user: ClubAuth, expense_id: UUID, body: ExpensePatch) -> ExpenseOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "finance", actor=user)
    x = await service.expense_for(session, club, expense_id)
    return ExpenseOut(**await service.update_expense(session, club, x, body))


@router.delete("/clubs/finance/expenses/{expense_id}", summary="Remove an expense")
async def delete_expense(session: SessionDep, user: ClubAuth, expense_id: UUID) -> dict:
    club = await _club_for(session, user)
    await service.delete_expense(session, await service.expense_for(session, club, expense_id))
    return {"deleted": True}


# ─── One event ───────────────────────────────────────────────────────────


@router.get("/events/{event_id}/finance", summary="What this event brought and what it cost")
async def event_finance(session: SessionDep, user: ClubAuth, event_id: int) -> FinanceSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return FinanceSummary(**await service.event_summary(session, event))


@router.get("/events/{event_id}/finance/statement.csv", summary="The event's ledger as a statement file")
async def event_statement(session: SessionDep, user: ClubAuth, event_id: int) -> Response:
    event = await deliveries.event_for_club(session, user, event_id)
    return _csv(await service.event_statement(session, event), f"{event.slug}-statement.csv")
