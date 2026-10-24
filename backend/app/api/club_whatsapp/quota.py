"""The message quotas' call site for what a club's own number sends.

A message sent from the club's own WhatsApp Business number is paid for by
the club, to Meta: it does NOT count against the platform's WhatsApp quota.
The quotas (branch feat/message-quotas, app/api/message_quota) record every
send with where it went from and count only `via="platform"` against a plan.

QUOTA HOOK: every send from a club's own number comes through `record_send`
-- an inbox reply, a template from the inbox, a ticket, certificate or
reminder the deliveries sent from the club's number. Until
feat/message-quotas is on master it records nothing; when it is, the body
below becomes (its signature as that branch has it):

    from app.api.message_quota.service import CLUB, record_message
    await record_message(club_id, "whatsapp", kind, via=CLUB, session=session, ref=ref)

and nothing else here changes. Never raises: a send that went must not be
reported as failed because it could not be counted.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: The kinds this module records: an inbox reply (free text), a template from
#: the inbox, and the deliveries' ticket, certificate and reminder.
KINDS = ("inbox_reply", "inbox_template", "ticket", "certificate", "reminder")
CHANNEL = "whatsapp"
VIA = "club"


async def record_send(session, club_id: int, kind: str, ref: Optional[str] = None) -> None:
    """One message sent from the club's own number (`via="club"`); `ref` is
    Meta's message id.

    QUOTA HOOK -- see the module docstring. No-op until feat/message-quotas."""
    return None
