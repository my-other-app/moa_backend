"""What is kept under docs/, by kind and age, and what the sweep would delete.

    docker compose exec -T backend python scripts/doc_cache_report.py              # at APP_DOC_CACHE_DAYS, else 90
    docker compose exec -T backend python scripts/doc_cache_report.py --days 60

Read-only: it lists `{APP_S3_BASE_PATH}/docs/` with ListObjectsV2 and nothing
else -- no read of any object, no write, no delete. Prints one JSON report:

- per kind (ticket, certificate, and "not a kept document" for anything else
  under docs/), the count and bytes last written under 30 days ago, 30 to 90,
  and over 90;
- what the sweep (app/api/events/doc_cache_sweep.py) would delete at
  --days: count and bytes, and how many daily runs that takes at
  APP_DOC_CACHE_MAX_DELETE_PER_RUN;
- the share of docs/ that is not kept documents, against
  APP_DOC_CACHE_SKIP_ALERT_PERCENT (above it, the sweep raises an alert).

Run it before setting APP_DOC_CACHE_DAYS (docs/OPERATIONS.md, section 13).
A large docs/ takes a LIST call per 1,000 objects.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, ".")

from app.api.events import doc_cache_sweep as sweep  # noqa: E402
from app.api.events.doc_cache import KINDS  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.storage import objects  # noqa: E402

OTHER = "not a kept document"
AGES = ((30, "under 30 days"), (90, "30-90 days"), (None, "over 90 days"))


def _age_bucket(age: timedelta) -> str:
    for days, name in AGES:
        if days is None or age < timedelta(days=days):
            return name
    return AGES[-1][1]


def report(client=None, *, days: int, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    base = sweep.root()
    client = client or objects.client()
    by_kind = {kind: {name: {"count": 0, "bytes": 0} for _, name in AGES} for kind in (*KINDS, OTHER)}
    would = {"count": 0, "bytes": 0}
    total = {"count": 0, "bytes": 0}
    for page in sweep.pages(client, base):
        for obj in page:
            key, size = obj.get("Key") or "", int(obj.get("Size") or 0)
            modified = obj.get("LastModified")
            modified = modified if modified is None or modified.tzinfo else modified.replace(tzinfo=timezone.utc)
            kind = sweep.kind_of(key, base) or OTHER
            cell = by_kind[kind][_age_bucket(now - modified if modified else timedelta(0))]
            cell["count"] += 1
            cell["bytes"] += size
            total["count"] += 1
            total["bytes"] += size
            if kind != OTHER and modified is not None and modified < cutoff:
                would["count"] += 1
                would["bytes"] += size
    cap = max(1, int(settings.DOC_CACHE_MAX_DELETE_PER_RUN))
    unknown = sum(c["count"] for c in by_kind[OTHER].values())
    return {
        "prefix": base,
        "at": now.isoformat(),
        "objects": total,
        "by_kind": by_kind,
        "would_delete": {
            "days": days, "cutoff": cutoff.isoformat(), **would,
            "runs_needed": math.ceil(would["count"] / cap), "max_per_run": cap,
        },
        "not_kept_documents": {
            "count": unknown,
            "percent": round(100.0 * unknown / total["count"], 2) if total["count"] else 0.0,
            "alert_above_percent": float(settings.DOC_CACHE_SKIP_ALERT_PERCENT),
        },
        "sweep_on": int(settings.DOC_CACHE_DAYS or 0) > 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=int(settings.DOC_CACHE_DAYS or 0) or 90,
                    help="what the sweep would delete at this many days (default APP_DOC_CACHE_DAYS, else 90)")
    args = ap.parse_args()
    print(json.dumps(report(days=max(1, args.days)), indent=2))


if __name__ == "__main__":
    main()
