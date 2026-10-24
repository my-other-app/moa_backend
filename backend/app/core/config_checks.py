"""Refuse to start on a missing, placeholder or half-set secret.

In production the API (main.py, and doth's lifespan) and the job worker
(app/worker.py) call `enforce()` before serving anything. It reads the
settings once and stops the process, naming every setting that is wrong --
never its value -- when:

- a secret the platform cannot run without is empty (REQUIRED, and the mail
  provider's own keys);
- any setting holds an obvious placeholder: PASTE_KEY_HERE, changeme,
  replace-me, xxx, your-..., <...>, ${...}, example.com, "password"...;
- APP_SECRET_KEY is weak: under 32 characters, few distinct characters, or
  a value known from a sample or a test;
- an optional integration is half set (PAIRS: a key id without its secret,
  WhatsApp's token without its phone number id...), names a mode that does
  not exist, or holds a key in the wrong shape (a Fernet key, Sentry's DSN,
  Firebase's service account);
- APP_ENV says production while APP_DEBUG is on.

Softer findings -- off-site backups or error tracking not set up, a test
Razorpay key, Firebase's file missing -- are warnings: logged, never fatal.

"Production" is any process whose APP_ENV is not development, dev, local,
test or ci; with APP_ENV unset, any process with APP_DEBUG off. The test
suite sets APP_ENV=test (tests/conftest.py); a laptop sets APP_DEBUG=true or
APP_ENV=development.

APP_CONFIG_CHECKS=warn is the emergency override: every finding is logged
at ERROR and the process starts anyway. Fix the setting, then remove it.

Run by hand -- in a container that is already running, before a deploy
restarts it -- to see what a restart would refuse:

    $DC exec -T backend python -m app.core.config_checks
    $DC exec -T backend python -m app.core.config_checks --env-file - < .env

The second reads the .env the next container will be created with from
stdin (docs/OPERATIONS.md, section 19). Exit status 1 when anything is fatal.

Only `app.config` is imported, so this file also runs in a container whose
image predates it (`docker cp` it in and run it with python).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger("app.config_checks")

#: APP_ENV values that are not production: nothing here is enforced.
NOT_PRODUCTION = frozenset({"development", "dev", "local", "test", "testing", "ci"})

#: Empty, and the platform cannot run: sign-in, storage, payments.
REQUIRED = (
    "SECRET_KEY", "DATABASE_URL", "DATABASE_URL_SYNC",
    "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BASE_PATH",
    "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
)

#: Each mail provider's own settings (MAIL_PROVIDER picks one).
MAIL_REQUIRED = {
    "ses": ("SES_ACCESS_KEY", "SES_SECRET_KEY", "SES_DEFAULT_SENDER"),
    "resend": ("RESEND_API_KEY", "MAIL_FROM_ADDRESS"),
}

#: Set together or not at all: one without the other is an integration that
#: looks on and fails on its first call.
PAIRS = (
    ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"),
    ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"),
    ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"),
    ("BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY"),
    ("AI_FALLBACK_PROVIDER", "AI_FALLBACK_API_KEY"),
)

#: (setting, needs): the first set without the second.
NEEDS = (
    ("SEARCH_URL", "SEARCH_KEY"),
)

#: Fernet keys, a comma-separated list each.
FERNET_KEYS = ("PAYMENT_KEYS_SECRET", "AI_KEYS_SECRET")

MIN_SECRET_KEY = 32
MIN_DISTINCT = 10

#: Settings whose whole value is checked against _PLACEHOLDER_VALUES too:
#: credentials, ids and addresses (a mode like AI_GEMINI_THINKING=none is not).
_SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|DSN|WEBHOOK|DATABASE_URL|_ID$|BUCKET|SENDER|ADDRESS|BASE_PATH")
#: Whole values that are never a real secret or address.
_PLACEHOLDER_VALUES = frozenset({
    "changeme", "change-me", "change_me", "password", "passw0rd", "secret", "secretkey", "secret-key",
    "secret_key", "placeholder", "todo", "tbd", "fixme", "none", "null", "nil", "undefined", "dummy",
    "example", "sample", "default", "test", "testing", "xxx", "key", "token", "admin", "root", "postgres",
    "123456", "12345678", "...", "-", "?", "your-key", "yourkey",
})
#: Placeholders anywhere in a value (lower-cased).
_PLACEHOLDER_PATTERNS = (
    re.compile(r"paste.{0,30}here"),
    re.compile(r"(?<![a-z])(?:change|replace)[\W_]?me(?![a-z])"),
    re.compile(r"(?<![a-z0-9])your[-_][a-z]"),
    re.compile(r"^your\s"),
    re.compile(r"^<.*>$|<[a-z][a-z _-]{0,40}>"),
    re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}"),
    re.compile(r"(?<![a-z0-9])x{3,}(?![a-z0-9])|x{6,}"),
    re.compile(r"(?<![a-z0-9-])example\.(?:com|org|net)\b"),
)
#: SECRET_KEY values seen in samples and test suites.
_KNOWN_SECRET_KEYS = frozenset({
    "test-secret-key-not-for-production", "ci-test-secret-key", "your-secret-key", "supersecret",
    "super-secret", "secret", "changeme", "dev", "development", "django-insecure",
})


@dataclass(frozen=True)
class Problem:
    """One finding: the setting's name and what is wrong with it -- never its value."""

    setting: str
    reason: str
    fatal: bool = True

    def __str__(self) -> str:
        return f"APP_{self.setting}: {self.reason}"


