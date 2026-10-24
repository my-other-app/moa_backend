"""What a club's own number sends, to the message quotas (app/api/message_quota).

A message sent from the club's own WhatsApp Business number is paid for by
the club, to Meta: it does NOT count against the club's WhatsApp quota, and
is never held back by it. The quotas record every send with the number it
went from and count only `via="platform"`.

- A ticket, certificate or reminder the deliveries send from the club's
  number answers `{"Via": "club"}` (app/core/whatsapp.py), and the pipeline
  counts by that (deliveries.service): nothing. One that fell back to the
  platform's number answers "platform", and counts.
- An inbox reply or template comes through `record_send` here: recorded
  `via="club"` -- the quotas' own rule, so a change there (a report of what
  clubs send themselves, say) reaches these too.

Never raises: a send that went must not be reported as failed because it
could not be counted (message_quota.count is in a SAVEPOINT).
"""
from __future__ import annotations

from typing import Optional

#: The kinds this module records: an inbox reply (free text), a template from the inbox.
KINDS = ("inbox_reply", "inbox_template")
CHANNEL = "whatsapp"
VIA = "club"


async def record_send(session, club_id: int, kind: str, ref: Optional[str] = None) -> None:
    """One message the inbox sent from the club's own number; `ref` is Meta's message id."""
    from app.api.message_quota import service as message_quota

    await message_quota.count(
        session, club_id=club_id, channel=message_quota.WHATSAPP, kind=kind, via=message_quota.CLUB,
        ref=f"club_whatsapp:{ref}" if ref else None,
    )
