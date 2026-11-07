"""A public id for every issued certificate, and a number of its own for every registration.

A certificate's public page was myotherapp.in/certificates/{ticket id}, and
an older certificate's number was the ticket id under CERT-: a certificate
shared anywhere gave away the code the door scans. Both now stand apart
from the ticket.

- event_registrations_link.certificate_public_id (varchar 32, nullable):
  the certificate's public id, 24 URL-safe characters from the CSPRNG (144
  bits), drawn when it is issued (certificates.service.give_public_ids).
  Every public certificate page, link and file is keyed by it. Here every
  certificate already issued draws one.
- uq_registrations_certificate_public_id, unique where not null: the
  public page's lookup, and no id twice.
- certificate_no: every registration still without a number of its own
  (one older than the certificate_numbers migration that already held a
  certificate or had checked in, whose number was made from its ticket id)
  draws one now, CERT-XXXXX-XXXXX, skipping any number already taken. Its
  printed number stops verifying: it was the ticket id, and verifying it
  would keep the ticket id a way into the verifier. The certificate itself,
  downloaded or resent, now carries the new number.

The column is nullable with no default, so adding it changes only the
catalog. The values are written a thousand rows at a time outside a
transaction, each batch re-checking that the row still has none (the old
code is serving meanwhile), and the index is built CONCURRENTLY after them
(any invalid leftover of a failed run dropped first), so registrations go on
throughout. Running it again changes nothing.

Downgrade drops the index and the column. The numbers drawn here stay: the
older code reads a registration's own number first, so they keep working
there too.

Revision ID: certificate_public_ids
Revises: feedback_channels
"""
import secrets

import sqlalchemy as sa
from alembic import op

revision = "certificate_public_ids"
down_revision = "feedback_channels"
branch_labels = None
depends_on = None

#: app.core.utils.keys.TICKET_ALPHABET, copied so the migration never changes with the code.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
#: app.core.utils.keys.CERTIFICATE_PUBLIC_ID_BYTES, likewise.
PUBLIC_ID_BYTES = 18
BATCH = 1000
INDEX = "uq_registrations_certificate_public_id"


def _number() -> str:
    group = lambda: "".join(secrets.choice(ALPHABET) for _ in range(5))  # noqa: E731
    return f"CERT-{group()}-{group()}"


def _batches(bind, where: str, update_sql: str, draw) -> None:
    """Every row matching `where`, a thousand at a time by id: each given a
    fresh value by `update_sql`, which re-checks `where` itself. A row the
    UPDATE skipped (its value was taken meanwhile) is drawn again next time
    round, so the loop runs until none is left."""
    while True:
        after = None
        touched = 0
        while True:
            ids = [
                str(row[0])
                for row in bind.execute(
                    sa.text(
                        f"SELECT id FROM event_registrations_link WHERE {where}"
                        + (" AND id > CAST(:after AS uuid)" if after else "")
                        + " ORDER BY id LIMIT :n"
                    ),
                    {"after": after, "n": BATCH} if after else {"n": BATCH},
                )
            ]
            if not ids:
                break
            drawn: set[str] = set()
            values = []
            for _ in ids:
                value = draw()
                while value in drawn:
                    value = draw()
                drawn.add(value)
                values.append(value)
            touched += bind.execute(sa.text(update_sql), {"ids": ids, "vals": values}).rowcount or 0
            after = ids[-1]
        left = bind.execute(sa.text(f"SELECT count(*) FROM event_registrations_link WHERE {where}")).scalar()
        if not left or not touched:
            return


def upgrade() -> None:
    # A hot table: wait for its lock briefly, and let env.py try again, rather than queue every request behind it.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("event_registrations_link", sa.Column("certificate_public_id", sa.String(32), nullable=True))
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        # A number of its own for every registration without one. One a
        # registration already holds is skipped (uq_registrations_certificate_no).
        _batches(
            bind,
            "certificate_no IS NULL",
            "UPDATE event_registrations_link AS r SET certificate_no = v.val "
            "FROM (SELECT unnest(CAST(:ids AS uuid[])) AS id, unnest(CAST(:vals AS text[])) AS val) AS v "
            "WHERE r.id = v.id AND r.certificate_no IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM event_registrations_link t WHERE t.certificate_no = v.val)",
            _number,
        )
        # A public id for every certificate already issued.
        _batches(
            bind,
            "certificate_issued_at IS NOT NULL AND certificate_public_id IS NULL",
            "UPDATE event_registrations_link AS r SET certificate_public_id = v.val "
            "FROM (SELECT unnest(CAST(:ids AS uuid[])) AS id, unnest(CAST(:vals AS text[])) AS val) AS v "
            "WHERE r.id = v.id AND r.certificate_issued_at IS NOT NULL AND r.certificate_public_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM event_registrations_link t WHERE t.certificate_public_id = v.val)",
            lambda: secrets.token_urlsafe(PUBLIC_ID_BYTES),
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX} ON event_registrations_link (certificate_public_id) "
            "WHERE certificate_public_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("event_registrations_link", "certificate_public_id")