class ConfigError(RuntimeError):
    """Production settings this process refuses to start with."""

    def __init__(self, problems: list[Problem]):
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"Refusing to start: {len(problems)} setting(s) are missing, placeholders or half set "
            f"(values not shown):\n{lines}\n"
            "Fix them in the server's .env (docs/OPERATIONS.md, section 19), or for an emergency "
            "start set APP_CONFIG_CHECKS=warn."
        )


# ─── Reading the settings ────────────────────────────────────────────────


def _get(settings, name: str) -> Any:
    return getattr(settings, name, None)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def is_production(settings) -> bool:
    env = _text(_get(settings, "ENV")).lower()
    if env:
        return env not in NOT_PRODUCTION
    return not bool(_get(settings, "DEBUG"))


def is_placeholder(value: str, *, secret: bool = True) -> bool:
    """Whether `value` is a stand-in for a real one. `secret`: also refuse
    the whole values no credential or address is (password, none, test...)."""
    low = (value or "").strip().lower()
    if not low:
        return False
    if secret and low in _PLACEHOLDER_VALUES:
        return True
    return any(p.search(low) for p in _PLACEHOLDER_PATTERNS)


def _string_settings(settings) -> Iterable[tuple[str, str]]:
    """(name, value) for every text setting, a list's items one by one, and
    the password inside each database URL."""
    fields = getattr(type(settings), "model_fields", None) or {}
    for name in sorted(fields):
        value = _get(settings, name)
        if isinstance(value, str):
            yield name, value
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str):
                    yield name, item
    for name in ("DATABASE_URL", "DATABASE_URL_SYNC"):
        try:
            password = urlsplit(_text(_get(settings, name))).password
        except ValueError:
            password = None
        if password:
            yield name, unquote(password)


# ─── The checks ──────────────────────────────────────────────────────────


def _required(settings) -> list[Problem]:
    out = [Problem(name, "is empty; the platform cannot run without it") for name in REQUIRED if not _text(_get(settings, name))]
    provider = _text(_get(settings, "MAIL_PROVIDER")).lower() or "ses"
    if provider not in MAIL_REQUIRED:
        out.append(Problem("MAIL_PROVIDER", f"must be one of {', '.join(sorted(MAIL_REQUIRED))}"))
    else:
        out += [
            Problem(name, f"is empty, and MAIL_PROVIDER is {provider}: no mail can be sent")
            for name in MAIL_REQUIRED[provider] if not _text(_get(settings, name))
        ]
        if provider == "resend":
            missing = [name for name in MAIL_REQUIRED["ses"][:2] if not _text(_get(settings, name))]
            if missing:
                out.append(Problem(missing[0], "is empty: a mail Resend refuses has no SES to fall back to", fatal=False))
    return out


def _placeholders(settings) -> list[Problem]:
    seen: set[str] = set()
    out = []
    for name, value in _string_settings(settings):
        if name not in seen and is_placeholder(value, secret=bool(_SECRET_LIKE.search(name))):
            seen.add(name)
            out.append(Problem(name, "holds a placeholder, not a real value"))
    return out


def _secret_key(settings) -> list[Problem]:
    key = _text(_get(settings, "SECRET_KEY"))
    if not key:
        return []  # _required says so
    if key.lower() in _KNOWN_SECRET_KEYS or key.lower().startswith(("django-insecure", "test-", "ci-")):
        return [Problem("SECRET_KEY", "is a value from a sample or a test suite; generate one: openssl rand -hex 32")]
    if len(key) < MIN_SECRET_KEY:
        return [Problem("SECRET_KEY", f"is shorter than {MIN_SECRET_KEY} characters; generate one: openssl rand -hex 32")]
    if len(set(key)) < MIN_DISTINCT:
        return [Problem("SECRET_KEY", f"has fewer than {MIN_DISTINCT} distinct characters; generate one: openssl rand -hex 32")]
    return []


