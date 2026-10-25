"""What the inbox does with what Meta's webhook brings, besides keeping it
(webhook.py): each in the SAVEPOINT of the message or status it is about,
so it is undone with it and done again when Meta delivers it again.

- A message kept: STOP or START (optout.py), then the alerts (alerts_inbox.py).
- A status: Meta's 131050 is a marketing opt-out (optout.py).
- `user_preferences`: someone stopping or resuming marketing in WhatsApp.
"""
from __future__ import annotations

from app.api.club_whatsapp import alerts_inbox, optout


async def after_message(session, account, conv_id: int, message: dict) -> None:
    await optout.on_inbound(session, account, conv_id, message)
    await alerts_inbox.sync_conversation(session, account.club_id, conv_id, arrived=True)


async def after_status(session, account, status: dict) -> None:
    await optout.on_status(session, account, status)


async def user_preferences(session, account, value: dict) -> None:
    await optout.user_preferences(session, account, value)
