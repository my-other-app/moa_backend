"""Social auto-posting and the share kit (app/api/social, docs/SOCIAL_POSTING.md,
feature-register/social-posting-contract.md).

New tables, every one empty:

- social_accounts: a club's connected Facebook Pages, Instagram, LinkedIn
  (member and Page) and X accounts; tokens encrypted (SOCIAL_KEYS_SECRET);
  uq_social_accounts_live keeps one live connection per account per club.
- social_settings: a club's approval setting.
- poster_variants: an event's poster in each platform's size;
  uq_poster_variants_event_size one per size.
- social_posts, social_post_targets: an event's planned posts and each
  one's account; uq_social_post_targets_account keeps an account to one
  target a post; ix_social_post_targets_due is the dispatcher's.
- social_oauth_states: one-time sign-in states and pending Page choices.
- social_data_deletions: Meta's data deletion requests, by code.

Changed: plan_features gets `social_autopost` (automatic posting to
Facebook, Instagram and LinkedIn) and `social_x` (to X), both off by
default and switched on as the owner decided (11 Nov): social_autopost on
the paid plans (starter, growth, pro, campus, enterprise), social_x on the
top plan (enterprise). Admins edit both in Admin -> Subscriptions like every
other flag; no price changes.

Lock-safe: the tables are new, so their indexes are built on nothing; the
two columns are added with a constant default (a catalog change);
plan_features is a handful of rows. The foreign keys take a brief SHARE ROW
EXCLUSIVE on clubs, users, events, club_members and tracking_links, under
lock_timeout 3s.

Downgrade drops the tables (every connection, post and poster size with
them) and the two columns.

Revision ID: social_posting
Revises: review_resends
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "social_posting"
down_revision = "review_resends"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
UUID_PK = dict(primary_key=True, server_default=sa.text("gen_random_uuid()"))

#: The plans with each flag, from the start.
AUTOPOST_PLANS = ("starter", "growth", "pro", "campus", "enterprise")
X_PLANS = ("enterprise",)

PLATFORMS = ("facebook", "instagram", "linkedin", "linkedin_page", "x")
SIZES = ("ig_portrait", "ig_square", "facebook", "linkedin", "linkedin_square", "x")


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def grant() -> None:
    """Switch the flags on for their plans. Plans that do not exist are skipped; safe to run again."""
    bind = op.get_bind()
    for column, plans in (("social_autopost", AUTOPOST_PLANS), ("social_x", X_PLANS)):
        bind.execute(
            sa.text(f"UPDATE plan_features SET {column} = true, updated_at = now() "
                    "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"),
            dict(keys=list(plans)),
        )


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.add_column("plan_features", sa.Column("social_autopost", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("plan_features", sa.Column("social_x", sa.Boolean(), nullable=False, server_default=sa.false()))
    grant()

    op.create_table(
        "social_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("handle", sa.String(100), nullable=True),
        sa.Column("picture_url", sa.String(1000), nullable=True),
        sa.Column("connector_external_id", sa.String(100), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
        sa.Column("last_error", sa.String(400), nullable=True),
        sa.Column("connected_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("connected_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("connected_by_name", sa.String(120), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry_warned_for", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_social_accounts_platform"),
        sa.CheckConstraint(_in("status", ("connected", "needs_reconnect", "disconnected")), name="ck_social_accounts_status"),
        if_not_exists=True,
    )
    op.create_index("uq_social_accounts_live", "social_accounts", ["club_id", "platform", "external_id"], unique=True,
                    postgresql_where=sa.text("status <> 'disconnected'"), if_not_exists=True)
    op.create_index("ix_social_accounts_club", "social_accounts", ["club_id", "platform"], if_not_exists=True)
    op.create_index("ix_social_accounts_connector", "social_accounts", ["connector_external_id"],
                    postgresql_where=sa.text("connector_external_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_social_accounts_member", "social_accounts", ["connected_by_member_id"],
                    postgresql_where=sa.text("connected_by_member_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_social_accounts_user", "social_accounts", ["connected_by_user_id"],
                    postgresql_where=sa.text("connected_by_user_id IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "social_settings",
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )

    op.create_table(
        "poster_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("size", sa.String(20), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False, server_default="crop"),
        sa.Column("focus_x", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("focus_y", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("background", sa.String(7), nullable=True),
        sa.Column("source_key", sa.String(500), nullable=True),
        sa.Column("own", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("key", sa.String(500), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("event_id", "size", name="uq_poster_variants_event_size"),
        sa.CheckConstraint(_in("size", SIZES), name="ck_poster_variants_size"),
        sa.CheckConstraint(_in("mode", ("crop", "fit")), name="ck_poster_variants_mode"),
        if_not_exists=True,
    )

    op.create_table(
        "social_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("trigger", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("status_reason", sa.String(40), nullable=True),
        sa.Column("template", sa.String(20), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_by_name", sa.String(120), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_snapshot", sa.JSON(), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("status", ("draft", "needs_approval", "scheduled", "cancelled", "done", "paused")),
                           name="ck_social_posts_status"),
        if_not_exists=True,
    )
    op.create_index("ix_social_posts_event", "social_posts", ["event_id", "club_id", "created_at"], if_not_exists=True)
    op.create_index("ix_social_posts_club", "social_posts", ["club_id"], if_not_exists=True)

    op.create_table(
        "social_post_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("social_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("poster_variants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uncertain_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rendered_text", sa.Text(), nullable=True),
        sa.Column("container_id", sa.String(100), nullable=True),
        sa.Column("external_post_id", sa.String(200), nullable=True),
        sa.Column("post_url", sa.String(1000), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.String(400), nullable=True),
        sa.Column("error_action", sa.String(20), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_social_post_targets_platform"),
        sa.CheckConstraint(_in("status", ("scheduled", "posting", "posted", "failed", "skipped", "cancelled", "held", "waiting_limit")),
                           name="ck_social_post_targets_status"),
        sa.UniqueConstraint("post_id", "account_id", name="uq_social_post_targets_account"),
        if_not_exists=True,
    )
    op.create_index("ix_social_post_targets_post", "social_post_targets", ["post_id"], if_not_exists=True)
    op.create_index("ix_social_post_targets_due", "social_post_targets", ["scheduled_for"],
                    postgresql_where=sa.text("status IN ('scheduled', 'waiting_limit', 'posting')"), if_not_exists=True)
    op.create_index("ix_social_post_targets_account", "social_post_targets", ["account_id", "status"], if_not_exists=True)
    op.create_index("ix_social_post_targets_event", "social_post_targets", ["event_id", "club_id"], if_not_exists=True)
    op.create_index("ix_social_post_targets_posted", "social_post_targets", ["platform", "posted_at"],
                    postgresql_where=sa.text("posted_at IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_social_post_targets_variant", "social_post_targets", ["variant_id"],
                    postgresql_where=sa.text("variant_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_social_post_targets_link", "social_post_targets", ["link_id"],
                    postgresql_where=sa.text("link_id IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "social_oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_name", sa.String(120), nullable=True),
        sa.Column("return_to", sa.String(500), nullable=False),
        sa.Column("verifier_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("choices", sa.JSON(), nullable=True),
        sa.Column("pending_enc", sa.Text(), nullable=True),
        sa.Column("chosen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("platform", PLATFORMS), name="ck_social_oauth_states_platform"),
        if_not_exists=True,
    )
    op.create_index("ix_social_oauth_states_club", "social_oauth_states", ["club_id", "created_at"], if_not_exists=True)
    op.create_index("ix_social_oauth_states_expires", "social_oauth_states", ["expires_at"], if_not_exists=True)

    op.create_table(
        "social_data_deletions",
        sa.Column("code", sa.String(40), primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("accounts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    for table in ("social_data_deletions", "social_oauth_states", "social_post_targets", "social_posts", "poster_variants",
                  "social_settings", "social_accounts"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.drop_column("plan_features", "social_x")
    op.drop_column("plan_features", "social_autopost")
