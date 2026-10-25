"""Monthly email and WhatsApp allowances per club plan, and prepaid credits.

`record_message(club_id, channel, kind, via="platform")` is the one call
the senders make (service.record_message); `via="club"` -- a message sent
through a club's own WhatsApp number -- is never counted. Before a
non-essential send, `service.check` (or `service.held_back`) says whether
it is held back; `credits.remaining(session, club_id, channel)` is what is
left of the month's allowance plus the club's bought credits.
"""
from app.api.message_quota.service import record_message  # noqa: E402,F401
