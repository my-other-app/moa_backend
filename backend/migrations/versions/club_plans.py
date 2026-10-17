"""Club plans for the pricing page and self-serve sign-up.

- plans.featured: the plan the pricing page highlights ("Most popular").
- plans.contact_sales: priced on request; the page says "Talk to us" and a
  club cannot start it at checkout.
- clubs.requested_plan / clubs.requested_cycle: what an applicant picked on
  the sign-up form, for the admin and for the dashboard after approval.
- Six plans seeded, public: free, starter, growth, pro, campus, enterprise.
  DRAFT prices, for the owner to adjust in Admin -> Subscriptions.

The seed only ever inserts. A plan whose key exists already (live, hidden,
legacy or soft-deleted) is left exactly as it is, limits and features
included, so an admin's edits survive this running again. Its limits and
features rows are written only for a plan this migration created.

Seeding `free` changes what `resolve_entitlements` answers for a club with no
subscription: the Free plan instead of "unconfigured, everything allowed".
Nothing enforces a limit or a flag today, so no club can do less after this;
anything that starts enforcing will cap those clubs at Free.

Downgrade removes a seeded key's plan only when no subscription row (live or
cancelled) points at it, then drops the four columns.

Revision ID: club_plans
Revises: discovery_polish
"""
import sqlalchemy as sa
from alembic import op

revision = "club_plans"
down_revision = "discovery_polish"
branch_labels = None
depends_on = None

#: (key, name, tagline, monthly paise, yearly paise, trial days, featured,
#: contact sales). Yearly is ten months of monthly: two months free.
PLANS = (
    ("free", "Free", "For a club just starting out.", 0, 0, 0, False, False),
    ("starter", "Starter", "For a club running regular events.", 29_900, 299_000, 0, False, False),
    ("growth", "Growth", "For an active club with a following.", 69_900, 699_000, 14, True, False),
    ("pro", "Pro", "For big clubs and paid events at scale.", 149_900, 1_499_000, 14, False, False),
    ("campus", "Campus", "For a college body running many clubs and a fest.", 399_900, 3_999_000, 0, False, False),
    ("enterprise", "Enterprise", "For large fests and institutions.", 0, 0, 0, False, True),
)

LIMIT_COLUMNS = (
    "events_per_month",
    "registrations_per_event",
    "registrations_per_month",
    "ticket_types_per_event",
    "coupons_per_event",
    "merch_items_per_event",
    "committee_seats",
    "storage_gb",
    "announcements_per_month",
    "website_templates",
)

#: In LIMIT_COLUMNS order. None is unlimited.
LIMITS = {
    "free": (2, 100, 300, 1, 0, 0, 2, 1, 4, 1),
    "starter": (5, 300, 1500, 3, 2, 5, 5, 5, 15, 3),
    "growth": (15, 1000, 8000, 5, 10, 20, 10, 20, 60, None),
    "pro": (None, 5000, 30000, 10, None, 100, 25, 100, None, None),
    "campus": (None, 20000, 150000, None, None, None, 60, 500, None, None),
    "enterprise": (None,) * 10,
}

# Each tier adds to the one before. Only flags with a feature built behind
# them; the rest (waitlist, coupons, sessions, domain, nobadge, tfa, payouts,
# sso, api) stay off everywhere until they exist.
_FREE = ("checkin", "certificates", "regform", "reviews", "threads")
_STARTER = _FREE + ("tickets", "website")
_GROWTH = _STARTER + ("quiz", "polls", "qa", "networking", "links", "report")
_PRO = _GROWTH + ("merch", "swag", "badges", "roles", "teams", "judging", "rounds")
_CAMPUS = _PRO + ("finance",)

FEATURES = {
    "free": _FREE,
    "starter": _STARTER,
    "growth": _GROWTH,
    "pro": _PRO,
    "campus": _CAMPUS,
    "enterprise": _CAMPUS,
}


def seed_plans() -> list[str]:
    """Insert each plan whose key is free, with its limits and features.
    Returns the keys it inserted. Safe to run any number of times."""
    bind = op.get_bind()
    inserted = []
    for sort_order, (key, name, tagline, monthly, yearly, trial, featured, contact) in enumerate(PLANS):
        plan_id = bind.execute(
            sa.text(
                "INSERT INTO plans (key, name, tagline, price_monthly_paise, price_yearly_paise, trial_days, "
                "visibility, sort_order, featured, contact_sales, created_at, updated_at, is_deleted) "
                "VALUES (:key, :name, :tagline, :monthly, :yearly, :trial, 'public', :sort_order, :featured, "
                ":contact, now(), now(), false) "
                "ON CONFLICT (key) DO NOTHING RETURNING id"
            ),
            dict(key=key, name=name, tagline=tagline, monthly=monthly, yearly=yearly, trial=trial,
                 sort_order=sort_order, featured=featured, contact=contact),
        ).scalar()
        if plan_id is None:
            continue
        inserted.append(key)

        bind.execute(
            sa.text(
                f"INSERT INTO plan_limits (plan_id, {', '.join(LIMIT_COLUMNS)}, created_at, updated_at) "
                f"VALUES (:plan_id, {', '.join(':' + c for c in LIMIT_COLUMNS)}, now(), now())"
            ),
            dict(plan_id=plan_id, **dict(zip(LIMIT_COLUMNS, LIMITS[key]))),
        )
        flags = FEATURES[key]
        bind.execute(
            sa.text(
                f"INSERT INTO plan_features (plan_id, {', '.join(flags)}, created_at, updated_at) "
                f"VALUES (:plan_id, {', '.join('true' for _ in flags)}, now(), now())"
            ),
            dict(plan_id=plan_id),
        )
    return inserted


def upgrade() -> None:
    op.add_column("plans", sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("plans", sa.Column("contact_sales", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("clubs", sa.Column("requested_plan", sa.String(40), nullable=True))
    op.add_column("clubs", sa.Column("requested_cycle", sa.String(10), nullable=True))
    seed_plans()


def downgrade() -> None:
    # plan_limits and plan_features go with their plan (ON DELETE CASCADE).
    op.get_bind().execute(
        sa.text(
            "DELETE FROM plans WHERE key = ANY(:keys) "
            "AND NOT EXISTS (SELECT 1 FROM club_subscriptions s WHERE s.plan_id = plans.id)"
        ),
        dict(keys=[plan[0] for plan in PLANS]),
    )
    op.drop_column("clubs", "requested_cycle")
    op.drop_column("clubs", "requested_plan")
    op.drop_column("plans", "contact_sales")
    op.drop_column("plans", "featured")
