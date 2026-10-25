"""The platform number's payment-pending and event-update templates
(app/core/whatsapp.py): exactly what Meta is handed when one is sent, with
the stand-in for Meta from tests/test_whatsapp.py. No flow sends either yet;
these pin the message for the day one does."""
import pytest

from app.core import whatsapp
from tests.test_whatsapp import meta  # noqa: F401 (fixture)


async def test_a_payment_pending_message_names_the_amount_and_links_to_the_event(session, meta):  # noqa: F811
    t = whatsapp.payment_pending(name="Devika  Menon", event="Build Night\n2026", amount=whatsapp.rupees_text("123456.5"), slug="build night/x1")
    out = await whatsapp.send("+91 98765-43210", t)
    assert out == {"MessageId": "wamid.TEST1", "Provider": "whatsapp", "Address": "+919876543210", "Via": "platform"}
    [sent] = meta["messages"]
    assert sent["to"] == "919876543210" and sent["type"] == "template"
    template = sent["template"]
    assert template["name"] == "payment_pending" and template["language"]["code"]
    body, button = template["components"]
    # No header: this template carries no document.
    assert body == {"type": "body", "parameters": [{"type": "text", "text": v} for v in ("Devika", "Build Night 2026", "1,23,456.50")]}
    # The URL button's part is the event's slug, made safe for a path.
    assert button == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "build%20night%2Fx1"}]}
    assert len(meta["uploads"]) == 0


async def test_an_event_update_carries_the_change_in_one_line_and_at_most_500_characters(session, meta):  # noqa: F811
    change = "The hall moved to\n\nBlock B.\tDoors at 6.  " + "More detail. " * 60
    t = whatsapp.event_update(name="", club="Yukthi", event="Build Night", update=change)
    await whatsapp.send("9876543210", t)
    [sent] = meta["messages"]
    [body] = sent["template"]["components"]
    first, club, event, update = (p["text"] for p in body["parameters"])
    assert (first, club, event) == ("there", "Yukthi", "Build Night")
    assert update.startswith("The hall moved to Block B. Doors at 6. More detail.")
    assert "\n" not in update and "\t" not in update and "  " not in update
    assert 490 < len(update) <= 500 and update.endswith("…")
    # Empty values never reach Meta (it refuses an empty parameter, 132000).
    empty = whatsapp.event_update(name=None, club="", event=" ", update="")
    assert empty.body == ("there", "The organisers", "your event", "a change")


async def test_neither_goes_to_a_number_whatsapp_cannot_take(session, meta):  # noqa: F811
    for number in (None, "12345", "+44 20 7946 0958x"):
        with pytest.raises(whatsapp.Undeliverable):
            await whatsapp.send(number, whatsapp.payment_pending(name="A", event="B", amount="1", slug="b"))
    assert meta["messages"] == []
