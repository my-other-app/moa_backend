"""A club's own domain for its website: `www.theirclub.org` serving the page
that `{slug}.myotherapp.in` serves.

Each domain is a Cloudflare for SaaS custom hostname on the myotherapp.in
zone. The club adds a CNAME to `sites.myotherapp.in` and two TXT records
(ownership, and the certificate's validation); Cloudflare issues the
certificate and routes the traffic to the club-sites Worker, which asks
`GET /sites/resolve` whose site it is.

A row is never reused: a club that changes its domain, or tries again after
a failure, gets a new row and the old one is `removed`.

- pending: created at Cloudflare, waiting for the records. Polled every five
  minutes for seven days, then `failed`.
- active: Cloudflare says the hostname and its certificate are active, and
  the club's plan includes `custom_domain`. The site is served there, and its
  `{slug}.myotherapp.in` address redirects to it.
- paused: the club's plan stopped including `custom_domain`. The Worker
  redirects the domain to `{slug}.myotherapp.in`. Back on a plan with it, the
  domain is active again; after thirty days paused it is removed.
- failed: never verified, or Cloudflare dropped it. Kept for the club to see
  why; it holds nothing at Cloudflare and does not hold the host.
- removed: the club, an admin or the sweep took it away.

At most one live row (pending, active or paused) per club and per host,
held by partial unique indexes.
"""
from sqlalchemy import JSON, CheckConstraint, Column, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

PENDING = "pending"
ACTIVE = "active"
PAUSED = "paused"
FAILED = "failed"
REMOVED = "removed"
STATUSES = (PENDING, ACTIVE, PAUSED, FAILED, REMOVED)
LIVE = (PENDING, ACTIVE, PAUSED)

_LIVE_SQL = "status IN ('pending', 'active', 'paused')"


class ClubDomains(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_domains"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # No cascade: a club is soft-deleted, and the row says what was at Cloudflare.
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    #: Lower case, punycode, no trailing dot: `www.theirclub.org`.
    host = Column(String(253), nullable=False)
    status = Column(String(10), nullable=False, default=PENDING, server_default=PENDING)
    #: Cloudflare's custom hostname id; null once it is gone from Cloudflare.
    cf_hostname_id = Column(String(64), nullable=True)
    #: What the club adds at its DNS host, as Cloudflare last gave it:
    #: [{"type": "CNAME"|"TXT", "name", "value", "purpose": "route"|"ownership"|"ssl"}].
    records = Column(JSON, nullable=False, default=list)
    #: Cloudflare's own words: the hostname's status and its certificate's.
    hostname_status = Column(String(40), nullable=True)
    ssl_status = Column(String(40), nullable=True)
    #: Why it is not working, in a sentence the club can act on; null when it is.
    last_error = Column(String(300), nullable=True)
    #: When Cloudflare first said both the hostname and the certificate were active.
    verified_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    paused_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When Cloudflare was last asked about it.
    checked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    removed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    club = relationship("Clubs", foreign_keys=[club_id])

    __table_args__ = (
        Index("uq_club_domains_live_host", "host", unique=True, postgresql_where=text(_LIVE_SQL)),
        Index("uq_club_domains_live_club", "club_id", unique=True, postgresql_where=text(_LIVE_SQL)),
        Index("ix_club_domains_status", "status"),
        CheckConstraint(
            "status IN ('pending', 'active', 'paused', 'failed', 'removed')",
            name="ck_club_domains_status",
        ),
    )
