"""An assistant customising an event's designs over MCP (app/api/mcp/design_tools.py).

Through the protocol, as test_mcp.py drives it: which tools a scope and a
role get; the guide's example being a valid design of each kind; a layout
checked as the board checks it, every problem named; starters, saves and
reverts, with the revision only moving on; a preview that is a PNG of the
made-up guest (the render service faked at the HTTP level, so the page it is
sent is read) and never a real one; another club's event and a co-hosted one
"not found"; the ID card's plan gate; a certificate already issued keeping
its design; and issuing certificates only through a person's approval.
"""
import base64
import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.club_access.models import ClubMembers
from app.api.events.models import EventRegistrationsLink, Events
from app.api.mcp.design_tools import example
from app.config import settings
from app.core import layout_v2
from app.core.storage import objects
from tests.conftest import auth_header
from tests.render_transport_fake import install, page_texts
from tests.test_mcp import NOW, _owner_token, call, connect, mcp_on, rpc, tools_list, world  # noqa: F401

API = "/api/v1"
READS = {"list_design_starters", "get_design_guide", "get_event_design", "preview_event_design", "list_design_revisions"}
WRITES = {"update_event_design", "apply_design_starter", "revert_event_design"}


@pytest.fixture
def render(monkeypatch):
    return install(monkeypatch)


@pytest.fixture
def store(monkeypatch):
    held: dict = {}
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: held.get(key))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


async def _design(client, token, event_id, kind, **more) -> dict:
    out = await call(client, token, "get_event_design", {"event_id": event_id, "kind": kind, **more})
    assert "_error" not in out, out
    return out


def _bad(out: dict, code: str = "INVALID_LAYOUT") -> dict:
    assert out.get("_error") == code, out
    return out


# ─── Who gets which tool ─────────────────────────────────────────────────


async def test_the_design_tools_follow_the_scopes_and_the_role(client, session, world, monkeypatch):
    owner = await connect(client, "robotics@example.com")
    names = set(await tools_list(client, owner.access_token))
    assert READS | WRITES <= names and "request_issue_certificates" not in names
    reads_only = await connect(client, "robotics@example.com", scopes=["designs:read"])
    assert set(await tools_list(client, reads_only.access_token)) == READS
    r = await rpc(client, reads_only.access_token, "tools/call", {"name": "update_event_design", "arguments": {}})
    assert r.status_code == 403 and 'scope="designs:write"' in r.headers["www-authenticate"]
    # Door staff read designs but neither draw nor change them (a preview is the board's POST).
    door = await connect(client, "door@robotics.example.com")
    door_names = set(await tools_list(client, door.access_token))
    assert READS - {"preview_event_design"} <= door_names and not (WRITES & door_names) and "preview_event_design" not in door_names
    assert (await call(client, door.access_token, "preview_event_design", {"event_id": world.ids.upcoming, "kind": "ticket"}))["_error"] == "ROLE_FORBIDDEN"
    # An editor may; demoted, they may not, at once.
    editor = await connect(client, "editor@robotics.example.com")
    assert WRITES <= set(await tools_list(client, editor.access_token))
    member = await session.scalar(select(ClubMembers).where(ClubMembers.email == "editor@robotics.example.com"))
    member.role = "door"
    await session.commit()
    out = await call(client, editor.access_token, "update_event_design", {"event_id": world.ids.upcoming, "kind": "ticket", "layout": example("ticket")})
    assert out["_error"] == "ROLE_FORBIDDEN"
    # The certificates request is offered only once the dashboard can confirm it.
    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", True)
    assert "request_issue_certificates" in await tools_list(client, owner.access_token)


async def test_another_clubs_event_and_a_cohosted_one_are_not_found(client, session, world, store):
    from app.api.cohosts.models import ACCEPTED, EventCohosts

    session.add(EventCohosts(event_id=world.ids.upcoming, club_id=world.ids.other_club, status=ACCEPTED, invited_at=NOW,
                             expires_at=NOW + timedelta(days=1), responded_at=NOW))
    await session.commit()
    host = await connect(client, "robotics@example.com")
    assert (await call(client, host.access_token, "get_event_design", {"event_id": world.ids.secret, "kind": "ticket"}))["_error"] == "EVENT_NOT_FOUND"
    out = await call(client, host.access_token, "update_event_design", {"event_id": world.ids.secret, "kind": "ticket", "layout": example("ticket")})
    assert out["_error"] == "EVENT_NOT_FOUND"
    # The co-host's assistant does not act on the host's event; the host's does.
    cohost = await connect(client, "chess@example.com")
    for name, args in (("get_event_design", {}), ("update_event_design", {"layout": example("ticket")}), ("list_design_revisions", {})):
        out = await call(client, cohost.access_token, name, {"event_id": world.ids.upcoming, "kind": "ticket", **args})
        assert out["_error"] == "EVENT_NOT_FOUND", name
    assert (await _design(client, host.access_token, world.ids.upcoming, "ticket"))["event"] == "Bot Build Night"


