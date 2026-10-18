"""Every Postgres advisory-lock key the code takes, each in one place, each once.

An advisory lock is a number. Two pieces of code that pick the same number
take the same lock: the store sweep and the club-sites sweep both took 773121
until 2026-10-19, so whichever held it made the other skip its turn. Nothing
said so, since a sweep that does not get its lock is meant to stay quiet.

Postgres keeps two forms apart: one bigint (pg_locks.objsubid 1), and a pair
of ints (objsubid 2), whose first int names a kind of lock and the second the
row it is for. A number in one form never meets the same number in the other,
but each value here is unique across both anyway, so a lock that moves from
one form to the other cannot land on another's.

The rules (tests/test_lock_keys.py holds the code to them):

- A key is declared here and nowhere else. The module that takes it keeps its
  own name for it (`SWEEP_LOCK = lock_keys.STORE_SWEEP`), so call sites and
  tests read as before; a lock written as a literal anywhere else fails.
- One owner per key: no two modules point at the same one.
- A key keeps its value. A deploy overlaps old and new processes, and a sweep
  held under its old key in an old process would run beside the new one's
  under the new key. Change one only to end a collision, and say so here.

Not here: locks keyed by a hash of a string (`hashtext('...')` in
payments/service.py and subscriptions/gates.py, `hashtextextended` in
events/registration/service.py). Their keys are made at run time in the
one-bigint form, so one could in principle equal a key below (one value in
four billion for hashtext). All of them are transaction locks held for
milliseconds; the worst a collision does is make one wait or skip one turn.
"""
from __future__ import annotations

# ─── Session locks, one bigint: one process runs the loop (app.db.locks) ─────

#: The timed-email sweep (app/api/emails/service.py).
EMAIL_SWEEP = 773_101
#: The close-out sweep (app/api/closeout/service.py).
CLOSEOUT_SWEEP = 773_102
#: The discover counters' check (app/api/discover/counters.py).
DISCOVER_COUNTERS = 773_104
#: The billing sweep (app/api/subscriptions/sweep.py).
SUBSCRIPTIONS_SWEEP = 773_110
#: Clubs' own domains (app/api/club_domains/sweep.py).
CLUB_DOMAINS_SWEEP = 773_120
#: The store's held stock (app/api/store/sweep.py).
STORE_SWEEP = 773_121
#: Club sites kept current (app/api/sites/sweep.py). 773_121 until
#: 2026-10-19, the store sweep's key; the store sweep was there first.
SITES_SWEEP = 773_123
#: Sign-in records and dead sessions (app/api/auth/retention.py).
AUTH_RETENTION = 773_130
#: The search outbox's drainer (app/api/search/sync.py, scripts/search_reindex.py).
SEARCH_SYNC = 773_140
#: The job queue's maintenance pass (app/core/jobs.py).
JOBS_MAINTENANCE = 774_501
#: The live room's sweep (app/api/live/service.py).
LIVE_SWEEP = 7_301_010
#: Discover's popularity scores (app/api/discover/popularity.py).
DISCOVER_POPULARITY = 7_731_402

# ─── Session lock, two ints: (this, an event id) ─────────────────────────────

#: One retention run per event (app/api/closeout/service.py).
CLOSEOUT_RETENTION = 773_103

# ─── Transaction locks, one bigint: whose turn a scheduled run is ────────────

#: The payments sweep's claim (app/api/payments/sweep.py).
PAYMENTS_SWEEP = 773_122
#: The ledger's daily run (app/api/ledger/jobs.py).
LEDGER_DAILY = 773_131
#: Discover's stats refresh (app/api/discover/stats.py).
DISCOVER_STATS = 7_420_401

# ─── Transaction locks, two ints: (this, the row it is for) ──────────────────

#: A template's manual send, so two clicks queue one message
#: (app/api/emails/service.py). 773_102 until 2026-10-19: the same number as
#: CLOSEOUT_SWEEP, in the other form, so never the same lock; moved so no
#: two keys share a number.
EMAIL_SEND = 773_105
#: Two guest merges into one account (app/api/users/service.py).
USER_MERGE = 7_316_203
#: Two verification codes for one account (app/api/auth/verification.py).
VERIFICATION_SEND = 7_316_204
#: Two reset requests for one address (app/api/auth/password_reset.py).
PASSWORD_RESET = 7_316_205
#: Two admin alerts of one kind about one thing (app/api/admin_alerts/service.py).
ADMIN_ALERT_DEDUPE = 7_420_311
#: A club connecting its payment gateway (app/api/payment_gateways/service.py).
GATEWAY_CONNECT = 7_420_377
#: A club's plan checkout (app/api/subscriptions/router.py).
PLAN_CHECKOUT = 7_420_388
#: A registration's or a subscription's ledger lines (app/api/ledger/service.py, backfill.py).
LEDGER = 7_420_455


def all_keys() -> dict[str, int]:
    """Every key declared above, by name."""
    return {name: value for name, value in globals().items() if name.isupper() and isinstance(value, int)}
