"""Kept documents expire (app/api/events/doc_cache_sweep.py), safely.

Nothing ever deleted a rendered ticket or certificate, so docs/ only grew.
The sweep deletes kept documents last written more than APP_DOC_CACHE_DAYS
ago, and nothing else: not a key of another shape under docs/, not a key
outside it, not one newer than the cutoff, and not more than the run's cap.

The bucket is a fake that pages and deletes like S3 and records each call.
"""
import inspect
import io
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.admin_alerts.models import AdminNotifications
from app.api.events import doc_cache, doc_cache_sweep as sweep
from app.config import settings
from app.core.storage import objects
from scripts import doc_cache_report
from tests.conftest import auth_header

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
HEX = "0123456789abcdef01234567"


class FakeBucket:
    """ListObjectsV2 (pages of MaxKeys, in key order, continuation tokens) and
    DeleteObjects (Quiet) over a dict; every call recorded."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.lists: list[dict] = []
        self.deletes: list[tuple[float, list]] = []
        self.refuse: set = set()
        self.ignore_prefix = False  # a listing that strays outside what was asked

    def add(self, key: str, age_days: float, size: int = 1000, body: bytes = b"%PDF-1.4"):
        self.objects[key] = {"Size": size, "LastModified": NOW - timedelta(days=age_days), "Body": body}
        return key

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        self.lists.append({"Bucket": Bucket, "Prefix": Prefix, "MaxKeys": MaxKeys, "ContinuationToken": ContinuationToken})
        keys = sorted(k for k in self.objects if self.ignore_prefix or k.startswith(Prefix))
        if ContinuationToken:
            keys = [k for k in keys if k > ContinuationToken]
        page = keys[:MaxKeys]
        out = {"Contents": [{"Key": k, "Size": self.objects[k]["Size"], "LastModified": self.objects[k]["LastModified"]} for k in page],
               "IsTruncated": len(keys) > MaxKeys, "KeyCount": len(page)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = page[-1]
        return out

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        assert Delete.get("Quiet") is True and 0 < len(keys) <= 1000
        self.deletes.append((time.monotonic(), keys))
        errors = [{"Key": k, "Code": "AccessDenied", "Message": "no"} for k in keys if k in self.refuse]
        for k in keys:
            if k not in self.refuse:
                self.objects.pop(k, None)
        return {"Errors": errors} if errors else {}

    @property
    def deleted(self) -> list:
        return [k for _, keys in self.deletes for k in keys]


def doc(kind="ticket", version=0, reg=None, fp=HEX) -> str:
    return doc_cache.key_for(kind, reg or uuid.uuid4(), version, fp)


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(objects, "client", lambda: b)
    return b


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "DOC_CACHE_DAYS", 90)
    monkeypatch.setattr(settings, "DOC_CACHE_MAX_DELETE_PER_RUN", 50_000)
    monkeypatch.setattr(settings, "DOC_CACHE_SKIP_ALERT_PERCENT", 5.0)


# First in the module, as it uses the database: a module whose first test
# does not leaves its database tests on another event loop (tests/test_redis.py).


async def test_off_by_default_nothing_is_listed(session, bucket):
    bucket.add(doc(), 400)
    assert settings.DOC_CACHE_DAYS == 0

    assert await sweep.run_if_due(session, NOW, pause=0) is None
    assert bucket.lists == [] and bucket.deletes == []


async def test_only_kept_documents_older_than_the_cutoff_are_deleted(session, bucket, on):
    base = sweep.root()
    old = [bucket.add(doc("ticket"), 91), bucket.add(doc("certificate", 7), 400), bucket.add(doc("ticket", 12), 90.01)]
    newer = [bucket.add(doc(), 89), bucket.add(doc("certificate"), 1)]
    at_cutoff = bucket.add(doc(), 90)  # written exactly at the cutoff: not older than it
    reg = str(uuid.uuid4())
    unknown = [
        bucket.add(f"{base}ticket/{reg}/0-{HEX}.pdf.bak", 400),           # another extension
        bucket.add(f"{base}ticket/{reg}/0-{HEX}.png", 400),               # an extension the cache does not write
        bucket.add(f"{base}idcard/{reg}/0-{HEX}.pdf", 400),               # a kind the cache does not write
        bucket.add(f"{base}ticket/not-a-uuid/0-{HEX}.pdf", 400),
        bucket.add(f"{base}ticket/{reg.upper()}/0-{HEX}.pdf", 400),        # not as the cache writes a uuid
        bucket.add(f"{base}ticket/{reg}/0-{HEX[:-1]}.pdf", 400),          # 23 hex
        bucket.add(f"{base}ticket/{reg}/0-{HEX.upper()}.pdf", 400),
        bucket.add(f"{base}ticket/{reg}/x/0-{HEX}.pdf", 400),             # one folder deeper
        bucket.add(f"{base}ticket/{reg}/-1-{HEX}.pdf", 400),
        bucket.add(f"{base}README.txt", 400),
        bucket.add(f"{base}ticket/{reg}/", 400),                           # a "folder" marker
    ]
    outside = [
        bucket.add(f"{settings.S3_BASE_PATH}/templates/certificates/backgrounds/v-{'c' * 32}.png", 400),
        bucket.add(f"{settings.S3_BASE_PATH}/docs-old/ticket/{reg}/0-{HEX}.pdf", 400),
        bucket.add(f"{settings.S3_BASE_PATH}/events/7/layout/docs/ticket/{reg}/0-{HEX}.pdf", 400),
        bucket.add(f"docs/ticket/{reg}/0-{HEX}.pdf", 400),                # docs/ under another base path
    ]

    done = await sweep.sweep(days=90, max_delete=50_000, now=NOW, pause=0)

    assert sorted(bucket.deleted) == sorted(old)
    for key in newer + [at_cutoff] + unknown + outside:
        assert key in bucket.objects, key
    assert {c["Prefix"] for c in bucket.lists} == {base} and base.endswith("/docs/")
    assert done["deleted"] == 3 and done["deleted_bytes"] == 3000 and done["documents"] == 6
    assert done["newer"] == 3 and done["unknown"] == len(unknown) and done["errors"] == 0 and not done["capped"]


async def test_a_listing_that_strays_outside_docs_touches_nothing_there(session, bucket, on):
    bucket.ignore_prefix = True
    outside = bucket.add(f"{settings.S3_BASE_PATH}/clubs/logos/v-{'d' * 32}.png", 400)
    lookalike = bucket.add(f"{settings.S3_BASE_PATH}/clubs/docs/ticket/{uuid.uuid4()}/0-{HEX}.pdf", 400)
    old = bucket.add(doc(), 400)

    done = await sweep.sweep(days=90, max_delete=50_000, now=NOW, pause=0)

    assert bucket.deleted == [old] and outside in bucket.objects and lookalike in bucket.objects
    assert done["outside"] == 2


async def test_at_most_the_runs_cap_and_the_rest_the_next_day(session, bucket, on):
    old = [bucket.add(doc(), 100 + i) for i in range(12)]

    first = await sweep.sweep(days=90, max_delete=5, now=NOW, pause=0)
    assert first["deleted"] == 5 and first["capped"] and len(bucket.objects) == 7
    second = await sweep.sweep(days=90, max_delete=5, now=NOW, pause=0)
    third = await sweep.sweep(days=90, max_delete=5, now=NOW, pause=0)

    assert (second["deleted"], third["deleted"], third["capped"]) == (5, 2, False)
    assert sorted(bucket.deleted) == sorted(old) and bucket.objects == {}
    nothing = await sweep.sweep(days=90, max_delete=0, now=NOW, pause=0)
    assert nothing["deleted"] == 0


async def test_pages_of_a_thousand_and_deletes_of_a_thousand_with_a_pause_between(session, bucket, on):
    old = {bucket.add(doc(), 120) for _ in range(2500)}
    keep = {bucket.add(doc(), 3) for _ in range(300)}

    done = await sweep.sweep(days=90, max_delete=50_000, now=NOW, pause=0.2)

    assert [len(keys) for _, keys in bucket.deletes] == [1000, 1000, 500]
    times = [t for t, _ in bucket.deletes]
    assert all(b - a >= 0.19 for a, b in zip(times, times[1:]))
    assert all(c["MaxKeys"] == 1000 for c in bucket.lists) and len(bucket.lists) == 3
    assert bucket.lists[0]["ContinuationToken"] is None and all(c["ContinuationToken"] for c in bucket.lists[1:])
    assert set(bucket.deleted) == old and set(bucket.objects) == keep
    assert done["listed"] == 2800 and done["deleted"] == 2500


async def test_a_delete_the_bucket_refuses_is_counted_and_left(session, bucket, on):
    refused = bucket.add(doc(), 200)
    bucket.refuse.add(refused)
    gone = bucket.add(doc(), 200)

    done = await sweep.sweep(days=90, max_delete=50_000, now=NOW, pause=0)

    assert done["deleted"] == 1 and done["errors"] == 1 and refused in bucket.objects and gone not in bucket.objects
    assert done["error_samples"] == [f"{refused}: AccessDenied"]


async def test_once_a_day_on_one_process_with_a_summary_in_the_log(session, bucket, on, caplog, _engine):
    old = bucket.add(doc(), 200)
    caplog.set_level(logging.INFO, logger="app.api.events.doc_cache_sweep")

    # Another process running it: this one does nothing.
    async with _engine.connect() as other:
        other = await other.execution_options(isolation_level="AUTOCOMMIT")
        await other.scalar(select(func.pg_advisory_lock(sweep.SWEEP_LOCK)))
        try:
            assert await sweep.run_if_due(session, NOW, pause=0) is None
        finally:
            await other.scalar(select(func.pg_advisory_unlock(sweep.SWEEP_LOCK)))
    assert bucket.lists == []

    done = await sweep.run_if_due(session, NOW, pause=0)
    assert done["deleted"] == 1 and old not in bucket.objects
    assert "doc cache sweep: 1 deleted" in caplog.text
    bucket.add(doc(), 300)
    assert await sweep.run_if_due(session, NOW + timedelta(hours=23), pause=0) is None  # not a day yet
    assert (await sweep.run_if_due(session, NOW + timedelta(hours=24, minutes=1), pause=0))["deleted"] == 1


async def test_an_alert_when_much_of_docs_is_not_kept_documents(session, bucket, on):
    base = sweep.root()
    for _ in range(54):
        bucket.add(doc(), 10)
    for i in range(6):
        bucket.add(f"{base}exports/report-{i}.csv", 10)  # 10% of what is there

    done = await sweep.run_if_due(session, NOW, pause=0)

    assert done["unknown"] == 6 and done["unknown_percent"] == 10.0 and len(bucket.objects) == 60
    alerts = list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "doc_cache_unknown"))).all())
    assert len(alerts) == 1 and alerts[0].data["unknown"] == 6 and alerts[0].data["samples"][0].endswith(".csv")


async def test_no_alert_for_a_few_stray_files(session, bucket, on):
    base = sweep.root()
    for _ in range(99):
        bucket.add(doc(), 10)
    bucket.add(f"{base}stray.txt", 10)

    done = await sweep.run_if_due(session, NOW, pause=0)

    assert done["unknown_percent"] == 1.0
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "doc_cache_unknown")) == 0


async def test_a_download_after_expiry_draws_it_again_and_serves_it(client, session, bucket, on, monkeypatch, make_club, make_event, make_user, make_registration, mint_token):
    """A deleted document is no lost document: the next download draws it
    (once: doc_cache's single-flight) and keeps it again."""
    from app.api.events import tickets

    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: (bucket.objects.get(key) or {}).get("Body"))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: bucket.add(key, 0, size=len(data), body=data))
    drawn = []

    def pdf(template_path, context, options=None, template_dir=None):
        drawn.append(context["email_payload"]["participant_name"])
        return io.BytesIO(b"%PDF-1.4 " + context["email_payload"]["participant_name"].encode())

    monkeypatch.setattr(tickets, "generate_pdf_bytes", pdf)
    owner, club = await make_club(name="Yukthi", email="sweep@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night",
                             event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    guest = await make_user(email="sweep-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer")
    await session.commit()
    h = auth_header(await mint_token(guest))
    url = f"/api/v1/events/tickets/{reg.ticket_id}/pdf"

    first = await client.get(url, headers=h)
    assert first.status_code == 200 and drawn == ["Ananya Iyer"]
    (kept,) = bucket.objects
    assert sweep.kind_of(kept) == "ticket"
    bucket.objects[kept]["LastModified"] = NOW - timedelta(days=120)  # four months on

    done = await sweep.sweep(days=90, max_delete=50_000, now=NOW, pause=0)
    assert done["deleted"] == 1 and bucket.objects == {}

    again = await client.get(url, headers=h)
    assert again.status_code == 200 and again.headers["content-type"] == "application/pdf"
    assert again.content == first.content and drawn == ["Ananya Iyer", "Ananya Iyer"]
    assert list(bucket.objects) == [kept]  # kept again, under the same key


def test_it_runs_with_the_other_leader_loops():
    import doth

    source = inspect.getsource(doth.start_loops)
    assert "from app.api.events.doc_cache_sweep import sweep_loop" in source and "doc_cache_sweep_loop" in source.split("return")[-1]


def test_the_pattern_is_exactly_what_the_cache_writes():
    reg = uuid.uuid4()
    for kind in doc_cache.KINDS:
        assert sweep.kind_of(doc_cache.key_for(kind, reg, 0, HEX)) == kind
        assert sweep.kind_of(doc_cache.key_for(kind, reg, 123, "f" * 24)) == kind
    assert sweep.kind_of(doc_cache.key_for("ticket", reg, 0, HEX).replace(".pdf", ".png")) is None


def test_the_report_counts_by_kind_and_age_and_says_what_would_go_and_writes_nothing(monkeypatch):
    b = FakeBucket()
    for age, size in ((5, 100), (20, 200), (45, 400), (100, 800), (400, 1600)):
        b.add(doc("ticket"), age, size=size)
    b.add(doc("certificate"), 95, size=3200)
    b.add(f"{sweep.root()}notes.txt", 2, size=7)
    monkeypatch.setattr(settings, "DOC_CACHE_MAX_DELETE_PER_RUN", 2)

    out = doc_cache_report.report(b, days=90, now=NOW)

    t = out["by_kind"]["ticket"]
    assert t["under 30 days"] == {"count": 2, "bytes": 300}
    assert t["30-90 days"] == {"count": 1, "bytes": 400}
    assert t["over 90 days"] == {"count": 2, "bytes": 2400}
    assert out["by_kind"]["certificate"]["over 90 days"] == {"count": 1, "bytes": 3200}
    assert out["by_kind"]["not a kept document"]["under 30 days"] == {"count": 1, "bytes": 7}
    assert out["would_delete"]["count"] == 3 and out["would_delete"]["bytes"] == 5600 and out["would_delete"]["runs_needed"] == 2
    assert out["objects"] == {"count": 7, "bytes": 6307} and out["not_kept_documents"]["count"] == 1
    assert b.deletes == [] and len(b.objects) == 7  # read-only
    assert doc_cache_report.report(b, days=30, now=NOW)["would_delete"]["count"] == 4
