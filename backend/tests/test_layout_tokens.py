"""The ticket template language, ported case for case from the contract's
own tests -- so the Python side fills a template exactly as the editor does."""
from datetime import datetime, timezone

from app.core.layout.tokens import (
    TokenAttendee,
    TokenEvent,
    canonical_token,
    carries_per_guest_token,
    field_token,
    parse_ticket_template,
    render_ticket_template,
    ticket_token_values,
)

VALUES = ticket_token_values(
    TokenEvent(
        name="Annual Summit", code="KF26", venue="Ravensbourne Hall", organiser=None,
        starts_at=datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc),
        timezone="Europe/London",
    ),
    "Kestrel Foundation",
    TokenAttendee(name="Amara Okonjo", organization=None, email="amara@x.co", custom_fields={"T-Shirt Size": "L"}),
    "ADM-0041-KF26",
    "VIP",
)


def test_fills_tokens_and_accepts_the_names_other_tools_use():
    assert render_ticket_template("{{name}} · {{ticket_code}} · {{event_title}}", VALUES) == "Amara Okonjo · ADM-0041-KF26 · Annual Summit"
    assert render_ticket_template("{{ attendee_name | upper }}", VALUES) == "AMARA OKONJO"
    assert render_ticket_template("{{event_day}} {{event_date}} {{event_time}}–{{event_end_time}}", VALUES) == "Wednesday 16 Sept 2026 09:00–18:00"
    assert render_ticket_template("Size {{field.t_shirt_size}}", VALUES) == "Size L"
    assert field_token("T-Shirt Size") == "field.t_shirt_size"
    assert canonical_token("Company") == "organization"


def test_shows_or_hides_a_block_on_whether_a_token_has_a_value():
    t = "{{event_start_time}}{% if {{event_end_time}} %} – {{event_end_time}}{% endif %}"
    assert render_ticket_template(t, VALUES) == "09:00 – 18:00"
    assert render_ticket_template(t, {**VALUES, "event_end_time": ""}) == "09:00"
    assert render_ticket_template("{% if organization %}{{organization}}{% else %}Independent{% endif %}", VALUES) == "Independent"
    assert render_ticket_template("{% if not seat %}No seat{% endif %}", VALUES) == "No seat"


def test_names_every_mistake_with_a_suggestion_rather_than_printing_blanks():
    assert "not a token. Did you mean {{name}}?" in parse_ticket_template("{{nmae}}").errors[0]
    assert "not a filter" in parse_ticket_template("{{name | shout}}").errors[0]
    assert "never closed" in parse_ticket_template("{% if name %}x").errors[0]
    assert "no {% if %}" in parse_ticket_template("{% endif %}").errors[0]
    assert "not understood" in parse_ticket_template("{% for x in y %}").errors[0]
    assert "empty" in parse_ticket_template("{{}}").errors[0]
    assert parse_ticket_template("Plain words and {{venue}}").errors == []
    assert parse_ticket_template("{{name}} {{company}}").tokens == ["attendee_name", "organization"]


def test_our_own_aliases_reach_the_same_keys():
    """What our earlier editor and templates called things keeps working."""
    assert canonical_token("participant_name") == "attendee_name"
    assert canonical_token("club_name") == "org_name"
    assert canonical_token("certificate_id") == "certificate_no"
    assert canonical_token("event_location") == "venue"
    assert canonical_token("role") == "ticket_type"


def test_a_custom_field_is_reachable_by_key_and_by_label():
    values = ticket_token_values(
        TokenEvent(name="E", code="X1", venue=None, organiser=None,
                   starts_at=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
                   ends_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc)),
        "Club",
        TokenAttendee(name="A B", custom_fields={"tshirt_size": "S"}),
        "MOA-0001-X1", "General",
        field_labels={"tshirt_size": "T-Shirt Size"},
    )
    assert values["field.tshirt_size"] == "S"
    assert values["field.t_shirt_size"] == "S"


def test_times_follow_the_events_clock():
    e = TokenEvent(name="E", code="X1", venue=None, organiser=None,
                   starts_at=datetime(2026, 3, 1, 3, 30, tzinfo=timezone.utc),
                   ends_at=datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc),
                   timezone="Asia/Kolkata", hour12=True)
    v = ticket_token_values(e, "C", TokenAttendee(name="X"), "N", "T")
    assert v["event_time"] == "9:00 AM"
    assert v["event_end_time"] == "6:00 PM"
    assert v["event_date"] == "1 Mar 2026"


def test_per_guest_text_is_recognised_however_it_is_written():
    assert carries_per_guest_token("{{name}}")
    assert carries_per_guest_token("Seat {{ seat_label }}")
    assert carries_per_guest_token("{{field.t_shirt_size}}")
    assert not carries_per_guest_token("{{event_name}} · {{venue}}")
