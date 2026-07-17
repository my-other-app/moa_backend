"""money_columns_to_numeric

Convert money columns from Float to Numeric(10, 2) for exact fixed-point
storage/arithmetic (Postgres-side SUM and threshold comparisons on Float
columns can drift; Numeric doesn't). Also fixes a pre-existing unit
inconsistency: payment_logs.amount was stored in paise (Razorpay's native
unit) while every other money column is in rupees — this migration
normalizes it to rupees for existing rows too, in the same cast.

Revision ID: money_columns_to_numeric
Revises: add_user_token_version
"""
from alembic import op
import sqlalchemy as sa


revision = 'money_columns_to_numeric'
down_revision = 'add_user_token_version'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'payment_orders', 'amount',
        type_=sa.Numeric(10, 2),
        postgresql_using='amount::numeric(10,2)',
    )
    op.alter_column(
        'payment_logs', 'amount',
        type_=sa.Numeric(10, 2),
        postgresql_using='(amount / 100.0)::numeric(10,2)',
    )
    op.alter_column(
        'events', 'reg_fee',
        type_=sa.Numeric(10, 2),
        postgresql_using='reg_fee::numeric(10,2)',
    )
    op.alter_column(
        'events', 'prize_amount',
        type_=sa.Numeric(10, 2),
        postgresql_using='prize_amount::numeric(10,2)',
    )
    op.alter_column(
        'event_registrations_link', 'actual_amount',
        type_=sa.Numeric(10, 2),
        postgresql_using='actual_amount::numeric(10,2)',
    )
    op.alter_column(
        'event_registrations_link', 'paid_amount',
        type_=sa.Numeric(10, 2),
        postgresql_using='paid_amount::numeric(10,2)',
    )


def downgrade() -> None:
    op.alter_column(
        'event_registrations_link', 'paid_amount',
        type_=sa.Float(),
        postgresql_using='paid_amount::float',
    )
    op.alter_column(
        'event_registrations_link', 'actual_amount',
        type_=sa.Float(),
        postgresql_using='actual_amount::float',
    )
    op.alter_column(
        'events', 'prize_amount',
        type_=sa.Float(),
        postgresql_using='prize_amount::float',
    )
    op.alter_column(
        'events', 'reg_fee',
        type_=sa.Float(),
        postgresql_using='reg_fee::float',
    )
    op.alter_column(
        'payment_logs', 'amount',
        type_=sa.Float(),
        postgresql_using='(amount * 100.0)::float',
    )
    op.alter_column(
        'payment_orders', 'amount',
        type_=sa.Float(),
        postgresql_using='amount::float',
    )
