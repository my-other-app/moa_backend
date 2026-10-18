"""Attendance integrity: the door, registrations, kiosks and competitions
held by the database, not by check-then-write.

Registrations (event_registrations_link):
- uq_registrations_event_user_live: UNIQUE (event_id, user_id) WHERE NOT
  is_deleted, replacing UNIQUE (event_id, user_id, is_deleted), which allowed
  one retired row per account and event, so a second removal was a 500. The
  old constraint already kept live rows unique, so there is nothing to
  de-duplicate (a failed build would say otherwise, and change nothing).
- uq_registrations_event_admission: UNIQUE (event_id, upper(admission_no))
  WHERE NOT is_deleted AND admission_no IS NOT NULL. Duplicates first: the
  oldest registration keeps the number, each later one gets "-2", "-3", ...
  appended (logged, with the count). The number stays findable in search.
- ix_registrations_event_name: (event_id, lower(full_name), id) WHERE NOT
  is_deleted, which the attendee list now pages through in order.
- Ticket ids are looked up upper-cased on the existing unique index; ids
  that are not upper case are counted and logged (none are minted so) and
  still found, as typed.

Doors: uq_doors_event_lower_name UNIQUE (event_id, lower(name)), replacing
uq_doors_event_name (case-sensitive, while doors are found by lower(name)).
Doors whose names differ only in case are renamed "Gate A (2)", ... (the
oldest keeps its name; logged); nothing that points at a door moves.

Kiosks: events.kiosk_pin_hash, a salted PBKDF2 hash of the staff PIN (the
format of app/api/kiosk/pins.py); every PIN set is hashed here and the plain
kiosk_pin emptied (the column stays, unmapped, until a later migration drops
it). kiosk_devices.pin_attempts and pin_window_at count PIN tries per kiosk.

Competitions:
- competition_team_members.competition_id, filled from the team, NOT NULL,
  and uq_competition_members_once UNIQUE (competition_id, registration_id).
  A ticket on two teams of one competition keeps its oldest membership; the
  later ones are removed (logged).
- uq_competition_teams_name UNIQUE (competition_id, lower(name)); clashing
  names become "Name (2)", ... (oldest keeps it; logged).
- uq_competition_slots_team UNIQUE (mentor_id, team_id) WHERE team_id IS
  NOT NULL: a team's second slot with one mentor is freed (the earliest is
  kept; logged).
- competitions.score_method, "mean" (as today) or "zscore".

Lock-light: the columns in one short transaction (catalog only, or a
default Postgres 11+ keeps in the catalog); then each data fix in its own
short transaction, run again right before the unique index it clears the way
for; every index built CONCURRENTLY outside a transaction, an invalid one
from a failed run dropped and built again. A constraint that replaces an
index is attached to the index already built (UNIQUE USING INDEX).

Downgrade drops what this adds. The old UNIQUE (event_id, user_id,
is_deleted) comes back only if no account has two retired registrations for
one event; otherwise the live-only index is left in its place (logged).
Kiosk PINs cannot be un-hashed: organisers set them again. Renamed doors and
teams, re-numbered admissions, removed duplicate memberships and freed slots
stay as they are.

Revision ID: attendance_integrity
Revises: discover_scale
"""
import base64
import hashlib
import logging
import secrets

import sqlalchemy as sa
from alembic import op

revision = "attendance_integrity"
down_revision = "discover_scale"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

REG = "event_registrations_link"

INDEXES = (
    # (name, table, UNIQUE or "", definition after ON <table>)
    ("uq_registrations_event_user_live", REG, "UNIQUE", "(event_id, user_id) WHERE NOT is_deleted"),
    ("uq_registrations_event_admission", REG, "UNIQUE", "(event_id, upper(admission_no)) WHERE NOT is_deleted AND admission_no IS NOT NULL"),
    ("ix_registrations_event_name", REG, "", "(event_id, lower(full_name), id) WHERE NOT is_deleted"),
    ("uq_doors_event_lower_name", "doors", "UNIQUE", "(event_id, lower(name))"),
    ("uq_competition_members_once", "competition_team_members", "UNIQUE", "(competition_id, registration_id)"),
    ("uq_competition_teams_name", "competition_teams", "UNIQUE", "(competition_id, lower(name))"),
    ("uq_competition_slots_team", "competition_slots", "UNIQUE", "(mentor_id, team_id) WHERE team_id IS NOT NULL"),
)

# ─── Data fixes, each returning how many rows it changed ──────────────────

