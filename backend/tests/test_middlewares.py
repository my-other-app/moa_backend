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