# ─── The guide ───────────────────────────────────────────────────────────


async def test_the_guide_has_a_valid_example_and_never_offers_contacts_on_a_poster(client, world):
    c = await connect(client, "robotics@example.com", scopes=["designs:read"])
    for kind in ("ticket", "certificate", "badge", "poster"):
        guide = await call(client, c.access_token, "get_design_guide", {"kind": kind, "event_id": world.ids.upcoming})
        assert "_error" not in guide, guide
        assert [p for p in layout_v2.problems(guide["example"], kind) if p["level"] == "error"] == [], kind
        assert guide["fonts"] and all({"family", "weights", "italic"} <= set(f) for f in guide["fonts"])
        assert guide["pages"] and guide["layers"]["qr"] and guide["rules"]
        keys = {t["key"] for t in guide["tokens"]}
        if kind == "poster":
            # The event's form asks for another phone number: never a poster's.
            assert not keys & {"email", "phone", "field.phone_alt"} and "field.why" in keys
            assert {p["format"] for p in guide["pages"]} == {"square", "portrait", "story"}
        else:
            assert "field.phone_alt" in keys
    assert (await call(client, c.access_token, "list_design_starters", {"kind": "certificate"}))["starters"][0]["id"].startswith("certificate/")
    posters = (await call(client, c.access_token, "list_design_starters", {"kind": "poster"}))["starters"]
    assert posters and all(s["id"].startswith("poster/") for s in posters)


# ─── Checking, saving, undoing ───────────────────────────────────────────


