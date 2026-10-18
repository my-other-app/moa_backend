"""The app's own middlewares, plain ASGI (app/core/middlewares/): every
response carries the security headers and its time to first byte, whatever
answered it, and a streamed body passes through them frame by frame."""


async def test_every_response_has_the_security_headers_and_its_time(client):
    for path, status in (("/api/v1/interests/topics", 200), ("/api/v1/no-such-thing", 404)):
        r = await client.get(path)
        assert r.status_code == status
        assert r.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert float(r.headers["x-process-time-ms"]) >= 0
        # Once each: written into the response's start, not appended to it.
        assert r.headers.get_list("x-frame-options") == ["DENY"]


async def test_a_streamed_body_passes_through_frame_by_frame():
    """Each chunk the app sends reaches the server as it is sent, not held by
    a middleware's task and memory stream (what BaseHTTPMiddleware did)."""
    import asyncio

    from fastapi.responses import StreamingResponse

    from doth import application

    gate = asyncio.Event()

    async def body():
        yield b"first"
        await gate.wait()
        yield b"second"

    path = "/__test/stream-through"

    @application.get(path)
    async def _stream():
        return StreamingResponse(body(), media_type="text/plain")

    try:
        sent: asyncio.Queue = asyncio.Queue()
        inbox: asyncio.Queue = asyncio.Queue()
        await inbox.put({"type": "http.request", "body": b"", "more_body": False})
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "server": ("testserver", 80), "client": ("49.36.113.7", 1), "headers": [(b"host", b"testserver")],
        }
        task = asyncio.get_running_loop().create_task(application(scope, inbox.get, sent.put))
        start = await asyncio.wait_for(sent.get(), 5)
        assert start["type"] == "http.response.start" and (b"x-frame-options", b"DENY") in start["headers"]
        first = await asyncio.wait_for(sent.get(), 5)
        assert first["body"] == b"first" and first["more_body"] is True
        assert sent.empty()
        gate.set()
        assert (await asyncio.wait_for(sent.get(), 5))["body"] == b"second"
        await asyncio.wait_for(task, 5)
    finally:
        application.router.routes = [r for r in application.router.routes if getattr(r, "path", None) != path]


async def test_with_request_ids_on_a_stream_is_still_unbuffered_and_carries_its_id(monkeypatch):
    """The whole stack as production builds it with APP_LOG_JSON (or Sentry)
    on: the request-id middleware outside Starlette's error handler
    (doth.Application) around the plain ASGI ones. A streamed body's first
    frame still arrives before the generator has finished, the response's
    start carries the caller's X-Request-ID, and so does a 500."""
    import asyncio

    from fastapi.responses import StreamingResponse

    from app.config import settings
    from doth import application

    gate = asyncio.Event()

    async def body():
        yield b"first"
        await gate.wait()
        yield b"second"

    path, boom = "/__test/stream-with-id", "/__test/boom-with-id"

    @application.get(path)
    async def _stream():
        return StreamingResponse(body(), media_type="text/event-stream")

    @application.get(boom)
    async def _boom():
        raise RuntimeError("boom")

    def scope_for(p: str, request_id: str) -> dict:
        return {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": p, "raw_path": p.encode(), "query_string": b"", "root_path": "",
            "server": ("testserver", 80), "client": ("49.36.113.7", 1),
            "headers": [(b"host", b"testserver"), (b"x-request-id", request_id.encode())],
        }

    monkeypatch.setattr(settings, "LOG_JSON", True)
    application.middleware_stack = None  # built again, now with request ids
    try:
        sent: asyncio.Queue = asyncio.Queue()
        inbox: asyncio.Queue = asyncio.Queue()
        await inbox.put({"type": "http.request", "body": b"", "more_body": False})
        task = asyncio.get_running_loop().create_task(application(scope_for(path, "stream-id-12345"), inbox.get, sent.put))
        start = await asyncio.wait_for(sent.get(), 5)
        assert start["type"] == "http.response.start" and start["status"] == 200
        assert dict(start["headers"])[b"x-request-id"] == b"stream-id-12345"
        assert (b"x-frame-options", b"DENY") in start["headers"]
        first = await asyncio.wait_for(sent.get(), 5)
        assert first["body"] == b"first" and first["more_body"] is True
        assert sent.empty() and not task.done()  # the generator is still waiting
        gate.set()
        assert (await asyncio.wait_for(sent.get(), 5))["body"] == b"second"
        await asyncio.wait_for(task, 5)

        sent = asyncio.Queue()
        inbox = asyncio.Queue()
        await inbox.put({"type": "http.request", "body": b"", "more_body": False})
        try:
            await asyncio.wait_for(application(scope_for(boom, "boom-id-123456"), inbox.get, sent.put), 5)
        except RuntimeError:
            pass  # Starlette's error handler answers the 500, then raises for the server to log
        start = await asyncio.wait_for(sent.get(), 5)
        assert start["status"] == 500 and dict(start["headers"])[b"x-request-id"] == b"boom-id-123456"
    finally:
        application.router.routes = [r for r in application.router.routes if getattr(r, "path", None) not in (path, boom)]
        application.middleware_stack = None  # and again without them, for the tests after


async def test_a_browser_may_read_retry_after_and_cors_is_otherwise_as_it_was(client, monkeypatch):
    """The live stream's 503 LIVE_STREAM_FULL carries Retry-After, which a
    page on another origin can read only when CORS exposes it; the rest of
    CORS (the origins, credentials, methods and headers) is unchanged."""
    from app.api.live import stream
    from app.config import settings

    origin = settings.cors_origins[0]
    r = await client.get("/api/v1/interests/topics", headers={"Origin": origin})
    assert r.headers["access-control-allow-origin"] == origin
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.headers["access-control-expose-headers"] == "Retry-After"
    pre = await client.options(
        "/api/v1/interests/topics",
        headers={"Origin": origin, "Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": "x-live-key, authorization"},
    )
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == origin
    assert "PATCH" in pre.headers["access-control-allow-methods"]
    assert pre.headers["access-control-allow-headers"].lower() == "x-live-key, authorization"
    # Not for an origin it does not know.
    assert "access-control-allow-origin" not in (await client.get("/api/v1/interests/topics", headers={"Origin": "https://elsewhere.example"})).headers

    # The 503 itself, readable from the page.
    monkeypatch.setattr(settings, "LIVE_SSE", True)

    def full(*_a, **_k):
        raise stream.Full("this server")

    monkeypatch.setattr(stream.hub, "admit", full)
    r = await client.get("/api/v1/events/1/live/stream", params={"view": "room"}, headers={"Origin": origin})
    assert r.status_code == 503 and r.json()["error_code"] == "LIVE_STREAM_FULL"
    assert r.headers["retry-after"] == "60" and r.headers["access-control-expose-headers"] == "Retry-After"
