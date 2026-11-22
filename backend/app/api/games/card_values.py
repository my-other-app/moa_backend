"""The game's QR and PIN on ID cards and tickets (CONTRACT §1).

`game_qr` is the badge token's address and `game_pin` the guest's four digits;
a card's QR layer bound to `holder.game_qr` and its text layers ({{game_qr}},
{{game_pin}}) place them. Only filled when a game is on for the event (and the
club's plan has it): otherwise they are empty and a bound QR is not drawn.

The cards are drawn from rows read on the loop, so the values are read here
first (`prime`) and carried on the registration as `_game` = (url, pin); the
mappings (doc_presets.values.subject_of, events.documents.token_attendee) only
look at that.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import select

from app.api.games import common, tokens
from app.api.games.models import CARDS, HUNT, GamePlayers

SAMPLE_QR = "https://myotherapp.in/g/b.0.sampleSAMPLEsampleSAMPL.0.sampleSAMPLEsamp"
SAMPLE_PIN = "4821"


async def prime(session, event, registrations: Iterable) -> None:
    regs = list(registrations)
    if not regs:
        return
    settings = await common.settings_of(session, event.id)
    if settings is None or not (settings.cards_on or settings.hunt_on):
        return
    allowed = (settings.cards_on and await common.plan_allows(session, event, CARDS)) or (settings.hunt_on and await common.plan_allows(session, event, HUNT))
    if not allowed:
        return
    versions = dict((await session.execute(select(GamePlayers.registration_id, GamePlayers.pin_version).where(GamePlayers.event_id == event.id))).all())
    for reg in regs:
        code = tokens.code_of(event.id, reg.id, versions.get(reg.id, 0))
        reg._game = (code["url"], code["pin"])


def of(registration) -> tuple:
    """(address, pin) primed on the registration, or two empty strings."""
    return getattr(registration, "_game", None) or ("", "")
