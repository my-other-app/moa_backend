"""The free trial, Enterprise trials, three paid tiers, and no more Campus.

Schema (club_subscriptions; every column nullable, no default, so adding
each is a catalog change):

- trial_plan_id: the plan a club's free trial gives (Pro) while it runs.
  Every club approved from now on starts one (subscriptions.service.
  start_free_trial): 14 days, no card, no Razorpay, once per club.
- enterprise_trial_plan_id, _started_at, _ends_at, _by, _reminded_at: an
  Enterprise trial an admin grants after a sales call, laid over whatever
  the club has until it ends.

Data:

- trial_days 0 on starter, growth and pro: the free trial replaces each
  plan's own Razorpay trial. A plan picked during the free trial starts
  billing when the free trial ends instead.
- Campus is withdrawn (visibility legacy, never deleted: charges and old
  rows point at it) and folded into Enterprise, which has everything Campus
  had (Pro lacks finance). Every club row on Campus that Razorpay does not
  bill moves to Enterprise as it stands -- a complimentary Campus stays
  complimentary, to the same end date; a deal by hand keeps its price --
  and so do `previous_state` snapshots naming Campus and sign-ups that
  picked it. A row Razorpay bills for Campus is left where it is (a legacy
  plan keeps its subscribers): Razorpay cannot be asked from a migration to
  move it. Production had none when this was written.
- Clubs already on Free get no trial: the trial is for new clubs only.
- plan_limits.storage_gb is left in place, unread: nothing ever metered it
  and the API no longer has it, but the previous code reads the column
  while this migration runs (production migrates before it restarts, and a
  rollback is code-only), and the shipped club_plans seed writes it. Drop it
  in a later release.

Every statement is bounded by the handful of plan rows or the few club rows
on Campus, under lock_timeout 3s.

Downgrade drops the columns (running Enterprise trials end; a free trial
whose end has passed is cancelled first, so the older code, which has no
end for a trial nothing bills, does not keep it going), gives growth and pro
their 14-day trials back, and puts Campus back on sale. Rows moved to
Enterprise stay there.

Revision ID: trial_pricing
Revises: whatsapp_opt_in
"""
import sqlalchemy as sa
from alembic import op

revision = "trial_pricing"
down_revision = "whatsapp_opt_in"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
PAID_TIERS = ("starter", "growth", "pro")


def _plan_id(bind, key: str) -> int | None:
    return bind.execute(
        sa.text("SELECT id FROM plans WHERE key = :key AND NOT is_deleted"), {"key": key}
    ).scalar()


def fold_campus(bind) -> int:
    """Campus withdrawn and its clubs on Enterprise. Returns rows moved."""
    campus, enterprise = _plan_id(bind, "campus"), _plan_id(bind, "enterprise")
    if campus is None:
        return 0
    bind.execute(
        sa.text("UPDATE plans SET visibility = 'legacy', featured = false, updated_at = now() WHERE id = :id"),
        {"id": campus},
    )
    if enterprise is None:
        return 0
    moved = bind.execute(  # migration-check: ok full-table-update -- only the rows on Campus (none or a few)
        sa.text(
            "UPDATE club_subscriptions SET plan_id = :ent, updated_at = now() "
            "WHERE plan_id = :campus AND razorpay_subscription_id IS NULL"
        ),
        {"ent": enterprise, "campus": campus},
    ).rowcount
    bind.execute(  # migration-check: ok full-table-update -- only snapshots naming Campus (none or a few)
        sa.text(
            "UPDATE club_subscriptions "
            "SET previous_state = jsonb_set(previous_state::jsonb, '{plan_id}', to_jsonb(CAST(:ent AS integer)))::json "
            "WHERE previous_state IS NOT NULL AND previous_state->>'plan_id' = :campus"
        ),
        {"ent": enterprise, "campus": str(campus)},
    )
    bind.execute(  # migration-check: ok full-table-update -- only sign-ups that picked Campus (none or a few)
        sa.text("UPDATE clubs SET requested_plan = 'enterprise' WHERE requested_plan = 'campus'"),
    )
    return moved or 0


def retire_plan_trials(bind) -> None:
    bind.execute(
        sa.text("UPDATE plans SET trial_days = 0, updated_at = now() WHERE key = ANY(:keys) AND trial_days <> 0"),
        {"keys": list(PAID_TIERS)},
    )


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.add_column(
        "club_subscriptions",
        sa.Column("trial_plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "club_subscriptions",
        sa.Column(
            "enterprise_trial_plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.add_column("club_subscriptions", sa.Column("enterprise_trial_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("club_subscriptions", sa.Column("enterprise_trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "club_subscriptions",
        sa.Column("enterprise_trial_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "club_subscriptions", sa.Column("enterprise_trial_reminded_at", sa.DateTime(timezone=True), nullable=True)
    )

    bind = op.get_bind()
    retire_plan_trials(bind)
    fold_campus(bind)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE club_subscriptions SET status = 'cancelled', current_period_end = trial_ends_at, updated_at = now() "
            "WHERE trial_plan_id IS NOT NULL AND razorpay_subscription_id IS NULL AND status = 'trial' "
            "AND trial_ends_at <= now()"
        )
    )
    op.drop_column("club_subscriptions", "enterprise_trial_reminded_at")
    op.drop_column("club_subscriptions", "enterprise_trial_by")
    op.drop_column("club_subscriptions", "enterprise_trial_ends_at")
    op.drop_column("club_subscriptions", "enterprise_trial_started_at")
    op.drop_column("club_subscriptions", "enterprise_trial_plan_id")
    op.drop_column("club_subscriptions", "trial_plan_id")
    bind.execute(sa.text("UPDATE plans SET trial_days = 14, updated_at = now() WHERE key IN ('growth', 'pro')"))
    bind.execute(sa.text("UPDATE plans SET visibility = 'public', updated_at = now() WHERE key = 'campus' AND NOT is_deleted"))
