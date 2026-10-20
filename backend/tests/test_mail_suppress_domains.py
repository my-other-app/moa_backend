"""Addresses at a suppressed domain (APP_MAIL_SUPPRESS_DOMAINS: the demo's
invented people at demo.myotherapp.in) are never mailed and never pushed.

Every test takes the session, used or not (see tests/test_demo_seed.py: a
run whose first test takes no database fixture puts later tests on another
event loop)."""
import pytest
from sqlalchemy import select

from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core.email import transport
from app.core.mail import suppression
from app.core.notifications import service as push
from app.core.notifications import topics


@pytest.fixture
def providers(monkeypatch):
    """The providers, standing in: what each was asked to send."""
    sent = []
    monkeypatch.setattr(transport, "_resend_available", lambda: False)
    monkeypatch.setattr(transport, "_send_ses", lambda mail: sent.append(mail) or "ses-id")
    return sent


def _mail(to, bcc=()):
    return transport.Mail(to=list(to), bcc=list(bcc), subject="Hello", text="x", check_suppression=False)


async def test_default_domain_and_both_setting_forms(session, monkeypatch):
    assert "demo.myotherapp.in" in settings.mail_suppress_domains
    monkeypatch.setattr(settings, "MAIL_SUPPRESS_DOMAINS", "Example.TEST, @other.test ,")
    assert settings.mail_suppress_domains == frozenset({"example.test", "other.test"})
    monkeypatch.setattr(settings, "MAIL_SUPPRESS_DOMAINS", ["a.test"])
    assert settings.mail_suppress_domains == frozenset({"a.test"})


async def test_an_address_at_the_domain_or_below_is_suppressed(session):
    assert suppression.at_suppressed_domain("anjali.menon.4@demo.myotherapp.in")
    assert suppression.at_suppressed_domain("Hello@Respawn-Room.DEMO.myotherapp.in ")
    assert not suppression.at_suppressed_domain("someone@myotherapp.in")
    assert not suppression.at_suppressed_domain("demo.myotherapp.in@gmail.com")
    assert not suppression.at_suppressed_domain(None)


async def test_a_mail_only_to_demo_people_is_not_sent(session, providers):
    out = transport.deliver(_mail(["a@demo.myotherapp.in", "b@club.demo.myotherapp.in"]))
    assert out == {"MessageId": None, "Provider": "suppressed"}
    assert providers == []


async def test_demo_people_are_left_out_of_a_mail_to_others(session, providers):
    transport.deliver(_mail(["real@example.com", "a@demo.myotherapp.in"], bcc=["b@demo.myotherapp.in", "c@example.com"]))
    assert len(providers) == 1
    assert providers[0].to == ["real@example.com"] and providers[0].bcc == ["c@example.com"]


async def test_bcc_only_real_people_still_get_it(session, providers):
    transport.deliver(_mail(["a@demo.myotherapp.in"], bcc=["c@example.com", "d@example.com"]))
    assert providers[0].to == ["c@example.com"] and providers[0].bcc == ["d@example.com"]


async def test_nothing_is_suppressed_with_the_setting_empty(session, providers, monkeypatch):
    monkeypatch.setattr(settings, "MAIL_SUPPRESS_DOMAINS", "")
    transport.deliver(_mail(["a@demo.myotherapp.in"]))
    assert providers[0].to == ["a@demo.myotherapp.in"]


async def test_audiences_and_deliveries_skip_demo_addresses(session):
    found = await suppression.reasons(session, ["x@demo.myotherapp.in", "y@example.com"])
    assert set(found) == {"x@demo.myotherapp.in"}
    assert found["x@demo.myotherapp.in"].reason == suppression.DOMAIN
    assert "demo address" in suppression.why(found["x@demo.myotherapp.in"])
    from sqlalchemy import literal

    keep = await session.scalar(select(suppression.not_suppressed(literal("y@example.com"))))
    drop = await session.scalar(select(suppression.not_suppressed(literal("x@sub.demo.myotherapp.in"))))
    assert keep is True and drop is False


async def test_demo_people_are_never_pushed(session, make_user):
    real = await make_user(email="real@example.com")
    fake = await make_user(email="fake.person.1@demo.myotherapp.in")
    session.add_all([
        UserDeviceTokens(user_id=real.id, fcm_token="tok-real", platform="android"),
        UserDeviceTokens(user_id=fake.id, fcm_token="tok-fake", platform="ios"),
    ])
    await session.flush()
    assert await push.get_fcm_tokens_for_users(session, [real.id, fake.id]) == ["tok-real"]
    assert await push.get_fcm_token_for_user(session, fake.id) is None
    # Their devices join no topic either: a topic send cannot be filtered here.
    assert await topics.topics_for(session, fake.id) == []
