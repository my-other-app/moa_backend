"""Design artwork already in the bucket, made ready for print as a new upload is.

    docker compose exec -T backend python scripts/optimize_doc_art.py              # dry run: reads, writes nothing
    docker compose exec -T backend python scripts/optimize_doc_art.py --apply      # writes the new files, repoints

Uploads are now stored ready for print (app/core/storage/doc_art.py): upright,
at most APP_DOC_ART_MAX_PX on the longest side, a JPEG or (with transparency)
a PNG, no metadata. What was uploaded before is as it was sent: a 10 MB PNG
export is drawn into every ticket made from its design, at seconds a render
and megabytes a PDF (scripts/bench/doc_art.py).

This finds every picture a design names -- the ticket and certificate
templates' image columns, the same images named in their configs (and in
legacy `{field}_url` links), and the layered designs' template and pictures
(`layout.canvas.templateKey`, `layout.layers[].imageKey`) in ticket,
certificate, ID card and agenda designs, deleted ones included -- and, for
each object of at least --min-kb:

- reads it and normalises it as an upload would be;
- if that is smaller, writes it under a NEW key in the same folder, and
  points every design that named the old key at the new one (columns and
  configs; a design's version is left alone: it prints the same);
- never deletes or overwrites the old object. The report lists the old keys
  (`old_keys`), to delete by hand once nothing is rolled back.

A document already rendered from a design draws again with the new artwork on
its own: the rendered-document cache's key hashes the design itself, its image
keys included (app/api/events/doc_cache.py `fingerprint`), so a design that
names the new key names new cache keys. (The template file's hash in that
fingerprint is the HTML template's file, which this does not touch.) The
documents cached under the old keys are no longer asked for, and the doc-cache
sweep (APP_DOC_CACHE_DAYS) or a lifecycle rule removes them in time.

Prints one JSON report: counts, bytes before and after, every change and
failure. Safe to run again: a repointed design names a small file, which is
under the threshold next time. Read-only without --apply.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.api.agenda.models import AgendaTemplates  # noqa: E402
from app.api.certificate_templates.models import CertificateTemplates  # noqa: E402
from app.api.idcards.models import BadgeTemplates  # noqa: E402
from app.api.templates_images import CERTIFICATE_FIELDS, TICKET_FIELDS  # noqa: E402
from app.api.ticket_templates.models import TicketTemplates  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.storage import doc_art, objects  # noqa: E402
from app.core.storage.bucket import key_from_url  # noqa: E402
from app.core.storage.fields import stored_key  # noqa: E402
from app.response import CustomHTTPException  # noqa: E402

#: Every design table, with the image columns it has.
TABLES = (
    (TicketTemplates, TICKET_FIELDS),
    (CertificateTemplates, CERTIFICATE_FIELDS),
    (BadgeTemplates, ()),
    (AgendaTemplates, ()),
)
DEFAULT_MIN_KB = 300
_TEMPLATE_KEYS = ("templateKey", "template_key")
_IMAGE_KEYS = ("imageKey", "image_key")
#: The unique part at the end of a stored name: a uuid4's hex, with or without its dashes.
_UNIQUE = re.compile(r"^(?P<lead>.*?)(?P<id>[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


# ─── What a design names ────────────────────────────────────────────────────


def _layout(config: dict) -> dict:
    layout = (config or {}).get("layout")
    return layout if isinstance(layout, dict) else {}


def keys_in(row, fields) -> set[str]:
    """Every object key this design names, wherever it names it."""
    config = row.config if isinstance(row.config, dict) else {}
    found: set[str] = set()
    for f in fields:
        found.add(stored_key(getattr(row, f, None)))
        if f"{f}_key" in config:
            found.add(config.get(f"{f}_key"))
        elif f"{f}_url" in config:
            found.add(key_from_url(config.get(f"{f}_url")))
    layout = _layout(config)
    canvas = layout.get("canvas") if isinstance(layout.get("canvas"), dict) else {}
    found.update(canvas.get(k) for k in _TEMPLATE_KEYS)
    for layer in layout.get("layers") or []:
        if isinstance(layer, dict):
            found.update(layer.get(k) for k in _IMAGE_KEYS)
    return {k for k in found if isinstance(k, str) and k}


def repoint(row, fields, moved: dict[str, str]) -> bool:
    """Point this design at the new keys in `moved` (old -> new). True when it changed."""
    changed = False
    for f in fields:
        old = stored_key(getattr(row, f, None))
        if old in moved:
            setattr(row, f, moved[old])
            changed = True
    if not isinstance(row.config, dict):
        return changed
    config = copy.deepcopy(row.config)
    touched = False
    for f in fields:
        if f"{f}_key" in config:
            if config.get(f"{f}_key") in moved:
                config[f"{f}_key"] = moved[config[f"{f}_key"]]
                touched = True
        elif key_from_url(config.get(f"{f}_url")) in moved:
            # A legacy link: stored as a key now, as the next save would.
            config[f"{f}_key"] = moved[key_from_url(config[f"{f}_url"])]
            config.pop(f"{f}_url", None)
            touched = True
    layout = _layout(config)
    canvas = layout.get("canvas") if isinstance(layout.get("canvas"), dict) else {}
    for k in _TEMPLATE_KEYS:
        if canvas.get(k) in moved:
            canvas[k] = moved[canvas[k]]
            touched = True
    for layer in layout.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        for k in _IMAGE_KEYS:
            if layer.get(k) in moved:
                layer[k] = moved[layer[k]]
                touched = True
    if touched:
        row.config = config
    return changed or touched


def new_key(old: str, art: doc_art.PrintArt) -> str:
    """A fresh key beside `old`: its folder, its kind of name, the new file's extension."""
    folder, _, name = old.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    m = _UNIQUE.match(stem)
    lead = (m.group("lead") if m else f"{stem}-") or "v-"
    ext = art.ext(jpeg="jpeg" if lead == "v-" else "jpg")
    fresh = f"{lead}{uuid.uuid4().hex}.{ext}"
    return f"{folder}/{fresh}" if folder else fresh


