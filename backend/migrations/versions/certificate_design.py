"""The design an issued certificate keeps (feature-register/doc-designs-contract.md §6).

The designer's ticket, certificate and ID card designs become the defaults
(app/api/doc_presets): every event without a saved design switches to them.
A certificate already issued must never change with them.

- event_registrations_link.certificate_design (json, nullable): what a
  certificate keeps from the design it was issued with
  (app/api/doc_presets/pins.py). Null until issued.
- Every certificate already issued is marked {"mode": "legacy"}: it goes on
  being drawn exactly as it is today -- the event's design as it was before
  presets, through the layered or HTML paths -- whatever the defaults
  become. (The code reads an issued certificate with none as legacy too, so
  one issued by the old code in the seconds a deploy takes is safe.)

The column is nullable with no default, so adding it changes only the
catalog (under lock_timeout 3s: a hot table). The marks are written a
thousand rows at a time outside a transaction, each batch re-checking that
the row is issued and still unmarked, so registrations go on throughout and
running it again changes nothing.

Downgrade drops the column, and every certificate goes back to following
its event's design.

Revision ID: certificate_design
Revises: developer_docs_links
"""
import sqlalchemy as sa
from alembic import op

revision = "certificate_design"
down_revision = "developer_docs_links"
branch_labels = None
depends_on = None

BATCH = 1000
LEGACY = '{"mode": "legacy"}'


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("event_registrations_link", sa.Column("certificate_design", sa.JSON(), nullable=True))
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while True:
            ids = [
                str(row[0])
                for row in bind.execute(
                    sa.text(
                        "SELECT id FROM event_registrations_link "
                        "WHERE certificate_issued_at IS NOT NULL AND certificate_design IS NULL "
                        "ORDER BY id LIMIT :n"
                    ),
                    {"n": BATCH},
                )
            ]
            if not ids:
                break
            bind.execute(
                sa.text(
                    "UPDATE event_registrations_link SET certificate_design = CAST(:legacy AS json) "
                    "WHERE id = ANY(CAST(:ids AS uuid[])) "
                    "AND certificate_issued_at IS NOT NULL AND certificate_design IS NULL"
                ),
                {"ids": ids, "legacy": LEGACY},
            )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("event_registrations_link", "certificate_design")
