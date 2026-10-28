"""Platform settings an owner changes from the portal, without SSH.

A fixed allowlist (SPECS). Each key is either one of the server's env
settings (`env=True`: AI_ENABLED, MCP_ENABLED, ...), which the stored value
then stands in for, or a setting that lives only here (the maintenance
banner, whether admins must use two-factor).

How the server reads them: the env-backed ones are laid over `settings`
itself. Every process (each API worker and the job worker) reads the
`platform_settings` rows at start and every REFRESH_SECONDS after
(`refresh_loop`), and sets `settings.<KEY>` to the stored value; a key whose
row is removed goes back to the value the process started with. Every place
that reads `settings.AI_ENABLED` at the time it acts -- and all of these do --
follows within REFRESH_SECONDS, with no restart. The process that saved the
change applies it at once (`apply`).

Secrets never come here: they stay in the server's .env, and the portal is
told only whether each is set and looks right (`secrets_health`).

Nothing is applied in a process that has no rows: the test suite's own
monkeypatches of these settings are never overwritten.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.api.admin_ops.models import PlatformSettings
from app.config import settings

logger = logging.getLogger(__name__)

#: How often each process reads the stored settings again.
REFRESH_SECONDS = 15.0
BANNER_MAX = 300


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str  # "bool" | "text" | "choice"
    label: str
    help: str
    env: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()
    #: Whether a change needs the backend restarted to take effect.
    restart: bool = False
    #: Shown with the setting: what it depends on, or what to watch first.
    note: Optional[str] = None
    group: str = "Platform"


SPECS: dict[str, Spec] = {s.key: s for s in (
    Spec("AI_ENABLED", "bool", "AI assist",
         "Clubs' AI writing help and their own keys (BYOK). Off: every AI route answers that AI is off.",
         group="Features"),
    Spec("MCP_ENABLED", "bool", "AI assistants (MCP)",
         "The clubs' MCP server at /mcp and its sign-in. Off: /mcp and its OAuth endpoints answer 404.",
         group="Features"),
    Spec("MAIL_PROVIDER", "choice", "Mail provider",
         "Who sends the platform's mail. Each send reads it, so a switch applies to the next mail.",
         choices=("ses", "ses_smtp", "resend"),
         note="ses is SES's API (APP_SES_ACCESS_KEY / _SECRET_KEY); ses_smtp is SES's SMTP interface "
              "(APP_SMTP_HOST, _USERNAME, _PASSWORD); resend needs RESEND_API_KEY, and without it mail stays on SES.",
         group="Delivery"),
    Spec("PUSH_TOPICS", "bool", "Push by topic",
         "Club and platform pushes go to FCM topics instead of device by device.",
         note="Watch the share of devices on topics (Push health) before turning this on.",
         group="Delivery"),
    Spec("PUSH_TOPICS_LEGACY", "bool", "Also push device by device",
         "With push by topic on, devices that don't subscribe to topics yet are still sent to one by one.",
         group="Delivery"),
    Spec("LEDGER_READS", "bool", "Finance reads the ledger",
         "The finance and revenue pages read the money ledger's monthly sums instead of adding up the rows.",
         note="Run scripts/ledger_compare.py first: both ways must agree.",
         group="Money"),
    Spec("maintenance_banner", "text", "Maintenance banner",
         "A line shown across the top of the admin portal and the clubs dashboard. Empty: no banner.",
         env=False, default="", group="Platform"),
    Spec("admin_2fa_required", "bool", "Admins must use two-factor",
         "An admin without two-factor must set it up before using the portal. Turning it on gives every "
         "admin without it 7 days.",
         env=False, default=True, group="Security"),
)}

#: Keys only the server writes (not editable from the page).
GRACE_STARTED_AT = "admin_2fa_grace_started_at"
INTERNAL_KEYS = frozenset({GRACE_STARTED_AT})

#: Secrets and keys shown as set or not -- never their values.
SECRET_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Payments", ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "PAYMENT_KEYS_SECRET")),
    ("Mail", ("SES_ACCESS_KEY", "SES_SECRET_KEY", "SES_DEFAULT_SENDER", "SES_WEBHOOK_TOKEN", "SMTP_HOST",
              "SMTP_USERNAME", "SMTP_PASSWORD", "RESEND_API_KEY", "RESEND_WEBHOOK_SECRET", "MAIL_FROM_ADDRESS")),
    ("WhatsApp", ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID",
                  "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_KEYS_SECRET")),
    ("Push", ("FIREBASE_SERVICE_ACCOUNT_PATH", "FIREBASE_SERVICE_ACCOUNT_BASE64")),
    ("AI", ("GEMINI_API_KEY", "AI_FALLBACK_API_KEY", "AI_KEYS_SECRET")),
    ("Storage and backups", ("S3_ACCESS_KEY", "S3_SECRET_KEY", "BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY")),
    ("Monitoring", ("SENTRY_DSN",)),
    ("Infrastructure", ("REDIS_URL", "SEARCH_KEY", "CLOUDFLARE_API_TOKEN")),
)
#: The mail keys, and the MAIL_PROVIDER values that send with them (the
#: page says "in use" or "not in use" beside each one that is set).
_MAIL_KEYS = {
    "SES_ACCESS_KEY": ("ses", "resend"), "SES_SECRET_KEY": ("ses", "resend"),
    "SMTP_HOST": ("ses_smtp",), "SMTP_USERNAME": ("ses_smtp",), "SMTP_PASSWORD": ("ses_smtp",),
    "RESEND_API_KEY": ("resend",),
}
#: Of a pair, either one set is enough (Firebase's path or its base64).
ANY_OF = {"FIREBASE_SERVICE_ACCOUNT_PATH": "FIREBASE_SERVICE_ACCOUNT_BASE64",
          "FIREBASE_SERVICE_ACCOUNT_BASE64": "FIREBASE_SERVICE_ACCOUNT_PATH"}


# ─── What this process holds ─────────────────────────────────────────────


@dataclass
class _State:
    #: Every stored row's value, by key (env-backed and not).
    values: dict[str, Any] = field(default_factory=dict)
    #: What each env setting was before a stored value replaced it.
    originals: dict[str, Any] = field(default_factory=dict)
    loaded_at: float = 0.0


_state = _State()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def coerce(spec: Spec, value: Any) -> Any:
    """The value as the setting takes it; ValueError when it cannot be."""
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
            return False
        raise ValueError("must be true or false")
    if spec.kind == "choice":
        text = str(value or "").strip().lower()
        if text not in spec.choices:
            raise ValueError(f"must be one of {', '.join(spec.choices)}")
        return text
    text = " ".join(str(value or "").split())
    if len(text) > BANNER_MAX:
        raise ValueError(f"must be at most {BANNER_MAX} characters")
    return text


def env_value(key: str) -> Any:
    """What the server's .env (or its default) says for an env-backed key."""
    if key in _state.originals:
        return _state.originals[key]
    return getattr(settings, key, None)


