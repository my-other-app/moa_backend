"""Conditional GETs (app/core/middlewares/etag_middleware.py, APP_ETAGS).

The app is adding stale-while-revalidate: it shows what it last read and
asks again with that answer's ETag in If-None-Match. An unchanged answer is
then a 304 with no body instead of the whole list again. These pin what
gets a tag: a GET or HEAD under /api/v1/ answered 200 with JSON. They pin
what never does: the live stream, files, PDFs, redirects, errors and other
types. The Cache-Control a route set goes out unchanged, and the flag off
is exactly the old behaviour.
"""
import asyncio
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from app.config import settings
from app.core.middlewares import etag_middleware
from app.core.middlewares.etag_middleware import ETagMiddleware, matches, weak_etag
from tests.conftest import auth_header
from tests.test_live_stream import SSE, streams  # noqa: F401 (the live stream's reader and fixture)

WEAK = re.compile(r'W/"[0-9a-f]{32}"')
DOCUMENT = re.compile(r'"\d+-[0-9a-f]{24}"')


# ─── A small app behind the middleware ───────────────────────────────────


def _mini(state: dict, tmp_path) -> ETagMiddleware:
    """One route per kind of answer the API gives, behind the middleware alone."""
    kept = tmp_path / "kept.json"
    kept.write_bytes(b'{"a file": true}')

    async def things(request):
        return JSONResponse(
            {"items": [state["name"]]},
            headers={"Cache-Control": "private, max-age=60", "Vary": "Accept-Language", "X-Total": "1"},
        )

    async def bare(request):
        return JSONResponse({"items": [state["name"]]})

    async def public(request):
        return JSONResponse({"topics": ["Arts"]}, headers={"Cache-Control": "public, max-age=300"})

    async def pieces(request):
        body = b'{"items": ["one", "two", "three"]}'

        class Pieces(Response):
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                ]})
                for i in range(0, len(body), 12):
                    await send({"type": "http.response.body", "body": body[i:i + 12], "more_body": i + 12 < len(body)})

        return Pieces()

    async def events(request):
        async def frames():
            yield b"event: ping\n\n"
            await state["gate"].wait()
            yield b"event: snapshot\ndata: {}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform"})

    async def json_stream(request):
        async def parts():
            yield b'{"items": ['
            yield b"1]}"

        return StreamingResponse(parts(), media_type="application/json")

    routes = [
        Route("/api/v1/things", things),
        Route("/api/v1/bare", bare),
        Route("/api/v1/public", public),
        Route("/api/v1/pieces", pieces),
        Route("/api/v1/created", lambda r: JSONResponse({"id": 1}), methods=["POST"]),
        Route("/api/v1/stream", events),
        Route("/api/v1/json-stream", json_stream),
        Route("/api/v1/file", lambda r: FileResponse(kept, media_type="application/json")),
        Route("/api/v1/pdf", lambda r: Response(b"%PDF-1.4 stub", media_type="application/pdf")),
        Route("/api/v1/redirect", lambda r: RedirectResponse("/api/v1/things", status_code=302)),
        Route("/api/v1/missing", lambda r: JSONResponse({"message": "Not found"}, status_code=404)),
        Route("/api/v1/broken", lambda r: JSONResponse({"message": "Internal Server Error"}, status_code=500)),
        Route("/api/v1/page", lambda r: HTMLResponse("<p>hi</p>")),
        Route("/api/v1/csv", lambda r: PlainTextResponse("a,b\n1,2\n", media_type="text/csv")),
        Route("/api/v1/cookie", lambda r: JSONResponse({"ok": True}, headers={"Set-Cookie": "a=b"})),
        Route("/api/v1/no-store", lambda r: JSONResponse({"secret": 1}, headers={"Cache-Control": "no-store"})),
        Route("/api/v1/own-tag", lambda r: JSONResponse({"ok": True}, headers={"ETag": '"mine"'})),
        Route("/elsewhere/things", things),
    ]
    return ETagMiddleware(Starlette(routes=routes))


