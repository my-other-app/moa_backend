"""The domains sweep reaches every row, however many there are.

`_ids` took the first BATCH rows by id. The entitlement sync leaves a row as
it was, so every run took the same hundred and the hundred-and-first domain
was never looked at again; the Cloudflare poll went the same way while
Cloudflare kept answering "pending".
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.club_domains import sweep
from app.api.club_domains.models import ACTIVE, PENDING, ClubDomains


@pytest.fixture
async def domains(session, make_club):
    async def make(n: int, status: str, checked=None) -> list[int]:
        ids = []
        for i in range(n):
            _, club = await make_club(name=f"Rota {status} {i}", email=f"rota-{status}-{i}@example.com")
            row = ClubDomains(club_id=club.id, host=f"www.rota-{status}-{i}.org", status=status, cf_hostname_id=f"cf-{status}-{i}",
                              checked_at=checked(i) if checked else None)
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
        return ids

    getattr(sweep, "_cursors", {}).clear()
    yield make
    getattr(sweep, "_cursors", {}).clear()


async def test_the_entitlement_sync_goes_round_every_row(session, domains, monkeypatch):
    ids = await domains(5, ACTIVE, checked=lambda i: datetime.now(timezone.utc))
    monkeypatch.setattr(sweep, "BATCH", 3)
    seen: list[int] = []

    async def sync(session, row, now):
        seen.append(row.id)
        return None  # nothing changes: the row stays in the criteria

    monkeypatch.setattr(sweep, "_sync", sync)
    await sweep.run_sweep(session)
    await sweep.run_sweep(session)

    assert set(ids) <= set(seen)


async def test_the_cloudflare_poll_asks_the_least_recently_asked_first(session, domains, monkeypatch):
    long_ago = datetime.now(timezone.utc) - timedelta(days=1)
    ids = await domains(3, PENDING, checked=lambda i: None if i == 2 else long_ago + timedelta(minutes=i))
    monkeypatch.setattr(sweep, "BATCH", 2)
    monkeypatch.setattr(sweep.cf, "available", lambda: True)
    polled: list[int] = []

    async def poll(session, row, now):
        polled.append(row.id)
        row.checked_at = now  # Cloudflare answered: still pending
        return None

    monkeypatch.setattr(sweep, "_poll", poll)
    monkeypatch.setattr(sweep, "_sync", lambda *a: _none())
    await sweep.run_sweep(session)

    # Never asked first, then the one asked longest ago.
    assert polled == [ids[2], ids[0]]
    await sweep.run_sweep(session)
    assert polled[2] == ids[1]


async def _none():
    return None
