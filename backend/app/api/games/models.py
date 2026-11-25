"""The games add-on: Hero Cards and the Scavenger Hunt (feature-register/games/CONTRACT.md).

Two separate games an organiser switches on per event. Nothing here is shared
with quests or any other module: every table is prefixed `game_`, belongs to
one event and goes with the event's retention.

- game_settings: one per event: the switches and names of both games, the look.
- game_players: one per registration that has signed in to a game: PIN version,
  attempts and lock, opt-out, session version, player number.
- Cards: game_card_config, game_heroes, game_tier_counters (the rows reveals
  lock), game_reveals, game_deck_items, game_meetings, game_card_scores.
- Hunt: game_hunts, game_hunt_points, game_hunt_claims, game_hunt_hints,
  game_hunt_scores.
- game_feed: the ticker's events. game_snapshots: a board's ranks, once a
  minute, for the up/down arrows.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.storage.fields import S3ImageField
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

NOW = sa.text("now()")

CARDS, HUNT = "cards", "hunt"
KINDS = (CARDS, HUNT)
THEMES = ("neon_arcade", "sunset_pop", "ocean_glow", "mono_night")
LAYOUTS = ("podium", "list")
ROWS = (5, 7, 10)

TIERS = ("common", "uncommon", "rare", "legendary")
PRESETS = ("easy", "medium", "hard", "custom")
#: Tier shares of each difficulty preset, in percent (SPEC §4).
PRESET_SHARES = {
    "easy": {"common": 40, "uncommon": 30, "rare": 20, "legendary": 10},
    "medium": {"common": 52, "uncommon": 30, "rare": 13, "legendary": 5},
    "hard": {"common": 60, "uncommon": 28, "rare": 10, "legendary": 2},
}

DRAFT, SCHEDULED, LIVE, PAUSED, ENDED = "draft", "scheduled", "live", "paused", "ended"
HUNT_STATUSES = (DRAFT, SCHEDULED, LIVE, PAUSED, ENDED)
ORDERED, FREE = "ordered", "free"
MODES = (ORDERED, FREE)

#: Hero art: portrait 3:4, each size a box the picture is fitted inside.
ART_SIZES = {
    "thumbnail": {"width": 240, "height": 320},
    "medium": {"width": 600, "height": 800},
    "large": {"width": 900, "height": 1200},
}


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)


def _event():
    return Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)


def _registration():
    return Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)


def _at(nullable: bool = True, **kw):
    return Column(TZAwareDateTime(timezone=True), nullable=nullable, **kw)


def _created():
    return Column("created_at", TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())


def _updated():
    return Column("updated_at", TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())


class GameSettings(AbstractSQLModel):
    __tablename__ = "game_settings"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True)
    cards_on = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    cards_name = Column(String(40), nullable=False, default="Hero Cards", server_default="Hero Cards")
    hunt_on = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    hunt_name = Column(String(40), nullable=False, default="Scavenger Hunt", server_default="Scavenger Hunt")
    theme = Column(String(16), nullable=False, default="neon_arcade", server_default="neon_arcade")
    accent = Column(String(9), nullable=True)
    logo_url = Column(String(500), nullable=True)
    board_layout = Column(String(8), nullable=False, default="podium", server_default="podium")
    board_rows = Column(Integer, nullable=False, default=7, server_default="7")
    ticker_on = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    join_box_on = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    #: The venue screen's own colours over the theme: {background, surface, text,
    #: accent, gold, silver, bronze}, each "#RRGGBB" or absent (null: the theme's).
    colors = Column(JSONB, nullable=True)
    screen_version = Column(Integer, nullable=False, default=1, server_default="1")
    #: The last player number handed out: players are numbered 1, 2, 3 per event.
    player_seq = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (
        sa.CheckConstraint("theme IN ('neon_arcade', 'sunset_pop', 'ocean_glow', 'mono_night')", name="ck_game_settings_theme"),
        sa.CheckConstraint("board_layout IN ('podium', 'list')", name="ck_game_settings_layout"),
        sa.CheckConstraint("board_rows IN (0, 5, 7, 10, 25, 50)", name="ck_game_settings_rows"),
    )


class GamePlayers(AbstractSQLModel):
    __tablename__ = "game_players"

    id = _uuid_pk()
    event_id = _event()
    registration_id = _registration()
    player_no = Column(Integer, nullable=False)
    pin_version = Column(Integer, nullable=False, default=0, server_default="0")
    pin_failed = Column(Integer, nullable=False, default=0, server_default="0")
    pin_locked_until = _at()
    #: Shown on the boards by name; false: "Player #N".
    public = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    #: A new session bumps it; a token of an older one is refused.
    session_version = Column(Integer, nullable=False, default=0, server_default="0")
    last_seen_at = _at()
    last_collect_at = _at()
    last_scan_at = _at()
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("event_id", "registration_id", name="uq_game_players_registration"),
        sa.UniqueConstraint("event_id", "player_no", name="uq_game_players_no"),
    )


class GameCardConfig(AbstractSQLModel):
    __tablename__ = "game_card_config"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True)
    preset = Column(String(8), nullable=False, default="medium", server_default="medium")
    share_common = Column(Numeric(5, 2), nullable=False, default=52, server_default="52")
    share_uncommon = Column(Numeric(5, 2), nullable=False, default=30, server_default="30")
    share_rare = Column(Numeric(5, 2), nullable=False, default=13, server_default="13")
    share_legendary = Column(Numeric(5, 2), nullable=False, default=5, server_default="5")
    legendary_floor = Column(Integer, nullable=False, default=4, server_default="4")
    pin_required = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    collect_on = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    two_way = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    show_holders = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (sa.CheckConstraint("legendary_floor >= 1", name="ck_game_card_config_floor"),)


class GameHeroes(AbstractSQLModel):
    __tablename__ = "game_heroes"

    id = _uuid_pk()
    event_id = _event()
    number = Column(Integer, nullable=False)
    name = Column(String(18), nullable=False)
    tagline = Column(String(60), nullable=False, default="", server_default="")
    tier = Column(String(10), nullable=False)
    art = Column(S3ImageField(upload_to="events/games/heroes/", variations=ART_SIZES, public=True), nullable=True)
    emblem_key = Column(String(24), nullable=True)
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (
        sa.UniqueConstraint("event_id", "number", name="uq_game_heroes_number", deferrable=True, initially="DEFERRED"),
        sa.CheckConstraint("tier IN ('common', 'uncommon', 'rare', 'legendary')", name="ck_game_heroes_tier"),
    )


class GameTierCounters(AbstractSQLModel):
    """One row per event and tier: reveals so far. A reveal locks them all."""

    __tablename__ = "game_tier_counters"

    id = _uuid_pk()
    event_id = _event()
    tier = Column(String(10), nullable=False)
    reveals = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = _created()

    __table_args__ = (sa.UniqueConstraint("event_id", "tier", name="uq_game_tier_counters"),)


class GameReveals(AbstractSQLModel):
    __tablename__ = "game_reveals"

    id = _uuid_pk()
    event_id = _event()
    registration_id = _registration()
    hero_id = Column(UUID(as_uuid=True), ForeignKey("game_heroes.id", ondelete="RESTRICT"), nullable=False)
    tier = Column(String(10), nullable=False)
    forced = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("registration_id", name="uq_game_reveals_registration"),
        sa.Index("ix_game_reveals_event_tier", "event_id", "tier"),
    )


class GameDeckItems(AbstractSQLModel):
    __tablename__ = "game_deck_items"

    id = _uuid_pk()
    event_id = _event()
    registration_id = _registration()
    hero_id = Column(UUID(as_uuid=True), ForeignKey("game_heroes.id", ondelete="RESTRICT"), nullable=False)
    source = Column(String(8), nullable=False)
    from_registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("registration_id", "hero_id", name="uq_game_deck_items"),
        sa.CheckConstraint("source IN ('reveal', 'collect')", name="ck_game_deck_items_source"),
    )


class GameMeetings(AbstractSQLModel):
    __tablename__ = "game_meetings"

    id = _uuid_pk()
    event_id = _event()
    collector_id = _registration()
    owner_id = _registration()
    #: The scan was this collector's own (a two-way collect writes the other side too).
    scanned = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("collector_id", "owner_id", name="uq_game_meetings_pair"),
        sa.Index("ix_game_meetings_event", "event_id", "scanned"),
    )


class GameCardScores(AbstractSQLModel):
    __tablename__ = "game_card_scores"

    id = _uuid_pk()
    event_id = _event()
    registration_id = _registration()
    distinct_heroes = Column(Integer, nullable=False, default=0, server_default="0")
    reached_at = _at(False, server_default=NOW, default=sa.func.now())
    completed_at = _at()
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("registration_id", name="uq_game_card_scores_registration"),
        sa.Index("ix_game_card_scores_board", "event_id", sa.text("distinct_heroes DESC"), "reached_at"),
    )


class GameHunts(AbstractSQLModel):
    __tablename__ = "game_hunts"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = Column(String(10), nullable=False, default=DRAFT, server_default=DRAFT)
    duration_minutes = Column(Integer, nullable=False, default=60, server_default="60")
    starts_at = _at()
    ends_at = _at()
    paused_at = _at()
    mode = Column(String(8), nullable=False, default=ORDERED, server_default=ORDERED)
    bonus_n = Column(Integer, nullable=False, default=10, server_default="10")
    bonus_pts = Column(Integer, nullable=False, default=20, server_default="20")
    hint_cost = Column(Integer, nullable=False, default=10, server_default="10")
    #: Who leads the board, so a new leader is announced once.
    leader_registration_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (
        sa.CheckConstraint("status IN ('draft', 'scheduled', 'live', 'paused', 'ended')", name="ck_game_hunts_status"),
        sa.CheckConstraint("mode IN ('ordered', 'free')", name="ck_game_hunts_mode"),
    )


class GameHuntPoints(AbstractSQLModel):
    __tablename__ = "game_hunt_points"

    id = _uuid_pk()
    event_id = _event()
    number = Column(Integer, nullable=False)
    label = Column(String(80), nullable=False)
    clue = Column(String(140), nullable=False)
    zone = Column(String(40), nullable=True)
    base_points = Column(Integer, nullable=False, default=30, server_default="30")
    hint = Column(String(100), nullable=True)
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (
        sa.UniqueConstraint("event_id", "number", name="uq_game_hunt_points_number", deferrable=True, initially="DEFERRED"),
        sa.CheckConstraint("base_points BETWEEN 30 AND 100", name="ck_game_hunt_points_base"),
    )


class GameHuntClaims(AbstractSQLModel):
    __tablename__ = "game_hunt_claims"

    id = _uuid_pk()
    event_id = _event()
    point_id = Column(UUID(as_uuid=True), ForeignKey("game_hunt_points.id", ondelete="CASCADE"), nullable=False)
    registration_id = _registration()
    claim_rank = Column(Integer, nullable=False)
    points_awarded = Column(Integer, nullable=False)
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("point_id", "registration_id", name="uq_game_hunt_claims_once"),
        sa.UniqueConstraint("point_id", "claim_rank", name="uq_game_hunt_claims_rank"),
        sa.Index("ix_game_hunt_claims_registration", "registration_id"),
    )


class GameHuntHints(AbstractSQLModel):
    __tablename__ = "game_hunt_hints"

    id = _uuid_pk()
    event_id = _event()
    point_id = Column(UUID(as_uuid=True), ForeignKey("game_hunt_points.id", ondelete="CASCADE"), nullable=False)
    registration_id = _registration()
    cost = Column(Integer, nullable=False)
    created_at = _created()

    __table_args__ = (sa.UniqueConstraint("point_id", "registration_id", name="uq_game_hunt_hints_once"),)


class GameHuntScores(AbstractSQLModel):
    __tablename__ = "game_hunt_scores"

    id = _uuid_pk()
    event_id = _event()
    registration_id = _registration()
    total = Column(Integer, nullable=False, default=0, server_default="0")
    found = Column(Integer, nullable=False, default=0, server_default="0")
    reached_at = _at(False, server_default=NOW, default=sa.func.now())
    completed_at = _at()
    created_at = _created()

    __table_args__ = (
        sa.UniqueConstraint("registration_id", name="uq_game_hunt_scores_registration"),
        sa.CheckConstraint("total >= 0", name="ck_game_hunt_scores_total"),
        sa.Index("ix_game_hunt_scores_board", "event_id", sa.text("total DESC"), "reached_at"),
    )


class GameFeed(AbstractSQLModel):
    __tablename__ = "game_feed"

    id = _uuid_pk()
    event_id = _event()
    game = Column(String(8), nullable=False)
    type = Column(String(24), nullable=False)
    message = Column(String(200), nullable=False)
    payload = Column(JSONB, nullable=True)
    hidden = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at = _created()

    __table_args__ = (sa.Index("ix_game_feed_event", "event_id", "created_at"),)


class GameSnapshots(AbstractSQLModel):
    """A board's ranks, one per minute at most: what the up and down arrows compare with."""

    __tablename__ = "game_snapshots"

    id = _uuid_pk()
    event_id = _event()
    game = Column(String(8), nullable=False)
    bucket = Column(Integer, nullable=False)
    ranks = Column(JSONB, nullable=False)
    taken_at = _at(False, server_default=NOW, default=sa.func.now())

    __table_args__ = (sa.UniqueConstraint("event_id", "game", "bucket", name="uq_game_snapshots_bucket"),)
