"""Certificate numbers of their own, and the hashes of the copies handed out.

- event_registrations_link.certificate_no (varchar 20, nullable): the
  certificate's number, CERT-XXXXX-XXXXX from the CSPRNG. It used to be the
  ticket id with CERT- for MOA-, so a certificate shared anywhere gave away
  the ticket id the door scans. New registrations draw one (the column's
  default in the model). Here, every registration that holds no
  certificate yet -- never issued one, never checked in -- draws one too.
  One that holds a certificate keeps none: its number, made from the ticket
  id, is already printed and must keep verifying.
- uq_registrations_certificate_no, unique where not null: the verifier's
  lookup, and no number twice.
- certificate_files: the SHA-256 of each certificate PDF kept in the bucket,
  filled when the public verifier is first asked to check a file.

The column is nullable with no default, so adding it changes only the
catalog. The numbers are written a thousand rows at a time outside a
transaction, each batch re-checking that the row still holds no
certificate (the old code is serving meanwhile), and the index is built
CONCURRENTLY after them (any invalid leftover of a failed run dropped
first), so registrations go on throughout.

Downgrade drops the index, the table and the column; certificates then
read their numbers from the ticket ids again.

Revision ID: certificate_numbers
Revises: mcp_server
"""
import secrets

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "certificate_numbers"
down_revision = "mcp_server"
branch_labels = None
depends_on = None

#: app.core.utils.keys.TICKET_ALPHABET, copied so the migration never changes with the code.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
BATCH = 1000
INDEX = "uq_registrations_certificate_no"

#: Holds no certificate: never issued one (or had one revoked), never checked in.
NONE_HELD = "certificate_no IS NULL AND NOT is_attended AND certificate_issued_at IS NULL AND certificate_revoked_at IS NULL"


def _number() -> str:
    group = lambda: "".join(secrets.choice(ALPHABET) for _ in range(5))  # noqa: E731
    return f"CERT-{group()}-{group()}"


def _backfill() -> None:
    bind = op.get_bind()
    drawn: set[str] = set()
    after = None
    while True:
        ids = [
            str(row[0])
            for row in bind.execute(
                sa.text(
                    f"SELECT id FROM event_registrations_link WHERE {NONE_HELD}"
                    + (" AND id > CAST(:after AS uuid)" if after else "")
                    + " ORDER BY id LIMIT :n"
                ),
                {"after": after, "n": BATCH} if after else {"n": BATCH},
            )
        ]
        if not ids:
            return
        numbers = []
        for _ in ids:
            number = _number()
            while number in drawn:
                number = _number()
            drawn.add(number)
            numbers.append(number)
        bind.execute(
            sa.text(
                "UPDATE event_registrations_link AS r SET certificate_no = v.no "
                "FROM (SELECT unnest(CAST(:ids AS uuid[])) AS id, unnest(CAST(:nos AS text[])) AS no) AS v "
                "WHERE r.id = v.id AND r.certificate_no IS NULL AND NOT r.is_attended "
                "AND r.certificate_issued_at IS NULL AND r.certificate_revoked_at IS NULL"
            ),
            {"ids": ids, "nos": numbers},
        )
        after = ids[-1]


def upgrade() -> None:
    op.add_column("event_registrations_link", sa.Column("certificate_no", sa.String(20), nullable=True))
    op.create_table(
        "certificate_files",
        sa.Column(
            "registration_id", UUID(as_uuid=True),
            sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    with op.get_context().autocommit_block():
        _backfill()
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX} ON event_registrations_link (certificate_no) "
            "WHERE certificate_no IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.drop_table("certificate_files")
    op.drop_column("event_registrations_link", "certificate_no")