def _apply_values(values: dict[str, Any]) -> None:
    """Lay the stored values over `settings`; put back the ones no longer stored."""
    for key, spec in SPECS.items():
        if not spec.env:
            continue
        if key in values:
            try:
                wanted = coerce(spec, values[key])
            except ValueError:
                logger.warning("platform setting %s holds a value it cannot take; ignored", key)
                continue
            if key not in _state.originals:
                _state.originals[key] = getattr(settings, key, None)
            if getattr(settings, key, None) != wanted:
                setattr(settings, key, wanted)
                logger.info("platform setting %s is now %r (from the portal)", key, wanted)
        elif key in _state.originals:
            setattr(settings, key, _state.originals.pop(key))
            logger.info("platform setting %s is back to the server's own value", key)
    _state.values = dict(values)
    _state.loaded_at = time.monotonic()


async def load(session) -> dict[str, Any]:
    """Read every stored setting and apply it to this process."""
    rows = (await session.execute(select(PlatformSettings.key, PlatformSettings.value))).all()
    values = {k: v for k, v in rows}
    _apply_values(values)
    return values


def stored(key: str, default: Any = None) -> Any:
    """The value this process last read for a key, else `default`."""
    return _state.values.get(key, default)


async def read(session, key: str, default: Any = None) -> Any:
    """One stored value, read now (for decisions that must not wait for a refresh)."""
    row = await session.scalar(select(PlatformSettings.value).where(PlatformSettings.key == key))
    return default if row is None else row


async def write(session, key: str, value: Any, *, actor_id: Optional[int]) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    await session.execute(
        pg_insert(PlatformSettings.__table__)
        .values(key=key, value=value, updated_by=actor_id, updated_at=now)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_by": actor_id, "updated_at": now})
    )


async def remove(session, key: str) -> None:
    from sqlalchemy import delete

    await session.execute(delete(PlatformSettings).where(PlatformSettings.key == key))


def apply_now(values: dict[str, Any]) -> None:
    """After a commit: this process takes the new values without waiting."""
    _apply_values(values)


async def refresh_loop() -> None:
    """Every process: the stored settings read again every REFRESH_SECONDS."""
    from app.db.core import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as s:
                await load(s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next pass tries again; the env values still stand
            logger.warning("reading the platform settings failed", exc_info=True)
        await asyncio.sleep(REFRESH_SECONDS)


def reset_for_tests() -> None:
    """Put every env setting back as it was and forget what was read."""
    for key, value in list(_state.originals.items()):
        setattr(settings, key, value)
    _state.originals.clear()
    _state.values.clear()
    _state.loaded_at = 0.0


# ─── What the page shows ─────────────────────────────────────────────────


def _is_set(name: str) -> bool:
    value = getattr(settings, name, None)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value is not False


def secrets_health() -> list[dict]:
    """Each secret: whether it is set, and whether the start-up check has
    anything to say about it -- never its value."""
    from app.core import config_checks

    problems: dict[str, str] = {}
    try:
        for p in config_checks.check(settings):
            problems.setdefault(p.setting, p.reason)
    except Exception:  # noqa: BLE001 - the page still shows what is set
        logger.warning("the settings check failed", exc_info=True)
    out = []
    for group, names in SECRET_GROUPS:
        for name in names:
            if not hasattr(settings, name):
                continue
            is_set = _is_set(name)
            alt = ANY_OF.get(name)
            covered = is_set or (alt is not None and _is_set(alt))
            problem = problems.get(name)
            out.append({
                "name": f"APP_{name}",
                "group": group,
                "set": is_set,
                "healthy": bool(covered and problem is None),
                "problem": problem,
                "mode": _mode(name) if is_set else None,
            })
    return out


def _mode(name: str) -> Optional[str]:
    """A word about a set key that is not its value: Razorpay's live or test,
    and whether the mail keys are the ones MAIL_PROVIDER sends with."""
    if name in _MAIL_KEYS:
        provider = (getattr(settings, "MAIL_PROVIDER", "") or "ses").lower()
        return "in use" if provider in _MAIL_KEYS[name] else "not in use"
    if name == "RAZORPAY_KEY_ID":
        value = str(getattr(settings, name, "") or "")
        if value.startswith("rzp_live_"):
            return "live"
        if value.startswith("rzp_test_"):
            return "test"
    return None