def _pairs(settings) -> list[Problem]:
    out = []
    for a, b in PAIRS:
        has_a, has_b = bool(_text(_get(settings, a))), bool(_text(_get(settings, b)))
        if has_a != has_b:
            given, missing = (a, b) if has_a else (b, a)
            out.append(Problem(missing, f"is empty while APP_{given} is set: set both or neither"))
    for setting, needs in NEEDS:
        if _text(_get(settings, setting)) and not _text(_get(settings, needs)):
            out.append(Problem(needs, f"is empty while APP_{setting} is set"))
    return out


def _fernet_ok(key: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(key.encode())) == 32
    except (binascii.Error, ValueError):
        return False


def _formats(settings) -> list[Problem]:
    out = []
    sse = _text(_get(settings, "BACKUP_S3_SSE")) or "AES256"
    if sse not in ("AES256", "aws:kms"):
        out.append(Problem("BACKUP_S3_SSE", "must be AES256 or aws:kms"))
    for name in FERNET_KEYS:
        raw = _text(_get(settings, name))
        if raw and not all(_fernet_ok(k.strip()) for k in raw.split(",") if k.strip()):
            out.append(Problem(name, "is not a Fernet key (or a comma-separated list of them)"))
    dsn = _text(_get(settings, "SENTRY_DSN"))
    if dsn:
        try:
            parts = urlsplit(dsn)
            ok = parts.scheme in ("https", "http") and bool(parts.username) and bool(parts.hostname) and parts.path.strip("/") != ""
        except ValueError:
            ok = False
        if not ok:
            out.append(Problem("SENTRY_DSN", "is not a Sentry DSN (https://<key>@<host>/<project>)"))
    b64 = _text(_get(settings, "FIREBASE_SERVICE_ACCOUNT_BASE64"))
    if b64:
        try:
            account = json.loads(base64.b64decode(b64, validate=False))
            ok = isinstance(account, dict) and bool(account.get("private_key")) and bool(account.get("client_email"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            ok = False
        if not ok:
            out.append(Problem("FIREBASE_SERVICE_ACCOUNT_BASE64", "is not a base64-encoded Firebase service account"))
    env = _text(_get(settings, "ENV")).lower()
    if env == "production" and bool(_get(settings, "DEBUG")):
        out.append(Problem("DEBUG", "is on while APP_ENV is production"))
    return out


def _warnings(settings) -> list[Problem]:
    out = []
    if _text(_get(settings, "BACKUP_S3_KMS_KEY_ID")) and _text(_get(settings, "BACKUP_S3_SSE")) != "aws:kms":
        out.append(Problem("BACKUP_S3_KMS_KEY_ID", "is set but APP_BACKUP_S3_SSE is not aws:kms, so it is not used", fatal=False))
    if _text(_get(settings, "RAZORPAY_KEY_ID")).startswith("rzp_test_"):
        out.append(Problem("RAZORPAY_KEY_ID", "is a test key: payments take no real money", fatal=False))
    path = _text(_get(settings, "FIREBASE_SERVICE_ACCOUNT_PATH"))
    if path and not os.path.isfile(path):
        out.append(Problem("FIREBASE_SERVICE_ACCOUNT_PATH", "names a file that does not exist: no push notifications", fatal=False))
    elif not path and not _text(_get(settings, "FIREBASE_SERVICE_ACCOUNT_BASE64")):
        out.append(Problem("FIREBASE_SERVICE_ACCOUNT_BASE64", "and _PATH are both empty: no push notifications", fatal=False))
    if not _text(_get(settings, "SENTRY_DSN")):
        out.append(Problem("SENTRY_DSN", "is empty: errors are not tracked", fatal=False))
    if not _text(_get(settings, "BACKUP_S3_BUCKET")):
        out.append(Problem("BACKUP_S3_BUCKET", "is empty: backups stay on the database's own disk", fatal=False))
    if _get(settings, "AI_ENABLED") and not _text(_get(settings, "GEMINI_API_KEY")):
        out.append(Problem("GEMINI_API_KEY", "is empty while APP_AI_ENABLED is on: only clubs' own keys work", fatal=False))
    if _text(_get(settings, "RESEND_API_KEY")) and not _text(_get(settings, "RESEND_WEBHOOK_SECRET")):
        out.append(Problem("RESEND_WEBHOOK_SECRET", "is empty: Resend's delivery reports are all refused", fatal=False))
    short = [t for t in _text(_get(settings, "SITE_TOKENS")).split(",") if t.strip() and len(t.strip()) < 32]
    if short:
        out.append(Problem("SITE_TOKENS", "has a token shorter than 32 characters, which is ignored", fatal=False))
    origins = _get(settings, "CORS_ORIGINS")
    listed = origins.split(",") if isinstance(origins, str) else (origins or [])
    if any(_text(o) == "*" for o in listed):
        out.append(Problem("CORS_ORIGINS", "includes *: any site may call the API from a browser", fatal=False))
    return out


def check(settings) -> list[Problem]:
    """Every finding for these settings, fatal ones first. Empty outside
    production (is_production)."""
    if not is_production(settings):
        return []
    problems = [*_required(settings), *_placeholders(settings), *_secret_key(settings), *_pairs(settings), *_formats(settings), *_warnings(settings)]
    # One line per setting and kind: a placeholder that is also "empty" is said once.
    unique: dict[tuple[str, bool], Problem] = {}
    for p in problems:
        unique.setdefault((p.setting, p.fatal), p)
    return sorted(unique.values(), key=lambda p: (not p.fatal, p.setting))


def enforce(settings=None, *, log: Optional[logging.Logger] = None) -> list[Problem]:
    """Raise ConfigError on any fatal finding in production; log the rest.
    With APP_CONFIG_CHECKS=warn, log the fatal ones at ERROR and go on.
    Returns the findings."""
    if settings is None:
        from app.config import settings
    log = log or logger
    problems = check(settings)
    fatal = [p for p in problems if p.fatal]
    for p in problems:
        if not p.fatal:
            log.warning("config: %s", p)
    if fatal:
        if _text(_get(settings, "CONFIG_CHECKS")).lower() == "warn":
            for p in fatal:
                log.error("config (APP_CONFIG_CHECKS=warn, starting anyway): %s", p)
        else:
            raise ConfigError(fatal)
    return problems


#: The exit status of a process that refused to start (EX_CONFIG).
EXIT_CONFIG = 78


def enforce_or_exit(settings=None) -> None:
    """enforce() for an entry point (main.py, app/worker.py): on a refusal,
    the reasons on stderr and exit status 78, before anything is started --
    so the container stops and Docker's restart backs off, rather than
    uvicorn restarting workers that each refuse."""
    try:
        enforce(settings)
    except ConfigError as error:
        print(error, file=sys.stderr, flush=True)
        raise SystemExit(EXIT_CONFIG) from None


# ─── By hand ─────────────────────────────────────────────────────────────

#: What docker-compose.prod.yml sets itself, over the .env: kept from this
#: process's environment when a .env is read from stdin.
_COMPOSE_SET = ("APP_DATABASE_URL", "APP_DATABASE_URL_SYNC", "APP_PROCESS")


def _settings_from_env_file(text: str):
    import io

    from dotenv import dotenv_values

    from app.config import AppConfig

    values = {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}
    for name in [k for k in os.environ if k.startswith("APP_") and k not in _COMPOSE_SET]:
        del os.environ[name]
    for key, value in values.items():
        if key.startswith("APP_") and key not in _COMPOSE_SET:
            os.environ[key] = value
    return AppConfig()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check this process's settings as production would at startup.")
    parser.add_argument("--env-file", help="read the settings from this .env ('-' for stdin) instead of the environment")
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            text = sys.stdin.read() if args.env_file == "-" else open(args.env_file).read()
            settings = _settings_from_env_file(text)
        else:
            from app.config import settings
    except Exception as error:  # noqa: BLE001 -- pydantic's own refusal, names only
        try:
            found = error.errors(include_input=False)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            found = []
        for e in found:
            # The message alone: a validator's message never quotes the value.
            print(f"FATAL APP_{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg', 'invalid')}")
        if not found:
            print(f"FATAL the settings could not be read: {type(error).__name__}")
        return 1
    if not is_production(settings):
        print("not production (APP_ENV or APP_DEBUG): nothing is enforced; checking as if it were")
        settings = settings.model_copy(update={"DEBUG": False, "ENV": "production"}) if hasattr(settings, "model_copy") else settings
    problems = check(settings)
    for p in problems:
        print(f"{'FATAL' if p.fatal else 'warn '} {p}")
    fatal = sum(1 for p in problems if p.fatal)
    print(f"{fatal} fatal, {len(problems) - fatal} warning(s)" if problems else "OK: nothing missing, no placeholders")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    sys.exit(main())
