"""Monthly email and WhatsApp quotas per club plan, and paid overage.

`record_message(club_id, channel, kind, via="platform")` is the one call
the senders make (service.record_message); `via="club"` -- a message sent
through a club's own WhatsApp number -- is never counted.
"""
from app.api.message_quota.service import record_message  # noqa: E402,F401