#: Admission numbers held twice on one event (case ignored): every holder but
#: the oldest gets "-n" appended. Run until none clash, as a suffix could.
#: The clashes are found with one hashed pass; only their rows are sorted.
_ADMISSIONS = f"""
WITH clash AS (
  SELECT event_id, upper(admission_no) AS number FROM {REG}
  WHERE NOT is_deleted AND admission_no IS NOT NULL
  GROUP BY 1, 2 HAVING count(*) > 1
)
UPDATE {REG} r SET admission_no = left(r.admission_no, 36) || '-' || d.n
FROM (
  SELECT x.id, row_number() OVER (PARTITION BY x.event_id, upper(x.admission_no) ORDER BY x.created_at, x.id) AS n
  FROM {REG} x JOIN clash c ON c.event_id = x.event_id AND c.number = upper(x.admission_no)
  WHERE NOT x.is_deleted
) d
WHERE r.id = d.id AND d.n > 1
"""

#: Doors named alike but for case: every one but the oldest renamed "Name (n)".
_DOORS = """
UPDATE doors x SET name = left(x.name, 34) || ' (' || d.n || ')'
FROM (
  SELECT id, row_number() OVER (PARTITION BY event_id, lower(name) ORDER BY created_at, id) AS n FROM doors
) d
WHERE x.id = d.id AND d.n > 1
"""

_TEAMS = """
UPDATE competition_teams x SET name = left(x.name, 74) || ' (' || d.n || ')'
FROM (
  SELECT id, row_number() OVER (PARTITION BY competition_id, lower(name) ORDER BY created_at, id) AS n FROM competition_teams
) d
WHERE x.id = d.id AND d.n > 1
"""

#: A ticket on two teams of one competition: the oldest membership stays.
_MEMBERS = """
DELETE FROM competition_team_members m USING (
  SELECT id, row_number() OVER (PARTITION BY competition_id, registration_id ORDER BY created_at, id) AS n
  FROM competition_team_members WHERE competition_id IS NOT NULL
) d
WHERE m.id = d.id AND d.n > 1
"""

#: A team's second slot with one mentor is freed; the earliest stays booked.
_SLOTS = """
UPDATE competition_slots x SET team_id = NULL
FROM (
  SELECT id, row_number() OVER (PARTITION BY mentor_id, team_id ORDER BY starts_at, id) AS n
  FROM competition_slots WHERE team_id IS NOT NULL
) d
WHERE x.id = d.id AND d.n > 1
"""

_FIXES = {
    "uq_registrations_event_admission": ("admission numbers renumbered", _ADMISSIONS, True),
    "uq_doors_event_lower_name": ("doors renamed", _DOORS, True),
    "uq_competition_teams_name": ("teams renamed", _TEAMS, True),
    "uq_competition_members_once": ("duplicate team memberships removed", _MEMBERS, False),
    "uq_competition_slots_team": ("second slots with one mentor freed", _SLOTS, False),
}


