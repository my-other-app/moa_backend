"""What the access log, the error reports and Sentry never carry from a URL
(app/core/redact.py, app/core/observability.py): the secret segment of a
link's path, the emailed links' signed parameters and ticket ids."""

import json
import logging

import pytest

from app.core import observability as obs
from app.core import redact

F = redact.FILTERED


@pytest.mark.parametrize(
    "logged, expected",
    [
        (
            "/api/v1/clubs/access/invite/AbCdEf123456789xyz",
            f"/api/v1/clubs/access/invite/{F}",
        ),
        (
            "/api/v1/clubs/access/invite/AbCdEf123456789xyz/accept",
            f"/api/v1/clubs/access/invite/{F}/accept",
        ),
        (
            "/api/v1/clubs/pending-actions/pa_9f8e7d6c5b4a/confirm",
            f"/api/v1/clubs/pending-actions/{F}/confirm",
        ),
        (
            "/api/v1/registration-requests/rq.AbCdEf-12_34",
            f"/api/v1/registration-requests/{F}",
        ),
        (
            "/api/v1/registration-requests/rq.AbCdEf-12_34/withdraw",
            f"/api/v1/registration-requests/{F}/withdraw",
        ),
        ("/api/v1/speakers/claim/Zx9_Yw8-Vu7", f"/api/v1/speakers/claim/{F}"),
        ("/api/v1/support/survey/s3cr3tSurveyToken", f"/api/v1/support/survey/{F}"),
        ("/api/v1/tickets/cancel/eyJpZCI6MX0.sig", f"/api/v1/tickets/cancel/{F}"),
        (
            "/api/v1/waitlist/offers/offer-token-123/claim",
            f"/api/v1/waitlist/offers/{F}/claim",
        ),
        ("/api/v1/profiles/by-code/K3Q9PZ2XWD", f"/api/v1/profiles/by-code/{F}"),
        (
            "/api/v1/social/data-deletion/0a1b2c3d4e5f",
            f"/api/v1/social/data-deletion/{F}",
        ),
        (
            "/api/v1/files/tickets/MOA-7K3QF-X9PWM.pdf?s=abc123",
            f"/api/v1/files/tickets/{redact.TICKET}.pdf?s={F}",
        ),
        (
            "/api/v1/files/certificates/AbCdEfGhIjKl.pdf?s=abc123",
            f"/api/v1/files/certificates/AbCdEfGhIjKl.pdf?s={F}",
        ),
        (
            "/api/v1/events/tickets/MOA-18F3A2B4C5D-ABCDE/pdf",
            f"/api/v1/events/tickets/{redact.TICKET}/pdf",
        ),
        (
            "/api/v1/events/42/answers/q0b6f3e1c?k=3f9a1c",
            f"/api/v1/events/42/answers/q0b6f3e1c?k={F}",
        ),
        (
            "/api/v1/events/42/feedback/abc?k=3f9a1c&x=1",
            f"/api/v1/events/42/feedback/abc?k={F}&x=1",
        ),
        ("/recap/unsubscribe?u=7&t=deadbeef", f"/recap/unsubscribe?u=7&t={F}"),
        (
            "/partner/v1/tickets/MOA-7K3QF-X9PWM/qr.png?sig=a.b.c",
            f"/partner/v1/tickets/{redact.TICKET}/qr.png?sig={F}",
        ),
        (
            "GET /api/v1/developers/access?t=moa_docs_xyz HTTP/1.1",
            f"GET /api/v1/developers/access?t={F} HTTP/1.1",
        ),
    ],
)
def test_secrets_in_paths_and_links_are_filtered(logged, expected):
    assert redact.path(logged) == expected


@pytest.mark.parametrize(
    "logged",
    [
        "/api/v1/registration-requests/mine",
        "/api/v1/events/42/registrations?sort=name&status=approved",
        "/api/v1/discover/events?q=music&size=20",
        "/api/v1/clubs/access/invite",
        "/api/v1/events/info/42",
    ],
)
def test_ordinary_paths_are_kept(logged):
    assert redact.path(logged) == logged


def test_the_access_line_has_no_link_secret():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        (
            "1.2.3.4:5",
            "GET",
            "/api/v1/waitlist/offers/SeCrEtOfFeR123/claim?s=xyz",
            "1.1",
            200,
        ),
        None,
    )
    assert redact.AccessLogFilter().filter(record)
    line = record.getMessage()
    assert "SeCrEtOfFeR123" not in line and "xyz" not in line and "/claim" in line


def test_sentry_events_have_no_ticket_id_link_secret_or_ai_key():
    gemini = "AIza" + "S" * 35
    event = obs.scrub_event(
        {
            "request": {
                "method": "GET",
                "url": "https://api.myotherapp.in/api/v1/events/tickets/MOA-7K3QF-X9PWM/pdf",
                "query_string": "s=SiGnAtUrE&page=2",
            },
            "exception": {
                "values": [
                    {
                        "type": "RuntimeError",
                        "value": f"render of MOA-7K3QF-X9PWM failed with {gemini}",
                    }
                ]
            },
            "breadcrumbs": {
                "values": [{"message": "GET /api/v1/tickets/cancel/CaNcElToKeN?k=abc"}]
            },
            "tags": {"ticket": "MOA-7K3QF-X9PWM"},
        }
    )
    flat = json.dumps(event)
    for leaked in ("MOA-7K3QF-X9PWM", "SiGnAtUrE", gemini, "CaNcElToKeN", "k=abc"):
        assert leaked not in flat, leaked
    assert "page=2" in event["request"]["query_string"]


def test_every_credential_setting_is_scrubbed(monkeypatch):
    from app.config import settings

    values = {
        "GEMINI_API_KEY": "gemini-" + "g" * 20,
        "APPLE_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----apple-" + "a" * 20,
        "MAILBOX_HOOK_SECRET": "mailbox-" + "m" * 20,
        "LINK_SIGNING_KEYS": "first-" + "1" * 30 + ",second-" + "2" * 30,
        "META_WEBHOOK_VERIFY_TOKEN": "meta-" + "v" * 20,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    obs._own_secrets.cache_clear()
    try:
        text = obs.scrub_text(" ".join(values.values()) + " " + "second-" + "2" * 30)
        rate = obs.scrub_text(settings.AUTH_PASSWORD_PER_ADDRESS)
    finally:
        obs._own_secrets.cache_clear()
    for value in values.values():
        for part in value.split(","):
            assert part not in text, part
    assert rate == settings.AUTH_PASSWORD_PER_ADDRESS