@pytest.fixture
async def mini(tmp_path, event_loop):
    # On the suite's one loop (tests/conftest.py event_loop), as the
    # database's fixtures are: without it pytest-asyncio starts a loop of its
    # own here, and every test after this one finds the engine on another.
    state = {"name": "Build Night", "gate": asyncio.Event()}
    app = _mini(state, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c, state, app


def _raw_scope(path: str, method: str = "GET", headers: tuple = ()) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "server": ("testserver", 80), "client": ("49.36.113.7", 1), "headers": [(b"host", b"testserver"), *headers],
    }


# ─── What gets a tag, and the 304 ────────────────────────────────────────


async def test_a_json_answer_gets_a_weak_tag_and_asking_with_it_is_a_304(mini):
    c, _, _ = mini
    first = await c.get("/api/v1/things")
    assert first.status_code == 200 and first.json() == {"items": ["Build Night"]}
    tag = first.headers["etag"]
    assert WEAK.fullmatch(tag) and tag == weak_etag(first.content)
    assert (await c.get("/api/v1/things")).headers["etag"] == tag  # the same bytes, the same tag

    again = await c.get("/api/v1/things", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    # What RFC 9110 asks a 304 to carry, as the 200 had it, and nothing else of the route's.
    assert again.headers["etag"] == tag
    assert again.headers["cache-control"] == "private, max-age=60"
    assert again.headers["vary"] == "Accept-Language"
    for gone in ("content-type", "content-length", "x-total"):
        assert gone not in again.headers


async def test_if_none_match_is_a_list_with_star_and_weak_comparison(mini):
    c, _, _ = mini
    tag = (await c.get("/api/v1/things")).headers["etag"]
    strong = tag[2:]
    assert matches(tag, tag) and matches(strong, tag) and matches(tag, strong)
    assert matches(f'"other", {tag}', tag) and matches("*", tag) and matches(' "a" ,*', tag)
    assert not matches(None, tag) and not matches("", tag) and not matches('"other"', tag)
    assert not matches(strong.replace('"', ""), tag)  # unquoted is not the tag

    for sent in (f'"other", {tag}', strong, "*", f'"x"\t,  {tag}'):
        assert (await c.get("/api/v1/things", headers={"If-None-Match": sent})).status_code == 304, sent
    # Two If-None-Match lines are one list.
    two = await c.get("/api/v1/things", headers=[("If-None-Match", '"other"'), ("If-None-Match", tag)])
    assert two.status_code == 304
    missed = await c.get("/api/v1/things", headers={"If-None-Match": '"other", W/"0123"'})
    assert missed.status_code == 200 and missed.json() == {"items": ["Build Night"]} and missed.headers["etag"] == tag


async def test_a_changed_answer_is_a_200_with_a_new_tag(mini):
    c, state, _ = mini
    old = (await c.get("/api/v1/things")).headers["etag"]
    state["name"] = "Build Night II"
    r = await c.get("/api/v1/things", headers={"If-None-Match": old})
    assert r.status_code == 200 and r.json() == {"items": ["Build Night II"]}
    assert WEAK.fullmatch(r.headers["etag"]) and r.headers["etag"] != old
    assert (await c.get("/api/v1/things", headers={"If-None-Match": r.headers["etag"]})).status_code == 304


async def test_head_gets_the_gets_tag_and_its_304(mini):
    c, _, _ = mini
    get = await c.get("/api/v1/things")
    head = await c.head("/api/v1/things")
    assert head.status_code == 200 and head.content == b"" and head.headers["etag"] == get.headers["etag"]
    assert head.headers["content-length"] == get.headers["content-length"]
    again = await c.head("/api/v1/things", headers={"If-None-Match": get.headers["etag"]})
    assert again.status_code == 304 and again.headers["etag"] == get.headers["etag"]


async def test_a_body_sent_in_pieces_is_hashed_whole(mini):
    c, _, _ = mini
    r = await c.get("/api/v1/pieces")
    assert r.status_code == 200 and r.json() == {"items": ["one", "two", "three"]}
    assert r.headers["etag"] == weak_etag(b'{"items": ["one", "two", "three"]}')
    assert (await c.get("/api/v1/pieces", headers={"If-None-Match": r.headers["etag"]})).status_code == 304


async def test_the_cache_control_a_route_set_is_what_goes_out(mini):
    """Private stays private and public stays public, on the 200 and the 304;
    a route that set none gets none. A tag never makes an answer shared."""
    c, _, _ = mini
    for path, expected in (("/api/v1/things", "private, max-age=60"), ("/api/v1/public", "public, max-age=300"), ("/api/v1/bare", None)):
        first = await c.get(path)
        again = await c.get(path, headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304
        for r in (first, again):
            assert r.headers.get("cache-control") == expected, path
            assert r.headers.get_list("cache-control") == ([expected] if expected else [])


# ─── What is left alone ──────────────────────────────────────────────────


async def test_files_redirects_errors_and_other_answers_are_left_alone(mini, monkeypatch):
    """No tag added and never a 304, even to `If-None-Match: *`: the body
    and headers are the route's own."""
    c, _, _ = mini
    cases = {
        "/api/v1/file": 200,  # a file: Starlette's own strong tag, untouched
        "/api/v1/pdf": 200,
        "/api/v1/redirect": 302,
        "/api/v1/missing": 404,
        "/api/v1/broken": 500,
        "/api/v1/page": 200,
        "/api/v1/csv": 200,
        "/api/v1/cookie": 200,
        "/api/v1/no-store": 200,
        "/api/v1/own-tag": 200,
        "/api/v1/json-stream": 200,  # JSON with no Content-Length: streamed
        "/elsewhere/things": 200,  # not the API
    }
    for path, status in cases.items():
        for headers in ({}, {"If-None-Match": "*"}):
            r = await c.get(path, headers=headers, follow_redirects=False)
            assert r.status_code == status, path
            assert not WEAK.fullmatch(r.headers.get("etag", "")), path
    filed = (await c.get("/api/v1/file")).headers["etag"]
    assert filed.startswith('"') and not filed.startswith("W/")  # Starlette's, from the file's size and time
    assert (await c.get("/api/v1/own-tag", headers={"If-None-Match": '"mine"'})).headers["etag"] == '"mine"'
    assert "etag" not in (await c.get("/api/v1/pdf")).headers
    assert "etag" not in (await c.post("/api/v1/created", headers={"If-None-Match": "*"})).headers

    # Larger than MAX_BODY: sent untagged, not hashed.
    monkeypatch.setattr(etag_middleware, "MAX_BODY", 10)
    big = await c.get("/api/v1/things", headers={"If-None-Match": "*"})
    assert big.status_code == 200 and "etag" not in big.headers and big.json() == {"items": ["Build Night"]}


async def test_a_stream_passes_through_untagged_frame_by_frame(mini):
    """text/event-stream: the start and each frame go out as they are sent,
    never held for a hash, whatever If-None-Match says."""
    _, state, app = mini
    sent: asyncio.Queue = asyncio.Queue()
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "http.request", "body": b"", "more_body": False})
    scope = _raw_scope("/api/v1/stream", headers=((b"if-none-match", b"*"),))
    task = asyncio.get_running_loop().create_task(app(scope, inbox.get, sent.put))
    start = await asyncio.wait_for(sent.get(), 5)
    assert start["type"] == "http.response.start" and start["status"] == 200
    names = [k for k, _ in start["headers"]]
    assert b"etag" not in names and b"content-length" not in names
    first = await asyncio.wait_for(sent.get(), 5)
    assert first["body"] == b"event: ping\n\n" and first["more_body"] is True
    assert sent.empty() and not task.done()  # the generator is still waiting
    state["gate"].set()
    assert (await asyncio.wait_for(sent.get(), 5))["body"].startswith(b"event: snapshot")
    await asyncio.wait_for(task, 5)