async def test_a_layout_is_checked_as_the_board_checks_it(client, world, store):
    c = await connect(client, "robotics@example.com")
    args = {"event_id": world.ids.upcoming, "kind": "ticket"}
    font = example("ticket")
    font["layers"][1]["font"]["family"] = "Comic Sans"
    out = _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": font}))
    assert "layers.1.font.family" in out["fields"] and "Comic Sans" in out["message"]
    no_qr = example("ticket")
    no_qr["layers"] = [layer for layer in no_qr["layers"] if layer["kind"] != "qr"]
    out = _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": no_qr}))
    assert "QR" in out["message"] and any(p["code"] == "qr_missing" for p in out["problems"])
    token = example("ticket")
    token["layers"][2]["text"] = "{{nope_not_a_token}}"
    assert "nope_not_a_token" in _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": token}))["message"]
    shape = example("ticket")
    shape["canvas"]["width"] = 2000
    assert any(p["code"] == "page_shape" for p in _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": shape}))["problems"])
    foreign = example("ticket")
    foreign["layers"].append({"id": "pic", "name": "Pic", "kind": "image", "x": 0, "y": 0, "width": 50, "height": 50,
                              "source": "upload", "imageKey": f"events/{world.ids.secret}/layout/image-x.png"})
    assert "layers.6.imageKey" in _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": foreign}))["fields"]
    script = example("ticket")
    script["layers"].append({"id": "art", "name": "Art", "kind": "svg", "x": 0, "y": 0, "width": 100, "height": 100,
                             "markup": '<svg viewBox="0 0 10 10"><script>alert(1)</script><rect width="10" height="10"/></svg>'})
    _bad(await call(client, c.access_token, "update_event_design", {**args, "layout": script}))
    # A poster never prints a contact: the catalogue's, or the form's phone question.
    poster = {"event_id": world.ids.upcoming, "kind": "poster", "format": "square"}
    for text in ("Mail {{email}}", "Call {{field.phone_alt}}"):
        p = example("poster")
        p["layers"][2]["text"] = text
        _bad(await call(client, c.access_token, "update_event_design", {**poster, "layout": p}))
    wrong = example("poster")
    _bad(await call(client, c.access_token, "update_event_design", {**poster, "format": "story", "layout": wrong}))
    # Nothing was saved by any of those, and no change was recorded.
    assert (await _design(client, c.access_token, world.ids.upcoming, "ticket"))["is_own"] is False
    assert (await call(client, c.access_token, "list_design_revisions", args))["revisions"] == []
    assert (await call(client, c.access_token, "update_event_design", {**args, "variant": "vip", "layout": example("ticket")}))["_error"] == "INVALID_VARIANT"


async def test_a_starter_a_change_and_undoing_both(client, session, world, store):
    c = await connect(client, "robotics@example.com")
    args = {"event_id": world.ids.upcoming, "kind": "ticket"}
    start = await _design(client, c.access_token, world.ids.upcoming, "ticket")
    assert start["is_own"] is False and start["from"] == "default" and start["layout"]["layers"]
    applied = await call(client, c.access_token, "apply_design_starter", {**args, "starter_id": "ticket/01-dusk-horizon", "accent": "#12ab34"})
    assert applied["saved"] and applied["revision"] == 1 and applied["layout"]["vars"]["accent"] == "#12AB34"
    assert (await call(client, c.access_token, "apply_design_starter", {**args, "starter_id": "ticket/nope"}))["_error"] == "STARTER_NOT_FOUND"
    mine = example("ticket")
    changed = await call(client, c.access_token, "update_event_design", {**args, "layout": mine})
    assert changed["saved"] and changed["revision"] == 2
    now = await _design(client, c.access_token, world.ids.upcoming, "ticket")
    assert now["is_own"] is True and now["from"] == "saved" and now["revision"] == 2
    assert [layer["id"] for layer in now["layout"]["layers"]] == [layer["id"] for layer in mine["layers"]]

    history = (await call(client, c.access_token, "list_design_revisions", args))["revisions"]
    assert [h["change"] for h in history] == ["update_event_design", "apply_design_starter"]
    assert all(h["can_revert"] for h in history) and "no design of its own" in history[1]["revert_gives"]
    # Undo the change: the starter is back, at a newer revision.
    back = await call(client, c.access_token, "revert_event_design", {**args, "revision_id": history[0]["revision_id"]})
    assert back["revision"] == 3 and back["reverted_to"] == history[0]["revision_id"]
    assert (await _design(client, c.access_token, world.ids.upcoming, "ticket"))["layout"] == applied["layout"]
    # Undo the starter too: the event follows the default design again.
    await call(client, c.access_token, "revert_event_design", {**args, "revision_id": history[1]["revision_id"]})
    assert (await _design(client, c.access_token, world.ids.upcoming, "ticket"))["is_own"] is False
    # Each revert is a change of its own, and can itself be undone.
    history = (await call(client, c.access_token, "list_design_revisions", args))["revisions"]
    assert [h["change"] for h in history] == ["revert_event_design", "revert_event_design", "update_event_design", "apply_design_starter"]
    await call(client, c.access_token, "revert_event_design", {**args, "revision_id": history[0]["revision_id"]})
    again = await _design(client, c.access_token, world.ids.upcoming, "ticket")
    assert again["is_own"] is True and again["layout"] == applied["layout"] and again["revision"] == 4
    # A revision of another design, or none, is not found.
    other = await call(client, c.access_token, "revert_event_design", {"event_id": world.ids.upcoming, "kind": "certificate", "revision_id": history[0]["revision_id"]})
    assert other["_error"] == "REVISION_NOT_FOUND"
    assert (await call(client, c.access_token, "revert_event_design", {**args, "revision_id": 10**9}))["_error"] == "REVISION_NOT_FOUND"
    # Every change left its audit line: via mcp, which app and member, what it replaced.
    rows = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.entity_type == "event_design").order_by(AdminAuditLogs.id)))
    assert [r.action for r in rows] == ["mcp.design.starter_applied", "mcp.design.updated", "mcp.design.reverted", "mcp.design.reverted", "mcp.design.reverted"]
    assert all(r.details["via"] == "mcp" and r.details["client"] == "Claude" and "before" in r.details for r in rows)