# ─── The bucket ─────────────────────────────────────────────────────────────


def _size(key: str) -> Optional[int]:
    """The object's size in bytes; None when it is not there."""
    try:
        return int(objects.client().head_object(Bucket=settings.S3_BUCKET, Key=key)["ContentLength"])
    except Exception as exc:  # noqa: BLE001
        if objects._not_found(exc):
            return None
        raise


@dataclass
class Report:
    mode: str
    min_bytes: int
    rows: dict = field(default_factory=dict)
    keys: int = 0
    over_threshold: int = 0
    optimised: int = 0
    skipped_small: int = 0
    skipped_no_gain: int = 0
    missing: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0
    rows_repointed: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)
    old_keys: list = field(default_factory=list)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["saved_bytes"] = self.bytes_before - self.bytes_after
        return out


async def run(session, *, apply: bool = False, min_kb: int = DEFAULT_MIN_KB, longest: Optional[int] = None) -> dict:
    report = Report(mode="apply" if apply else "dry-run", min_bytes=max(0, min_kb) * 1024)
    designs: list = []
    for model, fields in TABLES:
        rows = list((await session.scalars(select(model).order_by(model.id).execution_options(include_deleted=True))).all())
        report.rows[model.__tablename__] = len(rows)
        designs.extend((model, fields, row, keys_in(row, fields)) for row in rows)
    keys = sorted({k for *_, found in designs for k in found})
    report.keys = len(keys)

    moved: dict[str, str] = {}
    for key in keys:
        try:
            size = await asyncio.to_thread(_size, key)
        except Exception as exc:  # noqa: BLE001
            report.failed.append({"key": key, "error": f"size: {type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if size is None:
            report.missing.append(key)
            continue
        if size < report.min_bytes:
            report.skipped_small += 1
            continue
        report.over_threshold += 1
        raw = await asyncio.to_thread(objects.fetch, key)
        if raw is None:
            report.failed.append({"key": key, "error": "could not be read"})
            continue
        try:
            art = await asyncio.to_thread(doc_art.normalise, raw, max_bytes=0, longest=longest)
        except CustomHTTPException as exc:
            report.failed.append({"key": key, "error": exc.message})
            continue
        except Exception as exc:  # noqa: BLE001
            report.failed.append({"key": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if len(art.data) >= len(raw):
            report.skipped_no_gain += 1
            continue
        target = new_key(key, art)
        change = {"old": key, "new": target if apply else None, "before": len(raw), "after": len(art.data),
                  "format": art.format, "size": f"{art.width}x{art.height}"}
        if apply:
            try:
                await asyncio.to_thread(objects.put_bytes, target, art.data, art.content_type)
            except Exception as exc:  # noqa: BLE001
                report.failed.append({"key": key, "error": f"write {target}: {type(exc).__name__}: {str(exc)[:200]}"})
                continue
            moved[key] = target
        report.optimised += 1
        report.bytes_before += len(raw)
        report.bytes_after += len(art.data)
        report.changes.append(change)

    if apply and moved:
        for model, fields, row, found in designs:
            if not found & moved.keys():
                continue
            try:
                if repoint(row, fields, moved):
                    await session.commit()
                    report.rows_repointed[model.__tablename__] = report.rows_repointed.get(model.__tablename__, 0) + 1
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                report.failed.append({"row": f"{model.__tablename__}:{row.id}", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        report.old_keys = sorted(moved)
    return report.as_dict()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read and report only (the default)")
    mode.add_argument("--apply", action="store_true", help="write the new files and repoint the designs")
    ap.add_argument("--min-kb", type=int, default=DEFAULT_MIN_KB, help=f"only objects at least this large (default {DEFAULT_MIN_KB})")
    args = ap.parse_args()

    from app.db.core import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        report = await run(session, apply=args.apply, min_kb=args.min_kb)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
