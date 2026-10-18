"""scripts/check_migrations.py: the migration lint, over the real versions
folder and over migrations written here to break each rule once."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import check_migrations as cm

BACKEND = Path(__file__).resolve().parents[1]

#: What CONTRACT.md asks of a migration that touches a hot table. Nothing in
#: it may be flagged.
GOOD = '''
import sqlalchemy as sa
from alembic import op

revision = "w2_good"
down_revision = "w2_base"

INDEXES = (
    ("ix_events_new", "events (club_id, starts_at)"),
    ("ix_users_new", "users (lower(email))"),
)
BATCH = """
UPDATE events SET score = 0
WHERE id IN (SELECT id FROM events WHERE score IS NULL LIMIT 5000)
"""


def upgrade():
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("events", sa.Column("score", sa.Integer(), nullable=True))
    op.add_column("events", sa.Column("flag", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("ALTER TABLE users ADD COLUMN level int NOT NULL DEFAULT 0")
    op.create_table("brand_new", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("n", sa.Integer(), nullable=False))
    op.create_index("ix_brand_new_n", "brand_new", ["n"])
    op.execute("UPDATE brand_new SET n = 1")
    op.execute("ALTER TYPE mood ADD VALUE IF NOT EXISTS 'calm'")
    op.execute("UPDATE events SET score = 1 WHERE id = :id")
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while bind.execute(sa.text(BATCH)).rowcount:
            pass
        for name, definition in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")
        op.create_index("ix_clubs_new", "clubs", ["slug"], postgresql_concurrently=True)
        op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_new")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("clubs", "old")


def downgrade():
    op.drop_column("events", "score")  # downgrades are not read
'''


def _write(tmp_path: Path, body: str, revision: str = "w2_x", down: str = "w2_base", name: str = None) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "base.py").write_text('revision = "w2_base"\ndown_revision = None\n\ndef upgrade():\n    pass\n')
    source = textwrap.dedent(body) if "revision =" in body else (
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        f'revision = "{revision}"\ndown_revision = "{down}"\n\n\ndef upgrade():\n'
        + textwrap.indent(textwrap.dedent(body).strip() + "\n", "    ")
    )
    (versions / f"{name or revision}.py").write_text(source)
    return versions


def _rules(versions: Path, since="w2_base") -> list[str]:
    findings, _ = cm.check(versions, since=since)
    return [f.rule for f in findings]


def test_the_real_migrations_after_the_cutoff_are_clean():
    """Every migration added after the cutoff (topics) is lock-safe, or says
    on its line why a finding does not apply."""
    findings, checked = cm.check()
    assert not findings, "\n".join(str(f) for f in findings)


def test_every_real_migration_can_be_read():
    """--since none reads all of them without falling over, and finds what
    the old ones are known to do (money_columns_to_numeric changed column
    types on payment tables; nothing there is rewritten to please it)."""
    findings, checked = cm.check(since=None)
    assert len(checked) == len(list((BACKEND / "migrations" / "versions").glob("*.py")))
    assert any(f.rule == "alter-type" and f.path.endswith("money_columns_to_numeric.py") for f in findings)
    assert any(f.rule == "missing-lock-timeout" and f.path.endswith("money_integrity.py") for f in findings)


def test_the_cutoff_and_its_ancestors_are_not_checked(tmp_path):
    versions = _write(tmp_path, 'op.create_index("ix", "events", ["a"])', revision="w2_new")
    (versions / "older.py").write_text(
        'from alembic import op\nrevision = "w2_older"\ndown_revision = None\n\n'
        'def upgrade():\n    op.create_index("ix", "events", ["a"])\n'
    )
    (versions / "base.py").write_text('revision = "w2_base"\ndown_revision = "w2_older"\n\ndef upgrade():\n    pass\n')
    findings, checked = cm.check(versions, since="w2_base")
    assert checked == ["w2_new"]
    assert {f.rule for f in findings} == {"index-not-concurrent", "missing-lock-timeout"}


def test_the_contracts_pattern_passes(tmp_path):
    versions = _write(tmp_path, GOOD, name="good")
    findings, checked = cm.check(versions, since="w2_base")
    assert checked == ["w2_good"]
    assert findings == [], "\n".join(str(f) for f in findings)


@pytest.mark.parametrize(
    "body, rule",
    [
        ('op.execute("SET LOCAL lock_timeout = \'3s\'")\nop.create_index("ix_e", "events", ["club_id"])', "index-not-concurrent"),
        ('op.execute("SET LOCAL lock_timeout = \'3s\'")\nop.execute("CREATE UNIQUE INDEX ix_u ON public.users (lower(email))")', "index-not-concurrent"),
        ('op.execute("CREATE INDEX CONCURRENTLY ix ON events (a)")', "concurrently-in-transaction"),
        ('op.create_index("ix", "clubs", ["a"], postgresql_concurrently=True)', "concurrently-in-transaction"),
        ('op.drop_index("ix", table_name="clubs", postgresql_concurrently=True)', "concurrently-in-transaction"),
        ('op.add_column("plans", sa.Column("tier", sa.Integer(), nullable=False))', "not-null-without-default"),
        ('op.execute("ALTER TABLE plans ADD COLUMN tier int NOT NULL")', "not-null-without-default"),
        ('op.execute("UPDATE plans SET tier = 1")', "full-table-update"),
        ('op.execute("SET LOCAL lock_timeout = \'3s\'")\nop.execute("UPDATE events SET score = 0 WHERE score IS NULL")', "full-table-update"),
        (
            'bind = op.get_bind()\nwhile bind.execute(sa.text("UPDATE events SET s = 0 WHERE id IN '
            '(SELECT id FROM events WHERE s IS NULL LIMIT 1000)")).rowcount:\n    pass',
            "full-table-update",
        ),
        ('op.alter_column("plans", "price", type_=sa.Numeric(12, 2))', "alter-type"),
        ('op.execute("ALTER TABLE plans ALTER COLUMN price TYPE numeric(12,2)")', "alter-type"),
        ('op.execute("ALTER TYPE address ALTER ATTRIBUTE city TYPE text")', "alter-type"),
        ('op.add_column("events", sa.Column("x", sa.Integer(), nullable=True))', "missing-lock-timeout"),
        ('op.execute("ALTER TABLE payment_logs ADD COLUMN x int")', "missing-lock-timeout"),
        ('op.create_foreign_key("fk", "brand_new_elsewhere", "users", ["user_id"], ["id"])', "missing-lock-timeout"),
        ('op.execute("CREATE TRIGGER t AFTER INSERT ON event_registrations_link FOR EACH ROW EXECUTE FUNCTION f()")', "missing-lock-timeout"),
    ],
)
def test_each_rule_fires(tmp_path, body, rule):
    assert rule in _rules(_write(tmp_path, body))


def test_lock_timeout_does_not_survive_an_autocommit_block(tmp_path):
    """The block commits the transaction the SET LOCAL was in; DDL after it
    is in a new one and needs its own."""
    versions = _write(tmp_path, """
        op.execute("SET LOCAL lock_timeout = '3s'")
        op.add_column("events", sa.Column("a", sa.Integer()))
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix ON events (a)")
        op.add_column("events", sa.Column("b", sa.Integer()))
    """)
    findings, _ = cm.check(versions, since="w2_base")
    assert [(f.rule, f.line) for f in findings] == [("missing-lock-timeout", 13)]


def test_sql_in_module_constants_and_loops_is_followed(tmp_path):
    versions = _write(tmp_path, '''
        import sqlalchemy as sa
        from alembic import op

        revision = "w2_loops"
        down_revision = "w2_base"

        CHECKS = (
            ("events", "ck_a", "a >= 0"),
            ("plans", "ck_b", "b >= 0"),
        )
        CLEAN_UP = ("UPDATE plans SET b = 0",)


        def _columns(table):
            op.execute(f"ALTER TABLE {table} ADD COLUMN c int NOT NULL")


        def upgrade():
            for table, name, check in CHECKS:
                op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID")
            for sql in CLEAN_UP:
                op.execute(sql)
            _columns("clubs")
    ''', name="loops")
    findings, _ = cm.check(versions, since="w2_base")
    found = {(f.rule, f.message.split(" on hot table ")[-1].split()[0] if "hot table" in f.message else "") for f in findings}
    assert ("missing-lock-timeout", "events") in found
    assert ("missing-lock-timeout", "plans") not in found  # not hot
    assert "full-table-update" in {f.rule for f in findings}
    assert any(f.rule == "not-null-without-default" and "clubs" in f.message for f in findings)


def test_a_finding_can_be_silenced_with_a_reason(tmp_path):
    versions = _write(tmp_path, """
        op.execute("UPDATE plans SET tier = 1")  # migration-check: ok full-table-update -- eight rows
        # migration-check: ok alter-type, missing-lock-timeout -- the table is empty in production
        op.alter_column("payment_logs", "amount", type_=sa.Numeric(12, 2))
    """)
    assert _rules(versions) == []


def test_the_command_line_exit_status(tmp_path):
    bad = _write(tmp_path, 'op.execute("UPDATE plans SET tier = 1")')
    run = subprocess.run(
        [sys.executable, "scripts/check_migrations.py", str(bad), "--since", "w2_base"],
        cwd=BACKEND, capture_output=True, text=True,
    )
    assert run.returncode == 1
    assert "[full-table-update]" in run.stdout
    clean = subprocess.run([sys.executable, "scripts/check_migrations.py"], cwd=BACKEND, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr
