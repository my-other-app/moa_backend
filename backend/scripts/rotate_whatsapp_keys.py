"""Seal every club's WhatsApp keys again under the newest WHATSAPP_KEYS_SECRET key.

    $DC exec -T backend python scripts/rotate_whatsapp_keys.py [--dry-run]

A rotation of APP_WHATSAPP_KEYS_SECRET (app/core/keyring.py): put the new
Fernet key first and keep the old one after it (`new,old`), restart, run
this, then drop the old key and restart again. Each club's access token, app
secret and webhook verify token -- and, for a number connected with Meta
(Embedded Signup), its registration PIN (club_whatsapp_accounts' `_enc`
columns) -- is re-encrypted with MultiFernet.rotate: whatever key opened it,
it is sealed under the first. Rows are locked and written a batch at a time.

A value no key opens is left as it is and counted: that club pastes its keys
again (its page says so). --dry-run counts without writing. Prints
{"rows": n, "rotated": n, "unreadable": n}. Safe to run again.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from cryptography.fernet import InvalidToken  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.api.club_whatsapp import crypto  # noqa: E402
from app.api.club_whatsapp.models import ClubWhatsAppAccounts  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402

COLUMNS = ("access_token_enc", "app_secret_enc", "verify_token_enc", "register_pin_enc")
BATCH = 200


async def run(session, *, dry_run: bool = False) -> dict:
    """Every row's sealed values, rotated on `session` (committed a batch at a time)."""
    fernet = crypto._keys.fernet()
    if fernet is None:
        raise SystemExit("APP_WHATSAPP_KEYS_SECRET has no usable key: nothing can be rotated.")
    done = {"rows": 0, "rotated": 0, "unreadable": 0}
    last = 0
    while True:
        rows = (await session.scalars(
            select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.id > last)
            .order_by(ClubWhatsAppAccounts.id).limit(BATCH).with_for_update()
        )).all()
        if not rows:
            break
        for row in rows:
            last = row.id
            done["rows"] += 1
            for column in COLUMNS:
                sealed = getattr(row, column)
                if not sealed:
                    continue
                try:
                    again = fernet.rotate(sealed.encode("ascii")).decode("ascii")
                except (InvalidToken, ValueError):
                    done["unreadable"] += 1
                    continue
                done["rotated"] += 1
                if not dry_run:
                    setattr(row, column, again)
        # Writes the batch (nothing, in a dry run) and lets its rows go.
        await session.commit()
    return done


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count what would be rotated, write nothing")
    args = parser.parse_args()
    async with AsyncSessionLocal() as session:
        done = await run(session, dry_run=args.dry_run)
    print(json.dumps(done))


if __name__ == "__main__":
    asyncio.run(main())
