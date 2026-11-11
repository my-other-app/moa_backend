"""Social auto-posting: what a club connected, what it plans to post, and
what went out (docs/SOCIAL_POSTING.md).

- social_accounts: one connected Facebook Page, Instagram business or
  creator account, LinkedIn member, LinkedIn Page or X account of a club.
  Its tokens are kept encrypted (SOCIAL_KEYS_SECRET, social.crypto) and are
  never read back to anyone; disconnecting empties them and keeps the row
  (status `disconnected`) so the posts that went out through it still say
  where.
- social_posts: one planned post of an event by one club (the host or a
  co-host, each with its own accounts): its name, its trigger, its status.
- social_post_targets: the post on one account: its text (with fill-in
  words), its image, when it is due, and how it went. Exactly once: a target
  is claimed (`posting`) by one conditional UPDATE before anything is sent;
  the platform's post id is saved the moment the platform confirms it, and a
  target whose answer was lost (`uncertain_since`) is looked up on the
  platform before it is ever sent again.
- poster_variants: an event's poster made to each platform's size, by crop
  around a focus point or fit on a background, or a club's own image; public
  on the media CDN (Instagram fetches it by address).
- social_oauth_states: one sign-in with a platform: a one-time state, and
  the choice of Page or account waiting when there were several.
- social_settings: a club's approval setting.
- social_data_deletions: Meta's data deletion requests, by confirmation
  code, for the status page.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, UUID, Boolean, Column, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

PLATFORMS = ("facebook", "instagram", "linkedin", "linkedin_page", "x")

# Account statuses.
CONNECTED, NEEDS_RECONNECT, DISCONNECTED = "connected", "needs_reconnect", "disconnected"
ACCOUNT_STATUSES = (CONNECTED, NEEDS_RECONNECT, DISCONNECTED)

# Post statuses.
DRAFT, NEEDS_APPROVAL, SCHEDULED, CANCELLED, DONE, PAUSED = "draft", "needs_approval", "scheduled", "cancelled", "done", "paused"
POST_STATUSES = (DRAFT, NEEDS_APPROVAL, SCHEDULED, CANCELLED, DONE, PAUSED)

# Target statuses.
T_SCHEDULED, POSTING, POSTED, FAILED, SKIPPED, T_CANCELLED, HELD, WAITING_LIMIT = (
    "scheduled", "posting", "posted", "failed", "skipped", "cancelled", "held", "waiting_limit",
)
TARGET_STATUSES = (T_SCHEDULED, POSTING, POSTED, FAILED, SKIPPED, T_CANCELLED, HELD, WAITING_LIMIT)
#: A target that has not gone out and still may.
WAITING = (T_SCHEDULED, HELD, WAITING_LIMIT)
#: A target that is over, one way or another.
FINISHED = (POSTED, FAILED, SKIPPED, T_CANCELLED)

SIZES = {
    # (width, height): the platform's recommended picture.
    "ig_portrait": (1080, 1350),
    "ig_square": (1080, 1080),
    "facebook": (1200, 630),
    "linkedin": (1200, 627),
    "linkedin_square": (1080, 1080),
    "x": (1600, 900),
}


def _check(table: str, column: str, values) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=f"ck_{table}_{column}")


class SocialAccounts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "social_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(20), nullable=False)
    #: The Page's, Instagram account's, member's (`sub`), organisation's or X user's id.
    external_id = Column(String(100), nullable=False)
    name = Column(String(200), nullable=True)
    handle = Column(String(100), nullable=True)
    picture_url = Column(String(1000), nullable=True)
    #: Who signed in on the platform to connect it, as the platform knows them
    #: (Meta's app-scoped user id: what the deauthorise and data-deletion
    #: callbacks name). Never shown.
    connector_external_id = Column(String(100), nullable=True)
    access_token_enc = Column(Text, nullable=True)
    refresh_token_enc = Column(Text, nullable=True)
    token_expires_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    refresh_expires_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    scopes = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default=CONNECTED, server_default=CONNECTED)
    last_error = Column(String(400), nullable=True)
    connected_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    connected_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    connected_by_name = Column(String(120), nullable=True)
    connected_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    disconnected_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    refreshed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The expiry the club was last warned about (the daily job warns once per expiry).
    expiry_warned_for = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        _check("social_accounts", "platform", PLATFORMS),
        _check("social_accounts", "status", ACCOUNT_STATUSES),
        # One live connection per account per club; reconnecting updates it.
        Index("uq_social_accounts_live", "club_id", "platform", "external_id", unique=True,
              postgresql_where=sa.text("status <> 'disconnected'")),
        Index("ix_social_accounts_club", "club_id", "platform"),
        Index("ix_social_accounts_connector", "connector_external_id", postgresql_where=sa.text("connector_external_id IS NOT NULL")),
        Index("ix_social_accounts_member", "connected_by_member_id", postgresql_where=sa.text("connected_by_member_id IS NOT NULL")),
        Index("ix_social_accounts_user", "connected_by_user_id", postgresql_where=sa.text("connected_by_user_id IS NOT NULL")),
    )


class SocialSettings(AbstractSQLModel):
    __tablename__ = "social_settings"

    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True)
    #: An editor's post waits as "Needs approval" until an owner or admin approves it.
    approval_required = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


class PosterVariants(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "poster_variants"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    size = Column(String(20), nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    #: "crop" around the focus point, or "fit" on the background.
    mode = Column(String(8), nullable=False, default="crop", server_default="crop")
    focus_x = Column(Float, nullable=False, default=0.5, server_default="0.5")
    focus_y = Column(Float, nullable=False, default=0.5, server_default="0.5")
    #: "#rrggbb", for fit.
    background = Column(String(7), nullable=True)
    #: The picture it was made from: the event's poster's key, or the club's own image's.
    source_key = Column(String(500), nullable=True)
    #: True when the club gave its own image for this size.
    own = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: The made picture's key, in a public folder (media.PUBLIC_FOLDERS).
    key = Column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "size", name="uq_poster_variants_event_size"),
        _check("poster_variants", "size", tuple(SIZES)),
        _check("poster_variants", "mode", ("crop", "fit")),
    )


class SocialPosts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "social_posts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    #: The club posting it: the host, or a co-host from its own accounts.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(80), nullable=False)
    #: {mode: on_publish|at|before_start|before_reg_close|after_end, at?, minutes?}
    trigger = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default=DRAFT, server_default=DRAFT)
    #: Why it is paused ("event_changed") or cancelled ("event_cancelled", "event_unpublished", "cohost_left", "by_club").
    status_reason = Column(String(40), nullable=True)
    #: "announcement", "reminder", "cancellation": a post the platform made from its templates.
    template = Column(String(20), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    created_by_name = Column(String(120), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    approved_by_name = Column(String(120), nullable=True)
    approved_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The event's details when it was scheduled, to tell what changed since.
    event_snapshot = Column(JSON, nullable=True)

    __table_args__ = (
        _check("social_posts", "status", POST_STATUSES),
        Index("ix_social_posts_event", "event_id", "club_id", "created_at"),
        Index("ix_social_posts_club", "club_id"),
    )


class SocialPostTargets(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "social_post_targets"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    post_id = Column(UUID(as_uuid=True), ForeignKey("social_posts.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(20), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True)
    #: The text with its fill-in words, as the club wrote it.
    text = Column(Text, nullable=False, default="", server_default="")
    variant_id = Column(UUID(as_uuid=True), ForeignKey("poster_variants.id", ondelete="SET NULL"), nullable=True)
    #: The tracking link {link} becomes.
    link_id = Column(UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, default=T_SCHEDULED, server_default=T_SCHEDULED)
    #: When it is due; null while it waits for the event to be published.
    scheduled_for = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Not before this (a retry's gap, a platform's limit).
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    first_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    #: When it was claimed for sending, and by which claim.
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    claim = Column(UUID(as_uuid=True), nullable=True)
    #: Set when a send may have reached the platform without its answer
    #: reaching us: the next attempt looks it up first.
    uncertain_since = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The text as sent, fill-in words filled (what a lookup compares).
    rendered_text = Column(Text, nullable=True)
    #: Instagram's media container, kept between its steps.
    container_id = Column(String(100), nullable=True)
    external_post_id = Column(String(200), nullable=True)
    post_url = Column(String(1000), nullable=True)
    posted_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    error_code = Column(String(40), nullable=True)
    error_message = Column(String(400), nullable=True)
    #: retry | reconnect | share_manually | fix_image
    error_action = Column(String(20), nullable=True)

    __table_args__ = (
        _check("social_post_targets", "platform", PLATFORMS),
        _check("social_post_targets", "status", TARGET_STATUSES),
        UniqueConstraint("post_id", "account_id", name="uq_social_post_targets_account"),
        Index("ix_social_post_targets_post", "post_id"),
        # The dispatcher: what is waiting, by when.
        Index("ix_social_post_targets_due", "scheduled_for",
              postgresql_where=sa.text("status IN ('scheduled', 'waiting_limit', 'posting')")),
        Index("ix_social_post_targets_account", "account_id", "status"),
        Index("ix_social_post_targets_event", "event_id", "club_id"),
        # The monthly X count and Instagram's day.
        Index("ix_social_post_targets_posted", "platform", "posted_at", postgresql_where=sa.text("posted_at IS NOT NULL")),
        Index("ix_social_post_targets_variant", "variant_id", postgresql_where=sa.text("variant_id IS NOT NULL")),
        Index("ix_social_post_targets_link", "link_id", postgresql_where=sa.text("link_id IS NOT NULL")),
    )


class SocialOAuthStates(AbstractSQLModel):
    __tablename__ = "social_oauth_states"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(20), nullable=False)
    #: sha256 of the random half of the state handed to the platform.
    nonce_hash = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    actor_name = Column(String(120), nullable=True)
    return_to = Column(String(500), nullable=False)
    #: X's PKCE verifier, encrypted.
    verifier_enc = Column(Text, nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    #: The callback came back with it: it is never accepted again.
    used_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Several Pages or accounts: what the club picks from ([{external_id, name, picture}]) ...
    choices = Column(JSON, nullable=True)
    #: ... and their tokens, encrypted, until it picks.
    pending_enc = Column(Text, nullable=True)
    chosen_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        _check("social_oauth_states", "platform", PLATFORMS),
        Index("ix_social_oauth_states_club", "club_id", "created_at"),
        Index("ix_social_oauth_states_expires", "expires_at"),
    )


class SocialDataDeletions(AbstractSQLModel):
    __tablename__ = "social_data_deletions"

    code = Column(String(40), primary_key=True)
    platform = Column(String(20), nullable=False)
    #: sha256 of the platform's user id: what the request was about, never the id itself.
    subject_hash = Column(String(64), nullable=False)
    accounts = Column(Integer, nullable=False, default=0, server_default="0")
    requested_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    completed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
