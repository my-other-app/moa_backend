"""What connecting an AI assistant to a club leaves in the database.

An assistant (an OAuth *client*: Claude, ChatGPT, Cursor...) is registered
once, by itself (`McpClients`). A person who signs in and approves it makes a
*grant* (`McpGrants`): this app may act for this club, as this person, within
these scopes. That is the club's "connected app", what the Security page lists
and revokes. The grant's tokens (`McpTokens`) are only ever stored as sha256
digests; the token itself exists only in the answer that handed it out.

What an assistant may not do by itself -- publish an event, send an
announcement -- it asks for (`McpPendingActions`): a person opens the link in
the dashboard and approves it, or not, within a day. An announcement it drafts
waits in `AnnouncementDrafts` until then.
"""
import uuid

from sqlalchemy import JSON, CheckConstraint, Column, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

#: How a client came to be known: registered by itself (RFC 7591), or
#: identified by the https URL of its metadata document (fetched and cached).
CLIENT_KINDS = ("dcr", "cimd")
TOKEN_KINDS = ("access", "refresh")
ACTION_KINDS = ("publish_event", "send_announcement")
ACTION_STATUSES = ("pending", "confirmed", "rejected", "expired", "superseded", "failed")
DRAFT_STATUSES = ("draft", "sent", "discarded")
AUDIENCES = ("all", "attendees", "non_attendees")


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class McpClients(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "mcp_clients"

    id = _uuid_pk()
    # What the client sends as client_id: ours for a registered client, its
    # metadata document's URL for the other kind.
    client_id = Column(String(512), nullable=False, unique=True)
    kind = Column(String(8), nullable=False, default="dcr")
    client_name = Column(String(200), nullable=True)
    client_uri = Column(String(500), nullable=True)
    logo_uri = Column(String(500), nullable=True)
    # Exact strings: an authorization request's redirect_uri must be one of them.
    redirect_uris = Column(JSON, nullable=False, default=list)
    grant_types = Column(JSON, nullable=False, default=list)
    # "none" for a public client (PKCE alone); client_secret_post or
    # client_secret_basic for one that registered for a secret.
    token_endpoint_auth_method = Column(String(32), nullable=False, default="none")
    client_secret_hash = Column(String(64), nullable=True)
    software_id = Column(String(200), nullable=True)
    software_version = Column(String(60), nullable=True)
    # The registration or the fetched document, as received (secrets never).
    client_metadata = Column("metadata", JSON, nullable=True)
    # A metadata document's: when it was read, and until when it is trusted
    # without reading it again.
    fetched_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    fresh_until = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("kind IN ('dcr', 'cimd')", name="ck_mcp_clients_kind"),)


class McpAuthCodes(AbstractSQLModel):
    """An authorization code, between the consent and the token request: good
    once, for a few minutes, only with the verifier behind its challenge."""

    __tablename__ = "mcp_auth_codes"

    code_hash = Column(String(64), primary_key=True)
    client_pk = Column(PGUUID(as_uuid=True), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False)
    # Who approved it, for which club, as whom: the grant is made from these
    # when the code is exchanged.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True)
    token_version = Column(Integer, nullable=True)
    redirect_uri = Column(String(2000), nullable=False)
    code_challenge = Column(String(128), nullable=False)
    scopes = Column(String(400), nullable=False)
    resource = Column(String(500), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    used_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # The grant its exchange made: a second exchange of the code revokes it.
    grant_id = Column(PGUUID(as_uuid=True), ForeignKey("mcp_grants.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_mcp_auth_codes_expires_at", "expires_at"),)


class McpGrants(AbstractSQLModel, TimestampsMixin):
    """A connected app: this client may act for this club, as this person
    (the club's own account, or the team member in `member_id`), within
    `scopes`. Revoking it ends every token it has."""

    __tablename__ = "mcp_grants"

    id = _uuid_pk()
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    # The club's own account, which every club route acts as.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # The team member behind it; None when the club's own login connected it.
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True, index=True)
    client_pk = Column(PGUUID(as_uuid=True), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False)
    scopes = Column(String(400), nullable=False)
    # The club account's token_version when the owner connected it: a
    # password change or "sign out everywhere" ends the grant too.
    token_version = Column(Integer, nullable=True)
    last_used_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # disconnected | refresh_reused | access_removed | consent_pending
    revoked_reason = Column(String(40), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_mcp_grants_club_live", "club_id", postgresql_where=text("revoked_at IS NULL")),
    )


class McpTokens(AbstractSQLModel):
    """An access or refresh token, by its digest. A refresh token is spent
    when it is exchanged (`rotated_at`); presented again after that, the
    grant behind it is revoked."""

    __tablename__ = "mcp_tokens"

    id = _uuid_pk()
    grant_id = Column(PGUUID(as_uuid=True), ForeignKey("mcp_grants.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(8), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    scopes = Column(String(400), nullable=False)
    # The audience: the MCP endpoint's canonical URL (RFC 8707).
    resource = Column(String(500), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    rotated_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # The refresh token this one was issued for.
    parent_id = Column(PGUUID(as_uuid=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("kind IN ('access', 'refresh')", name="ck_mcp_tokens_kind"),
        Index("ix_mcp_tokens_expires_at", "expires_at"),
    )


class McpPendingActions(AbstractSQLModel, TimestampsMixin):
    """Something an assistant asked for that a person must approve: shown,
    then confirmed or rejected, through the dashboard's /confirm/{token}
    page. The token is good once and for a day; only its digest is kept."""

    __tablename__ = "mcp_pending_actions"

    id = _uuid_pk()
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    grant_id = Column(PGUUID(as_uuid=True), ForeignKey("mcp_grants.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(32), nullable=False)
    # What it acts on: an event's id, an announcement draft's id.
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(64), nullable=False)
    # What it will do, as the assistant asked for it.
    payload = Column(JSON, nullable=False, default=dict)
    token_hash = Column(String(64), nullable=False, unique=True)
    status = Column(String(12), nullable=False, default="pending")
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    requested_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    client_name = Column(String(200), nullable=True)
    decided_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    # What happened when it was confirmed (or why it failed).
    result = Column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', 'expired', 'superseded', 'failed')",
            name="ck_mcp_pending_actions_status",
        ),
        Index("ix_mcp_pending_actions_club_status", "club_id", "status"),
        Index("ix_mcp_pending_actions_target", "kind", "target_id", postgresql_where=text("status = 'pending'")),
    )


class AnnouncementDrafts(AbstractSQLModel, TimestampsMixin):
    """An announcement to an event's people, written but not sent. Sent only
    when a person confirms it (the same send, plan limit and channels as the
    dashboard's announcement page)."""

    __tablename__ = "announcement_drafts"

    id = _uuid_pk()
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(String(1000), nullable=False)
    audience = Column(String(16), nullable=False, default="all")
    status = Column(String(10), nullable=False, default="draft")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    # mcp: written by a connected assistant (client_name says which).
    created_via = Column(String(16), nullable=False, default="mcp")
    client_name = Column(String(200), nullable=True)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'sent', 'discarded')", name="ck_announcement_drafts_status"),
        CheckConstraint("audience IN ('all', 'attendees', 'non_attendees')", name="ck_announcement_drafts_audience"),
        Index("ix_announcement_drafts_club_status", "club_id", "status"),
    )
