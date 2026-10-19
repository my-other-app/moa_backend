"""Design artwork stored before uploads were made ready for print:
scripts/optimize_doc_art.py writes each one again under a new key, points
every design at it, and leaves the old file where it was.
"""
import re
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.api.certificate_templates.models import CertificateTemplates
from app.api.events import tickets
from app.api.idcards.models import BadgeTemplates
from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from app.core.storage import objects
from scripts import optimize_doc_art
from tests.conftest import auth_header
from tests.test_doc_art import _opened, _photo, _save


@pytest.fixture
def bucket(monkeypatch):
    """The bucket as a dict, key -> (bytes, content type); HEAD answers from it too."""
    held: dict = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, (data, content_type)))
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: (held.get(key) or (None,))[0])
    monkeypatch.setattr(objects, "client", lambda: FakeS3(held))
    return held


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="doc-art-script@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night",
                             event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    guest = await make_user(email="doc-art-script-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer", is_attended=True)
    await session.commit()
    return {"event": event, "club": club, "reg": reg, "h": auth_header(await mint_token(owner))}


class FakeS3:
    """HEAD for the script's sizes; reads and writes go through objects (the `bucket` fixture)."""

    def __init__(self, held: dict):
        self.held = held

    def head_object(self, Bucket, Key):
        if Key not in self.held:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.held[Key][0])}


def _layout(template_key=None, image_key=None, version=3) -> dict:
    s = ticket_starter("classic")
    body = dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))
    if template_key:
        body["canvas"]["templateKey"] = template_key
    if image_key:
        body["layers"].append({"kind": "image", "id": "pic", "name": "Picture", "x": 40, "y": 40, "width": 200,
                               "height": 100, "source": "upload", "imageKey": image_key})
    return {**body, "version": version}


@pytest.fixture
async def stored_before(session, world, bucket):
    """Designs as production has them: artwork stored as it was uploaded."""
    base, e = settings.S3_BASE_PATH, world["event"].id
    keys = {
        "poster": f"{base}/events/{e}/layout/template-{'a' * 32}.png",
        "small": f"{base}/events/{e}/layout/image-{'b' * 32}.png",
        "background": f"{base}/templates/certificates/backgrounds/v-{'c' * 32}.png",
        "legacy_logo": f"{base}/templates/certificates/logos/0b6f2d6e-4a51-4b0e-9a2b-1f7c4b9d2e11.jpg",
    }
    bucket[keys["poster"]] = (_save(_photo(3000, 1650, seed=1), "PNG"), "image/png")
    bucket[keys["small"]] = (_save(Image.new("RGB", (200, 100), (250, 250, 250)), "PNG"), "image/png")
    bucket[keys["background"]] = (_save(_photo(3508, 2480, seed=2), "PNG"), "image/png")
    bucket[keys["legacy_logo"]] = (_save(_photo(2600, 1300, seed=3), "JPEG", quality=98), "image/jpeg")
    legacy_link = f"https://{settings.S3_BUCKET}.s3.amazonaws.com/{keys['legacy_logo']}?Signature=old&Expires=1700000000"

    ticket = TicketTemplates(event_id=e, audience=TemplateAudience.event_default, name="Ticket",
                             config={"layout": _layout(keys["poster"], keys["small"])})
    deleted = TicketTemplates(club_id=world["club"].id, audience=TemplateAudience.event_default, name="Old",
                              config={"layout": _layout(keys["poster"])}, is_deleted=True)
    badge = BadgeTemplates(event_id=e, audience=TemplateAudience.event_default, name="Card",
                           config={"layout": _layout(keys["poster"])})
    certificate = CertificateTemplates(event_id=e, audience=TemplateAudience.event_default, name="Certificate",
                                       background_image=keys["background"],
                                       config={"background_image_key": keys["background"], "logo_url": legacy_link})
    session.add_all([ticket, deleted, badge, certificate])
    await session.commit()
    before = {k: v[0] for k, v in bucket.items()}
    return {"keys": keys, "before": before, "ticket": ticket, "deleted": deleted, "badge": badge, "certificate": certificate}