async def test_a_speakers_version_and_a_posters_formats(client, world, store):
    c = await connect(client, "robotics@example.com")
    event = world.ids.upcoming
    saved = await call(client, c.access_token, "update_event_design", {"event_id": event, "kind": "badge", "variant": "speaker", "layout": example("badge")})
    assert saved["saved"] and saved["variant"] == "speaker"
    assert (await _design(client, c.access_token, event, "badge", variant="speaker"))["is_own"] is True
    assert (await _design(client, c.access_token, event, "badge"))["is_own"] is False
    # A poster starter sets all three formats of the variant; undone, it is gone.
    applied = await call(client, c.access_token, "apply_design_starter", {"event_id": event, "kind": "poster", "variant": "role:speaker", "starter_id": "poster/02-minimal-card"})
    assert applied["formats"] == ["portrait", "square", "story"]
    story = await _design(client, c.access_token, event, "poster", variant="role:speaker", format="story")
    assert story["is_own"] and story["layout"]["canvas"]["height"] == 1920
    square = await call(client, c.access_token, "update_event_design", {"event_id": event, "kind": "poster", "variant": "role:speaker", "format": "square", "layout": example("poster")})
    assert square["saved"]
    history = (await call(client, c.access_token, "list_design_revisions", {"event_id": event, "kind": "poster", "variant": "role:speaker"}))["revisions"]
    assert [h["formats"] for h in history] == [["square"], ["portrait", "square", "story"]]
    await call(client, c.access_token, "revert_event_design", {"event_id": event, "kind": "poster", "variant": "role:speaker", "revision_id": history[1]["revision_id"]})
    assert (await _design(client, c.access_token, event, "poster", variant="role:speaker", format="story"))["is_own"] is False


async def test_an_id_card_design_needs_the_plans_badges(client, world, store, monkeypatch):
    from app.api.subscriptions import gates

    real = gates.allows

    async def allows(session, club_id, feature, **kw):
        return False if feature == "badges" else await real(session, club_id, feature, **kw)

    monkeypatch.setattr(gates, "allows", allows)
    c = await connect(client, "robotics@example.com")
    out = await call(client, c.access_token, "update_event_design", {"event_id": world.ids.upcoming, "kind": "badge", "layout": example("badge")})
    assert out["_error"] == "PLAN_FEATURE_LOCKED" and out["feature"] == "badges"
    assert (await call(client, c.access_token, "apply_design_starter", {"event_id": world.ids.upcoming, "kind": "badge", "starter_id": "badge/blank"}))["_error"] == "PLAN_FEATURE_LOCKED"
    # Reading it and the other kinds are not gated.
    assert (await _design(client, c.access_token, world.ids.upcoming, "badge"))["is_own"] is False
    assert (await call(client, c.access_token, "update_event_design", {"event_id": world.ids.upcoming, "kind": "ticket", "layout": example("ticket")}))["saved"]


async def test_a_certificate_already_issued_keeps_its_design(client, session, world, store):
    from app.api.certificates import service as certificates

    event = await session.scalar(select(Events).where(Events.id == world.ids.upcoming))
    assert (await certificates.issue(session, event, "checked_in", []))["issued"] == 1
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world.ids.upcoming, EventRegistrationsLink.full_name == "Asha K"))
    pinned = dict(reg.certificate_design)
    c = await connect(client, "robotics@example.com")
    out = await call(client, c.access_token, "update_event_design", {"event_id": world.ids.upcoming, "kind": "certificate", "layout": example("certificate")})
    assert out["saved"] and any("already issued" in n for n in (await _design(client, c.access_token, world.ids.upcoming, "certificate"))["notes"])
    await session.refresh(reg)
    assert reg.certificate_design == pinned


# ─── The picture ─────────────────────────────────────────────────────────


async def _picture(client, token, arguments) -> tuple[dict, Image.Image]:
    r = await rpc(client, token, "tools/call", {"name": "preview_event_design", "arguments": arguments})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    text, image = result["content"]
    assert text["type"] == "text" and image["type"] == "image" and image["mimeType"] == "image/png"
    return result["structuredContent"], Image.open(io.BytesIO(base64.b64decode(image["data"])))


async def test_a_preview_is_a_picture_of_the_made_up_guest_never_a_real_one(client, world, render, store):
    c = await connect(client, "robotics@example.com", scopes=["designs:read"])
    args = {"event_id": world.ids.upcoming, "kind": "ticket"}
    data, picture = await _picture(client, c.access_token, args)
    assert max(picture.size) <= settings.MCP_PREVIEW_MAX_PX and data["image"]["width"] == picture.size[0]
    assert data["drawn"] == "the design in use now" and "made-up" in data["guest"]
    for kind, extra in (("ticket", {"layout": example("ticket")}), ("certificate", {}), ("badge", {"variant": "speaker"}),
                        ("poster", {"format": "story"})):
        await _picture(client, c.access_token, {"event_id": world.ids.upcoming, "kind": kind, **extra})
        page = render.last_html()
        # What the page shows (its fonts and pictures are data, read apart).
        shown = " ".join(line for lines in page_texts(page) for line in lines)
        assert "Aisha" in shown, (kind, shown)
        for real in ("Asha K", "Ravi M", "asha.k@example.com", "ravi@example.com"):
            assert real not in page, (kind, real)
        for real in ("98765", "9123456789", "To meet folks", "2026-11-14"):
            assert real not in shown, (kind, real)
    assert render.renders == 5
    # An unsaved layout is checked as a save would be, and nothing is saved.
    bad = example("ticket")
    bad["layers"] = bad["layers"][:-1]
    assert (await call(client, c.access_token, "preview_event_design", {**args, "layout": bad}))["_error"] == "INVALID_LAYOUT"
    assert (await _design(client, c.access_token, world.ids.upcoming, "ticket"))["is_own"] is False
    render.down = True
    assert (await call(client, c.access_token, "preview_event_design", args))["_error"] == "RENDER_UNAVAILABLE"


