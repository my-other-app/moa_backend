"""An email from a design: the same markup the editor shows, the same words
in the plain-text twin, and nothing a guest's client could be tricked by."""
from app.core.layout.tokens import TokenEvent, sample_token_values
from app.core.mail.render import RenderInput, parse_email_template, render_email, sample_email_values, uses_qr
from app.core.mail.schema import JOURNEY_STAGES, EmailDesign, EmailTemplateFields
from app.core.mail.starters import email_starter
from datetime import datetime, timezone


def _values(**extra):
    event = TokenEvent(name="Build Night", code="E1", venue="Innovation Hall", organiser="Yukthi", starts_at=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), ends_at=datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc), timezone="Asia/Kolkata")
    v = sample_email_values(sample_token_values(event, "Yukthi", "ticket"), organiser_email="hello@yukthi.in", api_origin="https://api.test")
    return {**v, **extra}


def test_every_starter_is_a_valid_template_and_renders_both_bodies():
    for stage in [*JOURNEY_STAGES, "CUSTOM"]:
        fields = email_starter(stage)
        assert EmailTemplateFields.model_validate(fields.model_dump(by_alias=True))
        out = render_email(RenderInput(subject=fields.subject, design=fields.design, values=_values(), brand_primary="#1DB9A0", qr_url="cid:ticket-qr"))
        assert "<!DOCTYPE html>" in out.html and "Build Night" in out.html
        assert "Build Night" in out.text
        assert "{{" not in out.html and "{{" not in out.text


def test_the_invite_carries_the_guest_the_card_the_qr_and_the_ticket_link():
    fields = email_starter("INVITE")
    out = render_email(RenderInput(subject=fields.subject, design=fields.design, values=_values(), brand_primary="#1DB9A0", logo_url="https://cdn/logo.png", qr_url="cid:ticket-qr"))
    assert out.subject == "Your ticket for Build Night"
    assert "You’re in, Ananya." in out.html
    assert 'src="cid:ticket-qr"' in out.html
    assert 'href="https://api.test/api/v1/files/sample"' in out.html
    assert "background:#1DB9A0" in out.html  # the brand accent on the button
    assert 'src="https://cdn/logo.png"' in out.html
    assert "Download your ticket: https://api.test/api/v1/files/sample" in out.text
    assert uses_qr(fields.design)


def test_inline_markup_and_escaping():
    design = EmailDesign.model_validate({"blocks": [{"kind": "paragraph", "id": "p1", "text": "Hi **{{first_name}}**, see [the map](https://maps.example/x) <script>alert(1)</script> _soon_"}]})
    out = render_email(RenderInput(subject="s", design=design, values=_values(), brand_primary="#1DB9A0"))
    assert "<strong>Ananya</strong>" in out.html
    assert '<a href="https://maps.example/x"' in out.html
    assert "&lt;script&gt;" in out.html and "<script>" not in out.html
    assert "<em>soon</em>" in out.html
    assert "Hi Ananya, see the map (https://maps.example/x)" in out.text


def test_an_unsafe_link_is_dropped_not_sent():
    design = EmailDesign.model_validate({"blocks": [
        {"kind": "button", "id": "b1", "label": "Go", "href": "javascript:alert(1)"},
        {"kind": "paragraph", "id": "p1", "text": "[click](javascript:evil())"},
    ]})
    out = render_email(RenderInput(subject="s", design=design, values=_values(), brand_primary="#1DB9A0"))
    assert "javascript:" not in out.html
    assert 'href="#"' in out.html
    assert ">click<" in out.html or "click" in out.html


def test_the_editor_is_told_about_a_token_the_email_does_not_know():
    assert parse_email_template("Hello {{ticket_url}} {{organiser_email}}").errors == []
    errors = parse_email_template("Hello {{tikcet_url}}").errors
    assert errors and "not a token" in errors[0]


def test_a_conditional_hides_its_words_when_the_token_is_empty():
    fields = email_starter("POST_EVENT")
    with_cert = render_email(RenderInput(subject="s", design=fields.design, values=_values(), brand_primary="#1DB9A0"))
    without = render_email(RenderInput(subject="s", design=fields.design, values=_values(certificate_url=""), brand_primary="#1DB9A0"))
    assert "certificate of attendance" in with_cert.html
    assert "certificate of attendance" not in without.html