async def test_the_dry_run_reads_and_writes_nothing(session, stored_before, bucket, monkeypatch):
    writes = []
    monkeypatch.setattr(objects, "put_bytes", lambda *a, **k: writes.append(a))
    keys = stored_before["keys"]

    report = await optimize_doc_art.run(session, apply=False)

    assert report["mode"] == "dry-run" and writes == []
    assert {c["old"] for c in report["changes"]} == {keys["poster"], keys["background"], keys["legacy_logo"]}
    assert all(c["new"] is None and c["after"] < c["before"] for c in report["changes"])
    assert report["skipped_small"] == 1 and report["optimised"] == 3 and report["failed"] == []
    assert report["bytes_after"] < report["bytes_before"] / 5
    assert report["rows_repointed"] == {} and report["old_keys"] == []
    assert {k: v[0] for k, v in bucket.items()} == stored_before["before"]
    await session.refresh(stored_before["ticket"])
    assert stored_before["ticket"].config["layout"]["canvas"]["templateKey"] == keys["poster"]


async def test_apply_writes_new_keys_repoints_every_design_and_keeps_the_old_files(session, stored_before, bucket):
    keys = stored_before["keys"]

    report = await optimize_doc_art.run(session, apply=True)

    assert report["mode"] == "apply" and report["failed"] == [] and report["optimised"] == 3
    moved = {c["old"]: c["new"] for c in report["changes"]}
    assert sorted(report["old_keys"]) == sorted(moved)
    # The old files are where they were, byte for byte; the new ones beside them.
    for key, data in stored_before["before"].items():
        assert bucket[key][0] == data
    poster = moved[keys["poster"]]
    assert re.fullmatch(rf"{re.escape(keys['poster'].rsplit('/', 1)[0])}/template-[0-9a-f]{{32}}\.jpg", poster)
    assert poster != keys["poster"] and bucket[poster][1] == "image/jpeg" and max(_opened(bucket[poster][0]).size) == 2400
    background = moved[keys["background"]]
    assert re.search(r"/templates/certificates/backgrounds/v-[0-9a-f]{32}\.jpeg$", background)
    logo = moved[keys["legacy_logo"]]
    assert re.search(r"/templates/certificates/logos/v-[0-9a-f]{32}\.jpeg$", logo)

    for row in ("ticket", "deleted", "badge", "certificate"):
        await session.refresh(stored_before[row])
    ticket = stored_before["ticket"].config["layout"]
    assert ticket["canvas"]["templateKey"] == poster and ticket["version"] == 3  # it prints the same: no new version
    assert ticket["layers"][-1]["imageKey"] == keys["small"]  # under the threshold: left alone
    assert stored_before["deleted"].config["layout"]["canvas"]["templateKey"] == poster
    assert stored_before["badge"].config["layout"]["canvas"]["templateKey"] == poster
    certificate = stored_before["certificate"]
    assert certificate.background_image.variations["original"] == background
    assert certificate.config["background_image_key"] == background
    assert certificate.config["logo_key"] == logo and "logo_url" not in certificate.config  # the legacy link, as a key now
    assert report["rows_repointed"] == {"ticket_templates": 2, "badge_templates": 1, "certificate_templates": 1}

    again = await optimize_doc_art.run(session, apply=True)
    assert again["optimised"] == 0 and again["changes"] == [] and again["skipped_small"] == 4


async def test_a_ticket_kept_before_the_artwork_moved_is_drawn_again_with_the_new_one(session, world, stored_before, bucket, monkeypatch):
    """The cache key hashes the design, its image keys included: a design
    pointing at the new artwork names a new cache key, and the next download
    draws it. Nothing has to be invalidated."""
    from app.api.events import service

    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    asked: list = []
    real_get = objects.get_bytes
    monkeypatch.setattr(objects, "get_bytes", lambda key: asked.append(key) or real_get(key))

    reg = await service.get_ticket_details(session, ticket_id=world["reg"].ticket_id)
    first = await tickets.ticket_document(session, reg)
    kept_first = [k for k in bucket if "/docs/ticket/" in k]
    assert len(kept_first) == 1 and stored_before["keys"]["poster"] in asked

    report = await optimize_doc_art.run(session, apply=True)
    poster = {c["old"]: c["new"] for c in report["changes"]}[stored_before["keys"]["poster"]]
    asked.clear()
    second = await tickets.ticket_document(session, await service.get_ticket_details(session, ticket_id=world["reg"].ticket_id))

    kept = [k for k in bucket if "/docs/ticket/" in k]
    assert len(kept) == 2 and poster in asked  # drawn again, on the new artwork, kept under a new key
    assert second.content != first.content and len(second.content) < len(first.content) / 5
