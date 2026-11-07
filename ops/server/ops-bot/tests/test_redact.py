"""Nothing secret reaches Telegram: one case per pattern."""

import pytest

from opsbot.redact import MAX_CHARS, redact, scrub_secret


@pytest.mark.parametrize(
    "text, secret",
    [
        # Bearer tokens and credential headers
        ("Authorization: Bearer abcdefghijklmnop.qrs", "abcdefghijklmnop"),
        ("calling with bearer sk_live_0123456789abcdef", "sk_live_0123456789abcdef"),
        ("cookie: moa_session=abcdef0123456789", "abcdef0123456789"),
        ("x-api-key: k-1234567890", "k-1234567890"),
        # JWTs, anywhere
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl in a line", "eyJzdWIiOiIxMjMifQ"),
        ("refresh=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9", "eyJleHAiOjF9"),
        # Query parameters
        ("GET /api/v1/x?token=tok-123&page=2", "tok-123"),
        ("GET /x?key=key-456", "key-456"),
        ("GET /x?a=1&secret=sec-789", "sec-789"),
        ("GET /x?password=pw-000", "pw-000"),
        ("GET /api/v1/r?s=sig-abc", "sig-abc"),
        ("GET /api/v1/r?id=3&k=kk-def", "kk-def"),
        ("GET /oauth?access_token=at-111&x=1", "at-111"),
        ("GET /x?api_key=ak-222", "ak-222"),
        # key=value / JSON in log text
        ("connecting password=hunter2 user=moa", "hunter2"),
        ('{"client_secret": "cs-333", "user": "x"}', "cs-333"),
        ("APP_SECRET_KEY=super-secret-value", "super-secret-value"),
        # Capability links: the token after the prefix
        ("GET /api/v1/tickets/TkT0kEn123/pdf HTTP/1.1", "TkT0kEn123"),
        ("GET /api/v1/certificates/CeRt-tok/download", "CeRt-tok"),
        ("POST /api/v1/registration-requests/rr-tok-9/confirm", "rr-tok-9"),
        ("GET /api/v1/waitlist/offers/offer-tok/accept", "offer-tok"),
        ("GET /api/v1/clubs/access/invite/inv-tok-1", "inv-tok-1"),
        ("POST /api/v1/check-in/ci-tok-2", "ci-tok-2"),
        ("https://myotherapp.in/tickets/web-tok?x=1", "web-tok"),
        # Ticket codes, new and old
        ("ticket MOA-7K3QF-X9PWM checked in", "7K3QF-X9PWM"),
        ("legacy MOA-1a2b3c-QX7RT", "1a2b3c-QX7RT"),
        # UUIDs
        ("user 123e4567-e89b-12d3-a456-426614174000 did it", "123e4567-e89b-12d3-a456-426614174000"),
        ("USER 123E4567-E89B-12D3-A456-426614174000", "123E4567-E89B-12D3-A456-426614174000"),
        # Email addresses
        ("sent to someone.name+tag@example.co.in", "someone.name+tag@example.co.in"),
        ("GET /x?email=a.b%40gmail.com", "gmail.com"),
        # Phone numbers
        ("call +91 98765 43210 now", "98765 43210"),
        ("whatsapp to +919876543210", "9876543210"),
        ("phone 9876543210 saved", "9876543210"),
        ("phone 09876543210 saved", "9876543210"),
        ("intl +1-202-555-0143", "202-555-0143"),
        ("GET /x?phone=%2B919876543210", "919876543210"),
        # URL credentials and bot tokens
        ("postgresql://moa:hunter2-pw@postgres:5432/moa", "hunter2-pw"),
        ("https://api.telegram.org/bot123456789:TEST_not_a_real_token_000000000000/sendMessage", "TEST_not_a_real_token"),
    ],
)
def test_every_pattern_is_masked(text, secret):
    out = redact(text)
    assert secret not in out, out


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-08 03:00:00,123 ERROR app.main: boom (took 1234 ms)",
        "KeyError: 'club_id'",
        'INFO: 172.18.0.5:41234 - "GET /api/v1/events HTTP/1.1" 500 Internal Server Error',
        "backup-20261107-213000.dump (1234567 bytes)",
    ],
)
def test_ordinary_lines_stay_readable(text):
    assert redact(text) == text


def test_the_path_around_a_token_is_kept():
    out = redact('"GET /api/v1/tickets/TkT0kEn123/pdf?token=t1&page=2 HTTP/1.1" 500')
    assert out == '"GET /api/v1/tickets/***/pdf?token=***&page=2 HTTP/1.1" 500'


def test_length_is_capped_keeping_the_end():
    text = "x" * 10000 + "THE END"
    out = redact(text)
    assert len(out) <= MAX_CHARS and out.endswith("THE END")
    assert len(redact("y" * 1000, 300)) <= 300


def test_scrub_secret_removes_a_known_value():
    assert scrub_secret("HTTP Error for https://host/botSEKRIT/x", "SEKRIT") == "HTTP Error for https://host/bot***/x"
    assert redact("") == ""
