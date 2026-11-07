"""The client: retries with backoff, honours 429, caps and rate-limits."""

from conftest import TOKEN, FakeTelegram

from opsbot.telegram import MESSAGE_LIMIT, RateLimiter


def test_429_and_5xx_are_retried_with_retry_after():
    slept = []
    telegram = FakeTelegram(
        answers=[
            {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}},
            {"ok": False, "error_code": 502, "description": "Bad Gateway"},
            OSError("connection reset"),
            {"ok": True, "result": {"message_id": 1}},
        ]
    )
    telegram.sleep = slept.append
    assert telegram.send_message("hello") is True
    assert len(telegram.requests) == 4 and slept[0] == 7 and all(s <= 60 for s in slept)


def test_a_4xx_is_not_retried_and_send_message_never_raises():
    telegram = FakeTelegram(answers=[{"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}])
    assert telegram.send_message("<b>broken") is False
    assert len(telegram.requests) == 1


def test_gives_up_after_the_retries():
    telegram = FakeTelegram(answers=[OSError("down")] * 10)
    assert telegram.send_message("x") is False
    assert len(telegram.requests) == 4  # one try and three retries


def test_long_messages_are_cut_to_telegrams_limit():
    telegram = FakeTelegram()
    telegram.send_message("<pre>" + "x" * 10000)
    text = telegram.texts[0]
    assert len(text) <= MESSAGE_LIMIT + len("</pre>") and text.endswith("</pre>")


def test_the_rate_limit_drops_and_then_says_so():
    clock = [0.0]
    limiter = RateLimiter(min_interval=0, per_window=3, window=60, clock=lambda: clock[0], sleep=lambda s: None)
    telegram = FakeTelegram()
    telegram.limiter = limiter
    results = [telegram.send_message(f"m{i}") for i in range(6)]
    assert results == [True, True, True, False, False, False]
    clock[0] = 61
    assert telegram.send_message("later")
    assert telegram.texts[-1].startswith("(3 earlier message(s) dropped")


def test_min_interval_spaces_out_sends():
    slept = []
    clock = [100.0]
    limiter = RateLimiter(min_interval=1.1, per_window=50, clock=lambda: clock[0], sleep=slept.append)
    assert limiter.acquire() and limiter.acquire()
    assert slept == [1.1]


def test_get_updates_long_polls_without_retrying():
    telegram = FakeTelegram(answers=[{"ok": True, "result": [{"update_id": 1}]}])
    assert telegram.get_updates(5) == [{"update_id": 1}]
    method, params = telegram.requests[0]
    assert method == "getUpdates" and params["offset"] == 5 and params["timeout"] == 50


def test_the_token_is_only_in_the_url():
    telegram = FakeTelegram()
    assert TOKEN in telegram._url("sendMessage")
    telegram.send_message("x")
    assert TOKEN not in repr(telegram.requests)


def test_the_multipart_upload_is_well_formed(tmp_path, monkeypatch):
    """The real _post_multipart, against a server on localhost."""
    import email.parser
    import http.client
    import http.server
    import json
    import threading

    from opsbot import telegram as tg

    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["type"] = self.headers["Content-Type"]
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"ok": True, "result": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(
        tg.http.client, "HTTPSConnection", lambda host, timeout, context: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    )

    payload = tmp_path / "backup-x.dump.age"
    payload.write_bytes(tg.AGE_HEADER + bytes(range(256)) * 400)
    client = tg.Telegram(TOKEN, 42, limiter=RateLimiter(min_interval=0))
    client.send_document(payload, "<b>caption</b> · ok", filename=payload.name)
    server.server_close()

    assert got["path"] == f"/bot{TOKEN}/sendDocument"
    message = email.parser.BytesParser().parsebytes(b"Content-Type: " + got["type"].encode() + b"\r\n\r\n" + got["body"])
    parts = {part.get_param("name", header="content-disposition"): part for part in message.get_payload()}
    assert parts["chat_id"].get_payload() == "42"
    assert parts["caption"].get_payload(decode=True).decode() == "<b>caption</b> · ok"
    assert parts["document"].get_filename() == "backup-x.dump.age"
    assert parts["document"].get_payload(decode=True) == payload.read_bytes()
