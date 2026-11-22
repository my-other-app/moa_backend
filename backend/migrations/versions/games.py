"""The event games: Hero Cards and the Scavenger Hunt (app/api/games, feature-register/games/CONTRACT.md).

Every table is new and empty (prefix `game_`; each belongs to one event and
goes with it), so their indexes are made with them:

- game_settings, game_players: the switches, names and look; who has signed in.
- game_card_config, game_heroes, game_tier_counters, game_reveals,
  game_deck_items, game_meetings, game_card_scores: Hero Cards.
- game_hunts, game_hunt_points, game_hunt_claims, game_hunt_hints,
  game_hunt_scores: the Scavenger Hunt.
- game_feed, game_snapshots: the ticker; a board's ranks once a minute.

Changed: plan_features gets `game_cards` and `game_hunt`, both off on every
plan (a platform admin ticks them on the plans that get them, in Admin ->
Subscriptions); a constant default, a catalogue change, on a handful of rows.

lock_timeout is set first: the foreign keys take a brief SHARE ROW EXCLUSIVE
on events and event_registrations_link, and plan_features is altered; each
waits at most 5 s for its lock rather than queue behind a long transaction.
Idempotent: a table or column that exists is left as it is, so running it
again after a partial run finishes the job.

Downgrade drops the tables (all game data with them) and the two columns.

Revision ID: games
Revises: print_stations
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "games"
down_revision = "print_stations"
branch_labels = None
depends_on = None

NOW = sa.text("now()")
TIERS = "('common', 'uncommon', 'rare', 'legendary')"


def _id():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _event(unique: bool = False):
    return sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=unique)


def _reg(name: str = "registration_id"):
    return sa.Column(name, UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)


def _at(name, nullable=True, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def _stamps(updated: bool = True):
    return [_at("created_at", False, server_default=NOW)] + ([_at("updated_at", False, server_default=NOW)] if updated else [])


def _int(name, default, nullable=False):
    return sa.Column(name, sa.Integer(), nullable=nullable, server_default=str(default))


def _bool(name, default):
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true() if default else sa.false())


def tables():
    return {
        "game_settings": [
            _id(), _event(True), _bool("cards_on", False), sa.Column("cards_name", sa.String(40), nullable=False, server_default="Hero Cards"),
            _bool("hunt_on", False), sa.Column("hunt_name", sa.String(40), nullable=False, server_default="Scavenger Hunt"),
            sa.Column("theme", sa.String(16), nullable=False, server_default="neon_arcade"), sa.Column("accent", sa.String(9), nullable=True),
            sa.Column("logo_url", sa.String(500), nullable=True), sa.Column("board_layout", sa.String(8), nullable=False, server_default="podium"),
            _int("board_rows", 7), _bool("ticker_on", True), _bool("join_box_on", True), _int("screen_version", 1), _int("player_seq", 0), *_stamps(),
            sa.CheckConstraint("theme IN ('neon_arcade', 'sunset_pop', 'ocean_glow', 'mono_night')", name="ck_game_settings_theme"),
            sa.CheckConstraint("board_layout IN ('podium', 'list')", name="ck_game_settings_layout"),
            sa.CheckConstraint("board_rows IN (5, 7, 10)", name="ck_game_settings_rows"),
        ],
        "game_players": [
            _id(), _event(), _reg(), sa.Column("player_no", sa.Integer(), nullable=False), _int("pin_version", 0), _int("pin_failed", 0),
            _at("pin_locked_until"), _bool("public", True), _int("session_version", 0), _at("last_seen_at"), _at("last_collect_at"), _at("last_scan_at"),
            *_stamps(False),
            sa.UniqueConstraint("event_id", "registration_id", name="uq_game_players_registration"),
            sa.UniqueConstraint("event_id", "player_no", name="uq_game_players_no"),
        ],
        "game_card_config": [
            _id(), _event(True), sa.Column("preset", sa.String(8), nullable=False, server_default="medium"),
            sa.Column("share_common", sa.Numeric(5, 2), nullable=False, server_default="52"), sa.Column("share_uncommon", sa.Numeric(5, 2), nullable=False, server_default="30"),
            sa.Column("share_rare", sa.Numeric(5, 2), nullable=False, server_default="13"), sa.Column("share_legendary", sa.Numeric(5, 2), nullable=False, server_default="5"),
            _int("legendary_floor", 4), _bool("pin_required", True), _bool("collect_on", True), _bool("two_way", True), _bool("show_holders", True), *_stamps(),
            sa.CheckConstraint("legendary_floor >= 1", name="ck_game_card_config_floor"),
        ],
        "game_heroes": [
            _id(), _event(), sa.Column("number", sa.Integer(), nullable=False), sa.Column("name", sa.String(18), nullable=False),
            sa.Column("tagline", sa.String(60), nullable=False, server_default=""), sa.Column("tier", sa.String(10), nullable=False),
            sa.Column("art", sa.String(), nullable=True), sa.Column("emblem_key", sa.String(24), nullable=True), *_stamps(),
            sa.UniqueConstraint("event_id", "number", name="uq_game_heroes_number", deferrable=True, initially="DEFERRED"),
            sa.CheckConstraint(f"tier IN {TIERS}", name="ck_game_heroes_tier"),
        ],
        "game_tier_counters": [
            _id(), _event(), sa.Column("tier", sa.String(10), nullable=False), _int("reveals", 0), *_stamps(False),
            sa.UniqueConstraint("event_id", "tier", name="uq_game_tier_counters"),
        ],
        "game_reveals": [
            _id(), _event(), _reg(), sa.Column("hero_id", UUID(as_uuid=True), sa.ForeignKey("game_heroes.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("tier", sa.String(10), nullable=False), _bool("forced", False), *_stamps(False),
            sa.UniqueConstraint("registration_id", name="uq_game_reveals_registration"),
        ],
        "game_deck_items": [
            _id(), _event(), _reg(), sa.Column("hero_id", UUID(as_uuid=True), sa.ForeignKey("game_heroes.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("source", sa.String(8), nullable=False),
            sa.Column("from_registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True), *_stamps(False),
            sa.UniqueConstraint("registration_id", "hero_id", name="uq_game_deck_items"),
            sa.CheckConstraint("source IN ('reveal', 'collect')", name="ck_game_deck_items_source"),
        ],
        "game_meetings": [
            _id(), _event(), _reg("collector_id"), _reg("owner_id"), _bool("scanned", True), *_stamps(False),
            sa.UniqueConstraint("collector_id", "owner_id", name="uq_game_meetings_pair"),
        ],
        "game_card_scores": [
            _id(), _event(), _reg(), _int("distinct_heroes", 0), _at("reached_at", False, server_default=NOW), _at("completed_at"), *_stamps(False),
            sa.UniqueConstraint("registration_id", name="uq_game_card_scores_registration"),
        ],
        "game_hunts": [
            _id(), _event(True), sa.Column("status", sa.String(10), nullable=False, server_default="draft"), _int("duration_minutes", 60),
            _at("starts_at"), _at("ends_at"), _at("paused_at"), sa.Column("mode", sa.String(8), nullable=False, server_default="ordered"),
            _int("bonus_n", 10), _int("bonus_pts", 20), _int("hint_cost", 10), sa.Column("leader_registration_id", UUID(as_uuid=True), nullable=True), *_stamps(),
            sa.CheckConstraint("status IN ('draft', 'scheduled', 'live', 'paused', 'ended')", name="ck_game_hunts_status"),
            sa.CheckConstraint("mode IN ('ordered', 'free')", name="ck_game_hunts_mode"),
        ],
        "game_hunt_points": [
            _id(), _event(), sa.Column("number", sa.Integer(), nullable=False), sa.Column("label", sa.String(80), nullable=False),
            sa.Column("clue", sa.String(140), nullable=False), sa.Column("zone", sa.String(40), nullable=True), _int("base_points", 30),
            sa.Column("hint", sa.String(100), nullable=True), *_stamps(),
            sa.UniqueConstraint("event_id", "number", name="uq_game_hunt_points_number", deferrable=True, initially="DEFERRED"),
            sa.CheckConstraint("base_points BETWEEN 30 AND 100", name="ck_game_hunt_points_base"),
        ],
        "game_hunt_claims": [
            _id(), _event(), sa.Column("point_id", UUID(as_uuid=True), sa.ForeignKey("game_hunt_points.id", ondelete="CASCADE"), nullable=False), _reg(),
            sa.Column("claim_rank", sa.Integer(), nullable=False), sa.Column("points_awarded", sa.Integer(), nullable=False), *_stamps(False),
            sa.UniqueConstraint("point_id", "registration_id", name="uq_game_hunt_claims_once"),
            sa.UniqueConstraint("point_id", "claim_rank", name="uq_game_hunt_claims_rank"),
        ],
        "game_hunt_hints": [
            _id(), _event(), sa.Column("point_id", UUID(as_uuid=True), sa.ForeignKey("game_hunt_points.id", ondelete="CASCADE"), nullable=False), _reg(),
            sa.Column("cost", sa.Integer(), nullable=False), *_stamps(False),
            sa.UniqueConstraint("point_id", "registration_id", name="uq_game_hunt_hints_once"),
        ],
        "game_hunt_scores": [
            _id(), _event(), _reg(), _int("total", 0), _int("found", 0), _at("reached_at", False, server_default=NOW), _at("completed_at"), *_stamps(False),
            sa.UniqueConstraint("registration_id", name="uq_game_hunt_scores_registration"),
            sa.CheckConstraint("total >= 0", name="ck_game_hunt_scores_total"),
        ],
        "game_feed": [
            _id(), _event(), sa.Column("game", sa.String(8), nullable=False), sa.Column("type", sa.String(24), nullable=False),
            sa.Column("message", sa.String(200), nullable=False), sa.Column("payload", JSONB(), nullable=True), _bool("hidden", False), *_stamps(False),
        ],
        "game_snapshots": [
            _id(), _event(), sa.Column("game", sa.String(8), nullable=False), sa.Column("bucket", sa.Integer(), nullable=False),
            sa.Column("ranks", JSONB(), nullable=False), _at("taken_at", False, server_default=NOW),
            sa.UniqueConstraint("event_id", "game", "bucket", name="uq_game_snapshots_bucket"),
        ],
    }


INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_game_reveals_event_tier ON game_reveals (event_id, tier)",
    "CREATE INDEX IF NOT EXISTS ix_game_meetings_event ON game_meetings (event_id, scanned)",
    "CREATE INDEX IF NOT EXISTS ix_game_card_scores_board ON game_card_scores (event_id, distinct_heroes DESC, reached_at)",
    "CREATE INDEX IF NOT EXISTS ix_game_hunt_claims_registration ON game_hunt_claims (registration_id)",
    "CREATE INDEX IF NOT EXISTS ix_game_hunt_scores_board ON game_hunt_scores (event_id, total DESC, reached_at)",
    "CREATE INDEX IF NOT EXISTS ix_game_feed_event ON game_feed (event_id, created_at)",
)

FLAGS = ("game_cards", "game_hunt")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())
    for name, columns in tables().items():
        if name not in have:
            op.create_table(name, *columns)
    for statement in INDEXES:
        op.execute(statement)
    for flag in FLAGS:
        op.execute(f"ALTER TABLE plan_features ADD COLUMN IF NOT EXISTS {flag} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in reversed(list(tables())):
        op.execute(f"DROP TABLE IF EXISTS {name}")
    for flag in FLAGS:
        op.execute(f"ALTER TABLE plan_features DROP COLUMN IF EXISTS {flag}")