# ─── Issuing certificates: asked for, approved in the dashboard ──────────


async def test_issuing_certificates_waits_for_a_person(client, session, world, monkeypatch):
    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", True)
    c = await connect(client, "editor@robotics.example.com")
    out = await call(client, c.access_token, "request_issue_certificates", {"event_id": world.ids.upcoming})
    assert out["status"] == "pending_approval" and out["kind"] == "issue_certificates" and out["would_issue"] == 1
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.full_name == "Asha K"))
    assert reg.certificate_issued_at is None
    token = out["confirm_url"].rsplit("/", 1)[1]
    h = auth_header(await _owner_token(session, world))
    page = (await client.get(f"{API}/clubs/pending-actions/{token}", headers=h)).json()
    assert page["title"] == "Issue certificates for “Bot Build Night”" and page["can_decide"] is True
    assert page["target"]["type"] == "event" and page["target"]["certificates"]["to_issue"] == 1
    assert any("keep their design" in e for e in page["effects"])
    done = await client.post(f"{API}/clubs/pending-actions/{token}/confirm", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "confirmed" and done.json()["result"]["issued"] == 1
    await session.refresh(reg)
    assert reg.certificate_issued_at is not None
    # Nobody left without one: nothing to ask for. The roles audience needs its roles.
    assert (await call(client, c.access_token, "request_issue_certificates", {"event_id": world.ids.upcoming}))["_error"] == "NOTHING_TO_ISSUE"
    assert (await call(client, c.access_token, "request_issue_certificates", {"event_id": world.ids.upcoming, "audience": "roles"}))["_error"] == "INVALID_ARGUMENT"
    from app.api.club_access import policy

    assert policy.allows_action("editor", "issue_certificates") and not policy.allows_action("door", "issue_certificates")
    # Switched off again: not offered, and refused.
    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", False)
    assert (await call(client, c.access_token, "request_issue_certificates", {"event_id": world.ids.upcoming}))["_error"] == "UNKNOWN_TOOL"


async def test_a_certificate_request_says_who_and_when_it_cannot_be_approved(client, session, world, monkeypatch):
    from app.api.mcp.models import McpPendingActions

    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", True)
    c = await connect(client, "editor@robotics.example.com")
    out = await call(client, c.access_token, "request_issue_certificates", {"event_id": world.ids.upcoming})
    token = out["confirm_url"].rsplit("/", 1)[1]
    h = auth_header(await _owner_token(session, world))

    async def page() -> dict:
        return (await client.get(f"{API}/clubs/pending-actions/{token}", headers=h)).json()

    first = await page()
    assert first["effects"][0] == (
        "1 person gets a certificate for “Bot Build Night”: everyone who checked in, except anyone who already has one."
    )
    row = await session.scalar(select(McpPendingActions).where(McpPendingActions.kind == "issue_certificates"))
    row.payload = {**row.payload, "audience": "roles", "roles": ["speaker", "staff"]}
    await session.commit()
    roles = await page()
    assert roles["effects"][0].endswith("only speakers and staff, except anyone who already has one.")
    assert roles["can_decide"] is True
    # A count the server refuses (a role it doesn't know): said, and Approve not offered.
    row.payload = {**row.payload, "roles": ["wizard"]}
    await session.commit()
    refused = await page()
    assert refused["can_decide"] is False
    assert refused["cannot_decide_reason"] == "Pick roles from attendee, speaker and staff."
    # A deleted event is gone (soft-deleted rows are never read): no longer possible.
    row.payload = {**row.payload, "audience": "checked_in", "roles": []}
    event = await session.get(Events, world.ids.upcoming)
    event.is_deleted = True
    await session.commit()
    gone = await page()
    assert gone["target"] == {} and gone["can_decide"] is False
    assert gone["cannot_decide_reason"] == "This action is no longer possible."
