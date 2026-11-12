"""The real designs (app/core/doc_designs) filled from our own rows by the
real mapping (app/api/doc_presets), for every one of the 35 designs.

Each half was built against a stand-in of the other (tests/doc_designs_fake.py
for the mapping; the package's sample data for the designs). This is where
they meet: a value the mapping does not give, a token a design calls required
that a real guest or event lacks, or a token left in the page would make the
guest's document fall back to the classic look in production (D9), quietly.
So no render service is needed: the filled HTML is checked here.

The cases are the ones real people bring: a one-word name, an event with no
venue, no photo, no job title.
"""
import html
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.doc_presets import engine
from app.api.doc_presets.jobs import mapped_for, materialise
from app.api.doc_presets.values import subject_of
from app.api.events.models import EventRegistrationsLink, Events

NOW = datetime.now(timezone.utc)
KINDS = ("ticket", "certificate", "badge")


def _all_designs() -> list[tuple[str, str]]:
    return [(kind, d["id"]) for kind in KINDS for d in engine.catalogue(kind)]


async def _registration(session, make_club, make_event, make_user, make_registration, *, name: str, venue):
    owner, club = await make_club(name="Integration Club", email=f"integ-{abs(hash(name)) % 10**8}@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night Kochi", event_datetime=NOW + timedelta(days=5),
        location_name=venue, is_online=False,
    )
    guest = await make_user(email=f"guest-{abs(hash(name)) % 10**8}@example.com")
    guest.full_name = name
    reg = await make_registration(
        event=event, user=guest, full_name=name, ticket_label="General",
        certificate_issued_at=NOW,
    )
    await session.commit()
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg.id)
        .options(selectinload(EventRegistrationsLink.event).selectinload(Events.club))
        .execution_options(populate_existing=True)
    )
    return reg


@pytest.mark.parametrize("name,venue", [("Asha Nair", "Tagore Theatre, Trivandrum"), ("Meera", None)])
async def test_every_design_fills_from_a_real_registration(session, make_club, make_event, make_user, make_registration, name, venue):
    counts = {kind: len(engine.catalogue(kind)) for kind in KINDS}
    assert counts == {"ticket": 10, "certificate": 10, "badge": 15}, counts
    reg = await _registration(session, make_club, make_event, make_user, make_registration, name=name, venue=venue)
    event = reg.event
    subject = subject_of(reg, event)
    failures = []
    for kind, design_id in _all_designs():
        preset = {"id": design_id}
        try:
            mapped = mapped_for(kind, event, subject, {}, preset)
            values, _ = materialise(mapped, event_id=event.id)
            page = engine.fill(preset, values)
        except Exception as exc:  # noqa: BLE001 - collected, all reported at once
            failures.append(f"{design_id}: {type(exc).__name__}: {exc}")
            continue
        if "{{" in page:
            failures.append(f"{design_id}: a token is left in the page")
        first = name.split()[0]
        if html.escape(first) not in page and html.escape(first.upper()) not in page and first not in page:
            failures.append(f"{design_id}: the guest's name is not on it")
    assert not failures, "\n".join(failures)
