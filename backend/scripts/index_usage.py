"""How much each index is used, for deciding which to drop. Read-only.

    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/index_usage.py
    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/index_usage.py --all

Prints, from pg_stat_user_indexes, every index with its table, size, how many
times a query has used it (idx_scan) and the rows it handed out, and when
those counters were last reset. By default only the candidates for dropping
(docs/SCALING.md, section d.15): the single-column `ix_*_is_deleted` indexes,
the tracking-link indexes, and any index never scanned. --all prints every
index.

Read idx_scan against how long the counters have been running ("since"): an
index with 0 scans over weeks of production traffic serves no query and
costs a write on every insert and update of its table, and disk. A unique
index or a primary key is never a candidate however rarely it is scanned:
it enforces something. Drop nothing on the strength of a server that was
restarted or had its statistics reset an hour ago.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

QUERY = """
SELECT s.relname AS table_name,
       s.indexrelname AS index_name,
       pg_relation_size(s.indexrelid) AS bytes,
       s.idx_scan,
       s.idx_tup_read,
       s.idx_tup_fetch,
       i.indisunique OR i.indisprimary AS enforces,
       pg_get_indexdef(s.indexrelid) AS definition
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
ORDER BY pg_relation_size(s.indexrelid) DESC
"""

SINCE = "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"


def _url() -> str:
    url = os.environ.get("APP_DATABASE_URL_SYNC")
    if url:
        return url
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import settings

    return settings.DATABASE_URL_SYNC


def candidate(row) -> bool:
    if row.enforces:
        return False
    name = row.index_name
    return name.endswith("_is_deleted") or "tracking_link" in name or row.idx_scan == 0


def size(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--all", action="store_true", help="every index, not only the candidates")
    args = p.parse_args()
    engine = create_engine(_url())
    with engine.connect() as conn:
        since = conn.execute(text(SINCE)).scalar()
        uptime = conn.execute(text("SELECT now() - pg_postmaster_start_time()")).scalar()
        rows = conn.execute(text(QUERY)).all()
    engine.dispose()
    shown = [r for r in rows if args.all or candidate(r)]
    print(f"statistics since {since or 'the server started'} (server up {uptime}); {len(shown)} of {len(rows)} indexes")
    print(f"{'table':34} {'index':52} {'size':>9} {'scans':>12} {'rows read':>14}  enforces")
    for r in shown:
        print(f"{r.table_name:34} {r.index_name:52} {size(r.bytes):>9} {r.idx_scan:>12,} {r.idx_tup_read:>14,}  {'yes' if r.enforces else ''}")


if __name__ == "__main__":
    main()
