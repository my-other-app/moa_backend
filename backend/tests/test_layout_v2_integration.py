"""Every v2 starter (app.core.layout_v2: the 35 designs, the CR80 card, the
blanks) filled from our own rows by the one values function and drawn the
way documents are (feature-register/layout-v2-contract.md, Q).

P converted the starters against the designer's sample data; this is where
they meet our real data: a token the starters print that the values do not
give would print as nothing, a token left in the page, or a starter our
wiring cannot draw would send the guest's document to the classic look in
production (D9), quietly. So each is drawn as a real document is -- the
ticket and certificate jobs, the card pages -- through P's builder and A's
render client, with only the render service faked at the HTTP level
(tests/render_transport_fake.py); the page it is sent is checked.

The cases are the ones real people bring: an everyday guest at a venue, and
a one-word name at an event with no venue, no photo, no job title.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.doc_presets import layouts
from app.api.events.models import EventRegistrationsLink, Events
from app.core import layout_v2
from app.core.storage import objects
from tests.render_transport_fake import install, page_texts

NOW = datetime.now(timezone.utc)
KINDS = ("ticket", "certificate", "badge")
#: The designer's designs per kind, and the others P adds (layout-v2 contract, V3).
DESIGNS = {"ticket": 10, "certificate": 10, "badge": 15}
EXTRAS = {"ticket": {"ticket/blank"}, "certificate": {"certificate/blank"}, "badge": {"badge/card_cr80", "badge/blank"}}


def _starters() -> dict:
    return {kind: layout_v2.starters(kind, with_layout=True) for kind in KINDS}


async def _registration(session, make_club, make_event, make_user, make_registration, *, name: str, venue):
    owner, club = await make_club(name="Integration Club", email=f"v2-integ-{abs(hash(name)) % 10**8}@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night Kochi", event_datetime=NOW + timedelta(days=5),
        location_name=venue, is_online=False,
    )
    guest = await make_user(email=f"v2-guest-{abs(hash(name)) % 10**8}@example.com")
    guest.full_name = name
    reg = await make_registration(
        event=event, user=guest, full_name=name, ticket_label="General", certificate_issued_at=NOW,
    )
    await session.commit()
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg.id)
        .options(selectinload(EventRegistrationsLink.event).selectinload(Events.club))
        .execution_options(populate_existing=True)
    )


class _Fallback:
    """The classic job a v2 job falls back to (D9): reaching it is a failure here."""

    def __init__(self):
        self.used = False

    def draw(self):
        self.used = True
        return b"%PDF-fallback", False


@pytest.mark.parametrize("name,venue", [("Asha Nair", "Tagore Theatre, Trivandrum"), ("Meera", None)])
async def test_every_v2_starter_fills_from_a_real_registration(session, make_club, make_event, make_user, make_registration, monkeypatch, name, venue):
    found = _starters()
    if not any(found.values()):
        pytest.skip("P's starters (app/core/layout_v2/starters/) have not landed on this branch yet")
    for kind in KINDS:
        ids = {s["id"] for s in found[kind]}
        assert len(ids - EXTRAS[kind]) == DESIGNS[kind] and EXTRAS[kind] <= ids, (kind, sorted(ids))

    render = install(monkeypatch)
    # No bucket: no pictures (the club has no logo; nobody has a photo -- the initials tile).
    monkeypatch.setattr(objects, "get_bytes", lambda key: None)
    reg = await _registration(session, make_club, make_event, make_user, make_registration, name=name, venue=venue)
    event = reg.event
    first = name.split()[0]
    failures = []
    for kind in KINDS:
        for s in found[kind]:
            layout = s["layout"]
            errors = [p for p in layout_v2.problems(layout, kind) if p["level"] == "error"]
            if errors:
                failures.append(f"{s['id']}: the starter itself is refused: {errors[:2]}")
                continue
            style = {"layout": {**layout, "version": 2}}
            before = render.renders
            try:
                if kind == "badge":
                    await layouts.pages_pdf([layouts.card_page(event, reg, style, door="Main gate", printed_at=NOW)])
                else:
                    fallback = _Fallback()
                    job = layouts.job(kind, reg, style, fallback=fallback)
                    await job.draw()
                    if fallback.used or job.state.get("fallback"):
                        failures.append(f"{s['id']}: drawn by the classic renderer instead")
                        continue
            except Exception as exc:  # noqa: BLE001 - collected, all reported at once
                failures.append(f"{s['id']}: {type(exc).__name__}: {exc}")
                continue
            if render.renders != before + 1:
                failures.append(f"{s['id']}: not one call to the render service")
                continue
            page = render.last_html()
            if "{{" in page or "{%" in page:
                failures.append(f"{s['id']}: a token is left in the page")
            text = " ".join(" ".join(lines) for lines in page_texts(page))
            if first not in text and first.upper() not in text:
                failures.append(f"{s['id']}: the guest's name is not on it")
            if venue is None and "None" in text.split():
                failures.append(f"{s['id']}: prints None for the missing venue")
    assert not failures, "\n".join(failures)