async def test_with_the_flag_off_every_answer_is_as_before(mini, monkeypatch):
    c, _, app = mini
    tag = (await c.get("/api/v1/things")).headers["etag"]
    monkeypatch.setattr(settings, "ETAGS", False)
    async with AsyncClient(transport=ASGITransport(app=app.app), base_url="http://testserver") as bare:
        for path in ("/api/v1/things", "/api/v1/public", "/api/v1/pieces"):
            off = await c.get(path, headers={"If-None-Match": tag})
            before = await bare.get(path, headers={"If-None-Match": tag})
            assert off.status_code == before.status_code == 200
            assert off.content == before.content and list(off.headers.items()) == list(before.headers.items())
            assert "etag" not in off.headers


# ─── The real app ────────────────────────────────────────────────────────


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="etags@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime.now(timezone.utc) + timedelta(hours=1), duration=3.0,
    )
    a = await make_user(email="etags-a@example.com")
    b = await make_user(email="etags-b@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", is_attended=True)
    await session.commit()
    return {
        "event": event, "a": r_a, "b": r_b,
        "a_user": auth_header(await mint_token(a)), "b_user": auth_header(await mint_token(b)),
        "club": auth_header(await mint_token(owner)),
    }


async def test_the_apps_tickets_list_is_tagged_and_revalidated_until_it_changes(client, session, world):
    url = "/api/v1/events/list?is_registered=true&offset=0&limit=100"
    first = await client.get(url, headers=world["a_user"])
    assert first.status_code == 200 and first.json()["items"][0]["ticket_id"] == world["a"].ticket_id
    tag = first.headers["etag"]
    assert WEAK.fullmatch(tag)
    # A personal answer: no Cache-Control before, none now (none is added).
    assert "cache-control" not in first.headers

    again = await client.get(url, headers={**world["a_user"], "If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    assert "cache-control" not in again.headers
    # Another person's list is their own body, and their own tag.
    theirs = await client.get(url, headers={**world["b_user"], "If-None-Match": tag})
    assert theirs.status_code == 200 and theirs.headers["etag"] != tag

    world["event"].name = "Build Night II"
    await session.commit()
    changed = await client.get(url, headers={**world["a_user"], "If-None-Match": tag})
    assert changed.status_code == 200 and changed.json()["items"][0]["name"] == "Build Night II"
    assert WEAK.fullmatch(changed.headers["etag"]) and changed.headers["etag"] != tag


async def test_a_304_carries_cors_and_cors_exposes_the_tag(client):
    origin = settings.cors_origins[0]
    first = await client.get("/api/v1/interests/topics", headers={"Origin": origin})
    assert "ETag" in [h.strip() for h in first.headers["access-control-expose-headers"].split(",")]
    again = await client.get("/api/v1/interests/topics", headers={"Origin": origin, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["access-control-allow-origin"] == origin
    assert again.headers["access-control-allow-credentials"] == "true"
    assert again.headers["access-control-expose-headers"] == first.headers["access-control-expose-headers"]
    # The public answer's Cache-Control, as the route set it, on both.
    assert first.headers["cache-control"] == again.headers["cache-control"] == "public, max-age=300"
    # And the middlewares outside: the security headers and the time.
    assert again.headers["x-frame-options"] == "DENY" and float(again.headers["x-process-time-ms"]) >= 0
    # A preflight for a request carrying If-None-Match is allowed.
    pre = await client.options(
        "/api/v1/interests/topics",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "if-none-match"},
    )
    assert pre.status_code == 200 and pre.headers["access-control-allow-headers"].lower() == "if-none-match"


async def test_with_request_ids_on_a_304_carries_its_id(client, monkeypatch):
    from doth import application

    monkeypatch.setattr(settings, "LOG_JSON", True)
    application.middleware_stack = None  # built again, now with request ids
    try:
        first = await client.get("/api/v1/interests/topics", headers={"X-Request-ID": "etag-first-12345"})
        assert first.headers["x-request-id"] == "etag-first-12345"
        again = await client.get(
            "/api/v1/interests/topics",
            headers={"X-Request-ID": "etag-again-12345", "If-None-Match": first.headers["etag"]},
        )
        assert again.status_code == 304 and again.headers["x-request-id"] == "etag-again-12345"
    finally:
        application.middleware_stack = None  # and again without them


async def test_errors_are_not_tagged_and_never_a_304(client, world):
    for method, path, headers, status in (
        ("GET", "/api/v1/events/list?is_registered=true", {}, 401),
        ("GET", "/api/v1/no-such-thing", {}, 404),
        ("GET", f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", world["b_user"], 403),
        ("GET", "/api/v1/events/list?limit=nope", world["a_user"], 422),
        # FastAPI answers HEAD on a GET route with 405: left as it is.
        ("HEAD", "/api/v1/interests/topics", {}, 405),
    ):
        r = await client.request(method, path, headers={**headers, "If-None-Match": "*"})
        assert r.status_code == status, (path, r.status_code)
        assert "etag" not in r.headers, path


async def test_the_live_stream_is_untouched_and_the_rooms_poll_is_tagged(client, world, streams):
    """The stream: no tag, no length, frames as they come, whatever
    If-None-Match says (the live stream's own tests read it through the
    whole stack). The poll beside it is JSON: a tag, and a 304 while the
    room is as it was."""
    room = f"/api/v1/events/{world['event'].id}/live"
    key = {"x-forwarded-for": "49.36.113.7", "X-Live-Key": "browser-00000001"}
    s = await SSE(f"{room}/stream", {**key, "If-None-Match": "*"}, {"view": "room"}).open()
    assert s.status == 200 and s.response_headers["content-type"].startswith("text/event-stream")
    assert "etag" not in s.response_headers and "content-length" not in s.response_headers
    assert s.response_headers["cache-control"] == "no-cache, no-transform"
    assert (await s.frame())["retry"] == "3000"
    await streams.step()
    assert (await s.next_of("snapshot"))["json"] is not None
    await s.close()

    poll = await client.get(room, headers=key)
    assert poll.status_code == 200 and WEAK.fullmatch(poll.headers["etag"])
    assert (await client.get(room, headers={**key, "If-None-Match": poll.headers["etag"]})).status_code == 304


# ─── The documents ───────────────────────────────────────────────────────


@pytest.fixture
def drawn(monkeypatch):
    """The HTML renders stubbed and counted, and every trip to the cache counted."""
    from app.api.events import certificates, doc_cache, tickets

    calls: list = []

    def pdf(template_path, context, options=None, template_dir=None):
        calls.append(context["email_payload"]["participant_name"])
        return io.BytesIO(b"%PDF-1.4 " + context["email_payload"]["participant_name"].encode())

    monkeypatch.setattr(tickets, "generate_pdf_bytes", pdf)
    monkeypatch.setattr(certificates, "generate_pdf_bytes", pdf)
    real = doc_cache.cached
    trips: list = []

    async def cached(key, draw):
        trips.append(key)
        return await real(key, draw)

    monkeypatch.setattr(doc_cache, "cached", cached)
    return {"calls": calls, "trips": trips}


async def test_the_emailed_file_link_is_untouched(client, world, drawn):
    """/api/v1/files/tickets/<id>.pdf: a PDF, so no tag from the middleware,
    and the route sets none of its own."""
    from app.core.mail import links

    parts = urlsplit(links.file_url("ticket", world["a"].ticket_id))
    r = await client.get(f"{parts.path}?{parts.query}", headers={"If-None-Match": "*"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.4 Ananya Iyer" and "etag" not in r.headers