def _state(name: str):
    """None when there is no such index, else whether it is valid."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"name": name},
    ).first()
    return None if row is None else bool(row[0])


def _build(name: str, table: str, unique: str, definition: str) -> None:
    """CREATE [UNIQUE] INDEX CONCURRENTLY, once: a valid one is left alone,
    an invalid one (a failed build) dropped and built again. Autocommit."""
    state = _state(name)
    if state:
        return
    if state is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE {unique} INDEX CONCURRENTLY {name} ON {table} {definition}")


def _in_transaction(fn):
    bind = op.get_bind()
    bind.execute(sa.text("BEGIN"))
    try:
        out = fn(bind)
    except Exception:
        bind.execute(sa.text("ROLLBACK"))
        raise
    bind.execute(sa.text("COMMIT"))
    return out


def _fix(name: str) -> None:
    """The data fix that clears the way for unique index `name`, in its own
    transaction, logged when it changed anything."""
    what, sql, repeat = _FIXES[name]

    def run(bind):
        total = 0
        for _ in range(10 if repeat else 1):
            n = bind.execute(sa.text(sql)).rowcount or 0
            total += n
            if not n:
                break
        return total

    total = _in_transaction(run)
    if total:
        log.warning("attendance_integrity: %d %s before %s", total, what, name)


def _constraint_on(table: str, columns: list[str]):
    """The name of the UNIQUE constraint on exactly these columns, if any."""
    row = op.get_bind().execute(
        sa.text(
            """
            SELECT c.conname FROM pg_constraint c
            WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'u'
              AND (SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_attribute a
                   WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)) = CAST(:cols AS text[])
            """
        ),
        {"table": table, "cols": sorted(columns)},
    ).first()
    return row[0] if row else None


def _column_exists(table: str, column: str) -> bool:
    return bool(op.get_bind().execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).first())


def _hash_pin(pin: str) -> str:
    # The format of app/api/kiosk/pins.hash_pin, which reads it.
    salt = secrets.token_bytes(16)
    iterations = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    b64 = lambda raw: base64.b64encode(raw).decode().rstrip("=")  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


def _hash_pins(bind) -> int:
    if not _column_exists("events", "kiosk_pin"):
        return 0
    rows = bind.execute(sa.text("SELECT id, kiosk_pin FROM events WHERE kiosk_pin IS NOT NULL")).all()
    for event_id, pin in rows:
        pin = (pin or "").strip()
        bind.execute(
            sa.text("UPDATE events SET kiosk_pin_hash = :h, kiosk_pin = NULL WHERE id = :id"),
            {"h": _hash_pin(pin) if pin else None, "id": event_id},
        )
    return len(rows)


def upgrade() -> None:
    # Fast, and first: the columns the new code reads.
    op.add_column("events", sa.Column("kiosk_pin_hash", sa.String(160), nullable=True))
    op.add_column("kiosk_devices", sa.Column("pin_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("kiosk_devices", sa.Column("pin_window_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("competitions", sa.Column("score_method", sa.String(10), nullable=False, server_default="mean"))
    op.add_column(
        "competition_team_members",
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE", name="fk_competition_team_members_competition"), nullable=True),
    )

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        hashed = _in_transaction(_hash_pins)
        if hashed:
            log.warning("attendance_integrity: %d kiosk PINs hashed; the plain column is emptied", hashed)
        lower = bind.execute(sa.text(f"SELECT count(*) FROM {REG} WHERE ticket_id <> upper(ticket_id)")).scalar() or 0
        if lower:
            log.warning("attendance_integrity: %d ticket ids are not upper case; the door still finds them as typed", lower)

        # Each membership's competition, from its team; then NOT NULL.
        _in_transaction(lambda b: b.execute(sa.text(
            "UPDATE competition_team_members m SET competition_id = t.competition_id "
            "FROM competition_teams t WHERE t.id = m.team_id AND m.competition_id IS DISTINCT FROM t.competition_id"
        )))
        _in_transaction(lambda b: b.execute(sa.text("ALTER TABLE competition_team_members ALTER COLUMN competition_id SET NOT NULL")))

        for name, table, unique, definition in INDEXES:
            if name in _FIXES and not _state(name):
                _fix(name)
            _build(name, table, unique, definition)

        # The constraints the new indexes replace, or become.
        old = _constraint_on(REG, ["event_id", "user_id", "is_deleted"])
        if old:
            op.execute(f'ALTER TABLE {REG} DROP CONSTRAINT "{old}"')
        op.execute("ALTER TABLE doors DROP CONSTRAINT IF EXISTS uq_doors_event_name")
        if not _constraint_on("competition_team_members", ["competition_id", "registration_id"]):
            op.execute("ALTER TABLE competition_team_members ADD CONSTRAINT uq_competition_members_once UNIQUE USING INDEX uq_competition_members_once")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        op.execute("ALTER TABLE competition_team_members DROP CONSTRAINT IF EXISTS uq_competition_members_once")
        # The case-sensitive door constraint: the case-insensitive index
        # already keeps names unique, so this cannot fail.
        if not _constraint_on("doors", ["event_id", "name"]):
            _build("uq_doors_event_name", "doors", "UNIQUE", "(event_id, name)")
            op.execute("ALTER TABLE doors ADD CONSTRAINT uq_doors_event_name UNIQUE USING INDEX uq_doors_event_name")
        retired_twice = bind.execute(sa.text(
            f"SELECT count(*) FROM (SELECT 1 FROM {REG} GROUP BY event_id, user_id, is_deleted HAVING count(*) > 1) s"
        )).scalar() or 0
        keep_live_index = bool(retired_twice)
        if keep_live_index:
            log.warning(
                "attendance_integrity downgrade: %d accounts hold two retired registrations for one event; "
                "UNIQUE (event_id, user_id, is_deleted) is not restored and uq_registrations_event_user_live stays",
                retired_twice,
            )
        elif not _constraint_on(REG, ["event_id", "user_id", "is_deleted"]):
            _build("event_registrations_link_event_id_user_id_is_deleted_key", REG, "UNIQUE", "(event_id, user_id, is_deleted)")
            op.execute(
                f"ALTER TABLE {REG} ADD CONSTRAINT event_registrations_link_event_id_user_id_is_deleted_key "
                "UNIQUE USING INDEX event_registrations_link_event_id_user_id_is_deleted_key"
            )
        for name, _table, _unique, _definition in reversed(INDEXES):
            if name == "uq_registrations_event_user_live" and keep_live_index:
                continue
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.drop_column("competition_team_members", "competition_id")
    op.drop_column("competitions", "score_method")
    op.drop_column("kiosk_devices", "pin_window_at")
    op.drop_column("kiosk_devices", "pin_attempts")
    # The PINs were hashed: organisers set them again.
    op.drop_column("events", "kiosk_pin_hash")
