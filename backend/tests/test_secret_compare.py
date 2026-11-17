"""Secrets are compared as bytes (app/core/auth/compare.py): text with
characters outside ASCII is a refusal, never a 500."""

import pytest

from app.core.auth.compare import same


@pytest.mark.parametrize(
    "given, expected, ok",
    [
        ("abc", "abc", True),
        (b"abc", "abc", True),
        ("abc", "abd", False),
        ("é", "abc", False),
        ("sha256=☃", "sha256=00", False),
        ("\ud800", "x", False),
        (None, "abc", False),
        ("abc", None, False),
        ("", "", True),
    ],
)
def test_same(given, expected, ok):
    assert same(given, expected) is ok


async def test_hooks_refuse_a_non_ascii_signature_or_token(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SES_WEBHOOK_TOKEN", "the-ses-token")
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret")
    r = await client.post(
        "/api/v1/hooks/ses", params={"token": "t\u00f6k\u00e9n"}, content=b"{}"
    )
    assert r.status_code < 500, r.text
    r = await client.get(
        "/api/v1/hooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "v\u00e9rify",
            "hub.challenge": "1",
        },
    )
    assert r.status_code < 500 and r.text != "1"
    r = await client.post(
        "/api/v1/hooks/whatsapp",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=\u00e9\u00e9".encode("latin-1")},
    )
    assert r.status_code < 500, r.text


def test_signed_payloads_and_mail_links_refuse_non_ascii():
    from app.core.auth.payloads import sign_payload, verify_payload
    from app.core.mail import links

    good = sign_payload({"k": "x"}, 60)
    body = good.split(".", 1)[0]
    assert verify_payload(good) == {"k": "x"} or verify_payload(good)["k"] == "x"
    assert verify_payload(f"{body}.üü") is None
    assert links.verify("ticket:MOA-1", links.sign("ticket:MOA-1")) is True
    assert links.verify("ticket:MOA-1", "ü") is False
