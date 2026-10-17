"""A club's own Razorpay account, for the tickets and merch it sells.

One row per connection. A club pasting new keys does not overwrite the old
row: it is marked `replaced` and kept, and so is a `disconnected` or
`disabled` one, because every order made on it is still verified and refunded
through that account (payment_orders.payment_gateway_id), whatever the club
has connected since.

At most one row per club is live (`active` or `failed`), held by a partial
unique index. New orders go to the live row only while it is `active`;
otherwise, and for a club with no row, they go to the platform's account.

`mode` is `keys` today: the club pasted its key id and secret. `oauth` is
reserved for "Connect Razorpay" once Razorpay approves the partner
application; such a row will carry a token instead of a key secret, which is
why `key_secret_enc` may be null.
"""
from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

PROVIDER_RAZORPAY = "razorpay"

MODE_KEYS = "keys"
MODE_OAUTH = "oauth"  # reserved: Razorpay's partner OAuth
MODES = (MODE_KEYS, MODE_OAUTH)

ACTIVE = "active"
#: The keys stopped working (a check or an order was refused). Still the
#: club's connection, but new orders go to the platform until it is fixed.
FAILED = "failed"
#: The club pasted new keys; this row's orders still use it.
REPLACED = "replaced"
#: The club disconnected it.
DISCONNECTED = "disconnected"
#: A platform admin switched it off.
DISABLED = "disabled"
STATUSES = (ACTIVE, FAILED, REPLACED, DISCONNECTED, DISABLED)
LIVE = (ACTIVE, FAILED)


class ClubPaymentGateways(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_payment_gateways"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # No cascade: a club is soft-deleted, and these rows are money records.
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    provider = Column(String(20), nullable=False, default=PROVIDER_RAZORPAY, server_default=PROVIDER_RAZORPAY)
    mode = Column(String(10), nullable=False, default=MODE_KEYS, server_default=MODE_KEYS)
    #: Publishable: checkout opens with it.
    key_id = Column(String(64), nullable=False)
    #: Fernet, with settings.PAYMENT_KEYS_SECRET. Never returned, logged or audited.
    key_secret_enc = Column(Text, nullable=True)
    #: Ours, generated at connect and shown to the owner to paste into Razorpay.
    webhook_secret_enc = Column(Text, nullable=False)
    #: The last part of the club's webhook URL. Random, url-safe.
    webhook_token = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default=ACTIVE, server_default=ACTIVE)
    verified_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_webhook_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_error = Column(String(300), nullable=True)
    #: When it stopped taking new orders: replaced, disconnected or disabled.
    ended_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    club = relationship("Clubs", foreign_keys=[club_id])

    __table_args__ = (
        Index(
            "uq_club_payment_gateways_live",
            "club_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'failed')"),
        ),
    )
