"""Production refuses to start on a missing, placeholder, weak or half-set
secret (app/core/config_checks.py), and says which setting without its value.

Settings are built with AppConfig(**values) -- init values win over the
suite's environment -- and `model_copy(update=...)` where a value would not
get past pydantic's own validators."""
import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import AppConfig, settings
from app.core import config_checks as cc

BACKEND = Path(__file__).resolve().parents[1]

FIREBASE = base64.b64encode(json.dumps({
    "type": "service_account", "client_email": "push@moa-app.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----\n",
}).encode()).decode()

#: A production configuration with nothing wrong in it (made-up values).
GOOD = dict(
    ENV="production",
    DEBUG=False,
    SECRET_KEY="9f2c7e1a4b8d6f3e0a5c9b7d2e4f6a8c1b3d5e7f9a0c2e4b6d8f0a1c3e5b7d9f",
    DATABASE_URL="postgresql+asyncpg://moa:Zq8vN3pL7wR2kK9m@postgres:5432/moa",
    DATABASE_URL_SYNC="postgresql://moa:Zq8vN3pL7wR2kK9m@postgres:5432/moa",
    S3_BUCKET="moa-media-ap-south-1",
    S3_ACCESS_KEY="AKIA2Q7J5H3WN8PLM4RT",
    S3_SECRET_KEY="u7Hk2Lq9Zp4Rt8Wm3Nb6Vc1Xs5Df0Gj7Ka2Ye9Ui",
    S3_BASE_PATH="https://moa-media-ap-south-1.s3.ap-south-1.amazonaws.com",
    SES_ACCESS_KEY="AKIA6R4T2Y8U0I9O7P5L",
    SES_SECRET_KEY="Tq3Wm8Zr1Lp6Kn4Vb9Xc2Hs7Jd5Fg0Ya3Ue8Io1",
    SES_DEFAULT_SENDER="info@myotherapp.in",
    RAZORPAY_KEY_ID="rzp_live_Q8dK2mZ7pL4wR9",
    RAZORPAY_KEY_SECRET="h3Jk8Lm2Np7Qr4St9Uv1Wy6Zb",
    RAZORPAY_WEBHOOK_SECRET="w9Ex4Rt7Yu2Io5Pa8Sd3Fg6Hj",
    SENTRY_DSN="https://4f1c2a9e7b6d@o451234.ingest.sentry.io/6012345",
    BACKUP_S3_BUCKET="moa-backups",
    FIREBASE_SERVICE_ACCOUNT_BASE64=FIREBASE,
)


def make(**values) -> AppConfig:
    return AppConfig(**{**GOOD, **values})


def bypass(**values) -> AppConfig:
    """Values pydantic's own validators would refuse first."""
    return make().model_copy(update=values)


def fatal(config) -> dict[str, str]:
    return {p.setting: p.reason for p in cc.check(config) if p.fatal}


def warnings(config) -> dict[str, str]:
    return {p.setting: p.reason for p in cc.check(config) if not p.fatal}


# ---------------------------------------------------------------------------
# What counts as production
# ---------------------------------------------------------------------------


def test_a_clean_production_configuration_passes():
    assert fatal(make()) == {}
    assert cc.enforce(make()) == cc.check(make())


def test_the_suite_itself_is_not_production():
    assert settings.ENV == "test"
    assert not cc.is_production(settings)
    assert cc.check(settings) == []


@pytest.mark.parametrize(
    "env, debug, production",
    [
        ("", False, True),       # unset: production unless DEBUG
        ("", True, False),
        ("production", False, True),
        ("staging", False, True),
        ("development", False, False),
        ("test", False, False),
        ("ci", False, False),
        ("local", True, False),
    ],
)
def test_production_is_named_or_debug_off(env, debug, production):
    assert cc.is_production(make(ENV=env, DEBUG=debug)) is production


def test_nothing_is_enforced_outside_production():
    assert cc.check(make(ENV="development", SECRET_KEY="short")) == []
    assert cc.enforce(make(ENV="", DEBUG=True, SECRET_KEY="short")) == []


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", cc.REQUIRED)
def test_a_required_secret_that_is_empty(name):
    assert name in fatal(bypass(**{name: ""}))


def test_the_mail_providers_own_settings():
    assert set(fatal(bypass(SES_ACCESS_KEY="", SES_SECRET_KEY=""))) >= {"SES_ACCESS_KEY", "SES_SECRET_KEY"}
    resend = make(MAIL_PROVIDER="resend", RESEND_API_KEY="")
    assert "RESEND_API_KEY" in fatal(resend)
    ok = make(MAIL_PROVIDER="resend", RESEND_API_KEY="re_9Kd2Lm7Qp4Zx8Wv3Nb6", RESEND_WEBHOOK_SECRET="whsec_Lp8Kq3Zm9")
    assert fatal(ok) == {}
    # With Resend, SES is only its fallback: missing it is a warning.
    no_fallback = bypass(MAIL_PROVIDER="resend", RESEND_API_KEY="re_9Kd2Lm7Qp4Zx8Wv3Nb6", SES_ACCESS_KEY="", SES_SECRET_KEY="")
    assert fatal(no_fallback) == {} and "SES_ACCESS_KEY" in warnings(no_fallback)
    assert "MAIL_PROVIDER" in fatal(make(MAIL_PROVIDER="sendgrid"))


#: SES through its SMTP interface (made-up credentials).
SMTP = dict(
    MAIL_PROVIDER="ses_smtp", SMTP_HOST="email-smtp.ap-south-1.amazonaws.com", SMTP_PORT=587,
    SMTP_USERNAME="AKIA4Q7J2H8WN3PLM5RT", SMTP_PASSWORD="BHk2Lq9Zp4Rt8Wm3Nb6Vc1Xs5Df0Gj7Ka2Ye9Ui3Op",
)


def test_ses_over_smtp_needs_its_credentials_and_no_api_key():
    assert cc.MAIL_REQUIRED["ses_smtp"] == ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SES_DEFAULT_SENDER")
    # The API keys may be left out of the .env altogether.
    assert AppConfig.model_fields["SES_ACCESS_KEY"].default == "" and AppConfig.model_fields["SES_SECRET_KEY"].default == ""
    assert fatal(bypass(**SMTP, SES_ACCESS_KEY="", SES_SECRET_KEY="")) == {}
    for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SES_DEFAULT_SENDER"):
        assert name in fatal(bypass(**{**SMTP, "SES_ACCESS_KEY": "", "SES_SECRET_KEY": "", name: ""})), name
    # ses (the API) still needs its keys, whatever SMTP holds.
    assert {"SES_ACCESS_KEY", "SES_SECRET_KEY"} <= set(fatal(bypass(**{**SMTP, "MAIL_PROVIDER": "ses"}, SES_ACCESS_KEY="", SES_SECRET_KEY="")))


@pytest.mark.parametrize("host", ["smtp://email-smtp.ap-south-1.amazonaws.com", "email-smtp.ap-south-1.amazonaws.com:587", "localhost"])
def test_the_smtp_host_is_a_bare_host_name(host):
    assert "SMTP_HOST" in fatal(make(**{**SMTP, "SMTP_HOST": host}))


@pytest.mark.parametrize("port, bad", [(587, False), (2587, False), (465, True), (2465, True), (0, True)])
def test_the_smtp_port_speaks_starttls(port, bad):
    assert ("SMTP_PORT" in fatal(make(**{**SMTP, "SMTP_PORT": port}))) is bad


def test_a_placeholder_smtp_password_is_refused():
    assert "SMTP_PASSWORD" in fatal(make(**{**SMTP, "SMTP_PASSWORD": "PASTE_PASSWORD_HERE"}))


def test_resend_falls_back_on_smtp_without_a_warning():
    resend = dict(MAIL_PROVIDER="resend", RESEND_API_KEY="re_9Kd2Lm7Qp4Zx8Wv3Nb6", RESEND_WEBHOOK_SECRET="whsec_Lp8Kq3Zm9")
    with_smtp = bypass(**{**SMTP, **resend}, SES_ACCESS_KEY="", SES_SECRET_KEY="")
    assert fatal(with_smtp) == {} and "SES_ACCESS_KEY" not in warnings(with_smtp)


@pytest.mark.parametrize(
    "name, value",
    [
        ("RAZORPAY_KEY_SECRET", "PASTE_KEY_HERE"),
        ("RAZORPAY_KEY_SECRET", "paste-your-secret-here"),
        ("S3_SECRET_KEY", "changeme"),
        ("S3_SECRET_KEY", "ChangeMe123"),
        ("SES_SECRET_KEY", "REPLACE_ME"),
        ("RAZORPAY_KEY_ID", "rzp_live_xxxxxxxxxxxx"),
        ("S3_ACCESS_KEY", "AKIAXXXXXXXXXXXXXXXX"),
        ("SES_DEFAULT_SENDER", "noreply@example.com"),
        ("SENTRY_DSN", "<your sentry dsn>"),
        ("GEMINI_API_KEY", "your-gemini-api-key"),
        ("S3_ACCESS_KEY", "${S3_ACCESS_KEY}"),
        ("S3_BUCKET", "your_bucket_name"),
        ("DATABASE_URL", "postgresql+asyncpg://moa:password@postgres:5432/moa"),
        ("DATABASE_URL_SYNC", "postgresql://moa:changeme@postgres:5432/moa"),
        ("ADMIN_ALERT_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/<id>/<token>"),
        ("RAZORPAY_WEBHOOK_SECRET", "secret"),
        ("CORS_ORIGINS", ["https://myotherapp.in", "https://your-dashboard.example.com"]),
    ],
)
def test_a_placeholder_anywhere_is_refused(name, value):
    config = bypass(**{name: value})
    assert name in fatal(config)
    assert fatal(config)[name] == "holds a placeholder, not a real value"


@pytest.mark.parametrize(
    "value",
    [
        "rzp_live_Q8dK2mZ7pL4wR9", "AKIA2Q7J5H3WN8PLM4RT", "exchange_merchant_key_9Lk2", "0x1f9a8b7c6d5e",
        "https://myotherapp.in", "https://{slug}.myotherapp.in", "https://ipapi.co/{ip}/json/", "info@myotherapp.in",
        "Yourstory Events", "postgresql://moa:Zq8vN3pL7wR2kK9m@postgres:5432/moa", "60/minute;2000/day",
    ],
)
def test_real_values_are_not_placeholders(value):
    assert not cc.is_placeholder(value)


def test_no_default_setting_is_a_placeholder():
    """Every value the code ships as a default passes, so a production .env
    that leaves them alone is never refused for them."""
    defaults = {name: field.default for name, field in AppConfig.model_fields.items() if isinstance(field.default, str)}
    assert defaults
    assert [name for name, value in defaults.items() if cc.is_placeholder(value)] == []


def test_a_mode_is_not_mistaken_for_a_placeholder_secret():
    """Whole-value placeholders (none, test, default...) count only for
    credentials, ids and addresses."""
    assert "AI_GEMINI_THINKING" not in fatal(make(AI_GEMINI_THINKING="none"))
    assert "GEMINI_API_KEY" in fatal(make(GEMINI_API_KEY="none"))


@pytest.mark.parametrize(
    "key, why",
    [
        ("short-but-random-9f2c", "shorter than 32"),
        ("a" * 48, "distinct characters"),
        ("ab" * 24, "distinct characters"),
        ("test-secret-key-not-for-production", "sample or a test"),
        ("ci-test-secret-key", "sample or a test"),
    ],
)
def test_a_weak_or_default_secret_key(key, why):
    assert why in fatal(make(SECRET_KEY=key))["SECRET_KEY"]


@pytest.mark.parametrize(
    "values, missing",
    [
        ({"RAZORPAY_KEY_SECRET": ""}, "RAZORPAY_KEY_SECRET"),
        ({"WHATSAPP_ACCESS_TOKEN": "EAAG9xLk2Pq7Zm4Wv8Rt3Nb"}, "WHATSAPP_PHONE_NUMBER_ID"),
        ({"WHATSAPP_PHONE_NUMBER_ID": "104729384756201"}, "WHATSAPP_ACCESS_TOKEN"),
        ({"CLOUDFLARE_API_TOKEN": "cf9Lk2Pq7Zm4Wv8Rt3Nb6Xc1"}, "CLOUDFLARE_ZONE_ID"),
        ({"BACKUP_S3_ACCESS_KEY": "AKIA7Y4T2R8E0W9Q5P3L"}, "BACKUP_S3_SECRET_KEY"),
        ({"AI_FALLBACK_PROVIDER": "anthropic"}, "AI_FALLBACK_API_KEY"),
        ({"SEARCH_URL": "http://meilisearch:7700"}, "SEARCH_KEY"),
    ],
)
def test_a_half_configured_integration(values, missing):
    problems = fatal(bypass(**values))
    assert missing in problems
    # A required setting says so first (one line per setting); the rest say which half is there.
    expected = "is empty; the platform cannot run without it" if missing in cc.REQUIRED else "is empty while APP_"
    assert expected in problems[missing]


def test_both_halves_set_or_neither_is_fine():
    assert fatal(make(WHATSAPP_ACCESS_TOKEN="EAAG9xLk2Pq7Zm4Wv8Rt3Nb", WHATSAPP_PHONE_NUMBER_ID="104729384756201")) == {}
    assert fatal(make(WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID="")) == {}


def test_keys_in_the_wrong_shape():
    good = Fernet.generate_key().decode()
    assert fatal(make(PAYMENT_KEYS_SECRET=f"{good},{Fernet.generate_key().decode()}")) == {}
    assert "PAYMENT_KEYS_SECRET" in fatal(make(PAYMENT_KEYS_SECRET=f"{good},not-a-fernet-key"))
    assert "AI_KEYS_SECRET" in fatal(make(AI_KEYS_SECRET="0123456789abcdef"))
    assert "SENTRY_DSN" in fatal(make(SENTRY_DSN="sentry.io/12345"))
    assert "FIREBASE_SERVICE_ACCOUNT_BASE64" in fatal(make(FIREBASE_SERVICE_ACCOUNT_BASE64="bm90IGpzb24="))
    assert "BACKUP_S3_SSE" in fatal(make(BACKUP_S3_SSE="aes256"))
    assert "DEBUG" in fatal(make(DEBUG=True))  # APP_ENV=production says so


def test_softer_findings_only_warn():
    config = make(
        RAZORPAY_KEY_ID="rzp_test_Q8dK2mZ7pL4wR9", SENTRY_DSN="", BACKUP_S3_BUCKET="", FIREBASE_SERVICE_ACCOUNT_BASE64="",
        AI_ENABLED=True, SITE_TOKENS="tooshort", CORS_ORIGINS=["*"], BACKUP_S3_KMS_KEY_ID="alias/moa",
    )
    assert fatal(config) == {}
    assert set(warnings(config)) >= {
        "RAZORPAY_KEY_ID", "SENTRY_DSN", "BACKUP_S3_BUCKET", "FIREBASE_SERVICE_ACCOUNT_BASE64", "GEMINI_API_KEY",
        "SITE_TOKENS", "CORS_ORIGINS", "BACKUP_S3_KMS_KEY_ID",
    }
    assert cc.enforce(config)  # returned, not raised


# ---------------------------------------------------------------------------
# The media bucket on a store of our own (app/core/storage/clients.py)
# ---------------------------------------------------------------------------

#: Garage on the production server, as its .env says it (made-up keys).
GARAGE = dict(
    S3_ENDPOINT_URL="http://media:3900",
    S3_PUBLIC_URL="https://media.myotherapp.in",
    S3_REGION="garage",
    S3_BASE_PATH="",
    S3_BUCKET="moa-media",
    S3_ACCESS_KEY="GK31c2f218a2e44f485b94239e",
    S3_SECRET_KEY="b892c0665f0ada8a4755dae98baa3b133590e11dae3bcc1f9d769d67f16c3835",
    BACKUP_S3_ACCESS_KEY="AKIA7Y4T2R8E0W9Q5P3L",
    BACKUP_S3_SECRET_KEY="Hk2Lq9Zp4Rt8Wm3Nb6Vc1Xs5Df0Gj7Ka2Ye9Ui8",
)


def test_a_media_store_of_our_own_passes():
    config = make(**GARAGE)
    assert fatal(config) == {}
    assert not {"S3_ENDPOINT_URL", "S3_PUBLIC_URL", "S3_BASE_PATH", "MEDIA_CDN_URL"} & set(warnings(config))


def test_the_store_addresses_are_kept_without_a_trailing_slash():
    config = make(S3_ENDPOINT_URL=" http://media:3900/ ", S3_PUBLIC_URL="https://media.myotherapp.in/", S3_REGION=" garage ")
    assert (config.S3_ENDPOINT_URL, config.S3_PUBLIC_URL, config.S3_REGION) == ("http://media:3900", "https://media.myotherapp.in", "garage")


def test_an_empty_base_path_is_right_not_missing():
    assert fatal(make(S3_BASE_PATH="")) == {}
    assert "S3_BASE_PATH" not in cc.REQUIRED


@pytest.mark.parametrize(
    "name, value",
    [
        ("S3_ENDPOINT_URL", "media:3900"),
        ("S3_ENDPOINT_URL", "ftp://media:3900"),
        ("S3_PUBLIC_URL", "media.myotherapp.in"),
        ("S3_PUBLIC_URL", "https://media.myotherapp.in?bucket=moa-media"),
    ],
)
def test_a_store_address_that_is_not_http(name, value):
    assert "is not an http(s) address" in fatal(make(**{**GARAGE, name: value}))[name]


def test_a_public_address_without_the_endpoint_is_half_set():
    problems = fatal(make(**{**GARAGE, "S3_ENDPOINT_URL": ""}))
    assert problems["S3_ENDPOINT_URL"] == "is empty while APP_S3_PUBLIC_URL is set"


def test_links_that_are_not_https_only_warn():
    plain = make(**{**GARAGE, "S3_PUBLIC_URL": "http://media.myotherapp.in"})
    assert fatal(plain) == {} and warnings(plain)["S3_PUBLIC_URL"] == "is not https"
    internal = make(**{**GARAGE, "S3_PUBLIC_URL": ""})
    assert fatal(internal) == {} and "APP_S3_ENDPOINT_URL, which is not https" in warnings(internal)["S3_PUBLIC_URL"]


def test_the_media_cdn_on_the_stores_own_host_is_refused():
    assert "no bucket in it" in fatal(make(**GARAGE, MEDIA_CDN_URL="https://media.myotherapp.in"))["MEDIA_CDN_URL"]
    assert fatal(make(**GARAGE, MEDIA_CDN_URL="https://cdn.myotherapp.in")) == {}
    assert fatal(make(MEDIA_CDN_URL="https://media.myotherapp.in")) == {}  # on AWS, as before


def test_backups_never_fall_back_to_the_stores_keys():
    no_keys = {**GARAGE, "BACKUP_S3_ACCESS_KEY": "", "BACKUP_S3_SECRET_KEY": ""}
    assert "off-site backups would send the media store's keys to AWS" in fatal(make(**no_keys))["BACKUP_S3_ACCESS_KEY"]
    assert fatal(make(**{**no_keys, "BACKUP_S3_BUCKET": ""})) == {}  # no off-site copies: nothing to send
    assert fatal(make(BACKUP_S3_ACCESS_KEY="", BACKUP_S3_SECRET_KEY="")) == {}  # on AWS the media keys may do, as before


def test_a_base_path_that_is_an_address_warns_on_our_store():
    config = make(**{**GARAGE, "S3_BASE_PATH": "https://media.myotherapp.in/moa-media"})
    assert fatal(config) == {}
    assert "folder inside the bucket" in warnings(config)["S3_BASE_PATH"]
    assert "S3_BASE_PATH" not in warnings(make())  # AWS with an address-like base path: as before


# ---------------------------------------------------------------------------
# Refusing, and never saying the value
# ---------------------------------------------------------------------------

SECRETS = {
    "RAZORPAY_KEY_SECRET": "",
    "S3_SECRET_KEY": "changeme-Lk29Pq",
    "SECRET_KEY": "weak-Zm4Wv8",
    "WHATSAPP_ACCESS_TOKEN": "EAAG-leaky-token-77Qp",
    "DATABASE_URL": "postgresql+asyncpg://moa:password@postgres:5432/moa",
}


NAMES = ("RAZORPAY_KEY_SECRET", "S3_SECRET_KEY", "SECRET_KEY", "WHATSAPP_PHONE_NUMBER_ID", "DATABASE_URL")
VALUES = ("changeme-Lk29Pq", "weak-Zm4Wv8", "EAAG-leaky-token-77Qp", "moa:password@")


def test_warn_is_the_default_and_starts_saying_everything_once(caplog):
    """Production's .env may hold a placeholder today: the first deploy of
    these checks must report it, not take the API down."""
    assert AppConfig.model_fields["CONFIG_CHECKS"].default == "warn"
    config = bypass(**SECRETS)
    assert not cc.enforcing(config)
    with caplog.at_level(logging.WARNING, logger="app.config_checks"):
        problems = cc.enforce(config)  # no raise
    assert {p.setting for p in problems if p.fatal} >= {"RAZORPAY_KEY_SECRET", "S3_SECRET_KEY", "SECRET_KEY"}
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors  # one line, so one Sentry event, naming them all
    for name in NAMES:
        assert f"APP_{name}" in errors[0]
    assert "starting anyway" in errors[0] and "APP_CONFIG_CHECKS=enforce" in errors[0]
    for value in VALUES:
        assert not any(value in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["", "warn", "WARN", "enforced", "off"])
def test_anything_but_enforce_only_reports(mode):
    assert not cc.enforcing(bypass(CONFIG_CHECKS=mode))
    assert cc.enforce(bypass(**SECRETS, CONFIG_CHECKS=mode))


def test_enforce_refuses_naming_every_setting_and_no_value():
    config = bypass(**SECRETS, CONFIG_CHECKS="enforce")
    assert cc.enforcing(config) and cc.enforcing(bypass(CONFIG_CHECKS=" Enforce "))
    with pytest.raises(cc.ConfigError) as caught:
        cc.enforce(config)
    text = str(caught.value)
    for name in NAMES:
        assert f"APP_{name}" in text
    for value in VALUES:
        assert value not in text
    assert "APP_CONFIG_CHECKS=warn" in text  # how to start anyway
    assert {p.setting for p in caught.value.problems} >= {"RAZORPAY_KEY_SECRET", "S3_SECRET_KEY", "SECRET_KEY"}


def test_the_entry_points_exit_only_when_enforcing(caplog, capsys):
    warn = bypass(**SECRETS)
    with caplog.at_level(logging.WARNING, logger="app.config_checks"):
        cc.enforce_or_exit(warn, report=False)  # main.py: the workers' startup reports it
    assert caplog.records == []
    with caplog.at_level(logging.WARNING, logger="app.config_checks"):
        cc.enforce_or_exit(warn)
    assert [r for r in caplog.records if r.levelno == logging.ERROR]
    with pytest.raises(SystemExit) as caught:
        cc.enforce_or_exit(bypass(**SECRETS, CONFIG_CHECKS="enforce"), report=False)
    assert caught.value.code == cc.EXIT_CONFIG == 78
    err = capsys.readouterr().err
    assert "Refusing to start" in err and "APP_S3_SECRET_KEY" in err and "changeme-Lk29Pq" not in err


def test_a_value_pydantic_refuses_is_not_printed_either():
    with pytest.raises(ValidationError) as caught:
        AppConfig(**{**GOOD, "LINK_SIGNING_KEYS": "short-link-key-Qz8"})
    assert "LINK_SIGNING_KEYS" in str(caught.value)
    assert "short-link-key-Qz8" not in str(caught.value)


# ---------------------------------------------------------------------------
# The processes that refuse: the API's entry point, the worker, the lifespan
# ---------------------------------------------------------------------------


def _production_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("APP_")}
    env.update({f"APP_{k}": (json.dumps(v) if isinstance(v, list) else str(v)) for k, v in GOOD.items()})
    env.update({
        "APP_CORS_ORIGINS": '["https://myotherapp.in"]', "APP_GOOGLE_WEB_CLIENT_ID": "1234-web.apps.googleusercontent.com",
        "APP_GOOGLE_ANDROID_CLIENT_ID": "1234-android.apps.googleusercontent.com",
        "APP_GOOGLE_IOS_CLIENT_ID": "1234-ios.apps.googleusercontent.com",
        "APP_ACCESS_TOKEN_EXPIRE_MINUTES": "15", "APP_REFRESH_TOKEN_EXPIRE_DAYS": "7", "PYTHONPATH": str(BACKEND),
    })
    env.update({f"APP_{k}": v for k, v in extra.items()})
    return env


@pytest.mark.parametrize("command", [["main.py"], ["-m", "app.worker"]])
def test_under_enforce_the_api_and_the_worker_refuse_to_start(command):
    env = _production_env(S3_SECRET_KEY="PASTE_KEY_HERE", PORT="0", CONFIG_CHECKS="enforce")
    run = subprocess.run([sys.executable, *command], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 78, (run.stdout[-2000:], run.stderr[-2000:])
    assert "APP_S3_SECRET_KEY" in run.stderr
    assert "PASTE_KEY_HERE" not in run.stderr + run.stdout
    assert "Uvicorn running" not in run.stderr


def test_main_and_the_worker_leave_warn_to_the_startup_that_has_sentry():
    """Under warn the entry points neither exit nor log (report=False): the
    API's lifespan and the worker's main(), where Sentry is on, report."""
    assert "config_checks.enforce_or_exit(report=False)" in (BACKEND / "main.py").read_text()
    worker = (BACKEND / "app" / "worker.py").read_text()
    assert "config_checks.enforce_or_exit(report=False)" in worker
    assert worker.index("import doth") < worker.index("config_checks.enforce()")
    assert "config_checks.enforce()" in (BACKEND / "doth.py").read_text()


async def test_under_enforce_the_lifespan_refuses_too(monkeypatch):
    import doth

    monkeypatch.setattr(doth.settings, "ENV", "production")
    monkeypatch.setattr(doth.settings, "SECRET_KEY", "short")
    monkeypatch.setattr(doth.settings, "CONFIG_CHECKS", "enforce")
    with pytest.raises(cc.ConfigError):
        async with doth.lifespan(doth.application):
            pass


def test_checking_a_dotenv_by_hand():
    env = _production_env()
    dotenv = "\n".join(f"{k}={v}" for k, v in env.items() if k.startswith("APP_") and k not in ("APP_DATABASE_URL", "APP_DATABASE_URL_SYNC"))
    clean = subprocess.run(
        [sys.executable, "-m", "app.core.config_checks", "--env-file", "-"], cwd=BACKEND, env=env,
        input=dotenv, capture_output=True, text=True, timeout=60,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "APP_CONFIG_CHECKS=enforce can be set" in clean.stdout
    bad_lines = "\nAPP_RAZORPAY_KEY_SECRET=rzp_xxxxxxxx\nAPP_SECRET_KEY=hunter2hunter2\nAPP_PAYMENT_KEYS_SECRET=PASTE_KEY_HERE\n"
    broken = subprocess.run(
        [sys.executable, "-m", "app.core.config_checks", "--env-file", "-"], cwd=BACKEND, env=env,
        input=dotenv + bad_lines, capture_output=True, text=True, timeout=60,
    )
    assert broken.returncode == 1
    for name in ("RAZORPAY_KEY_SECRET", "SECRET_KEY", "PAYMENT_KEYS_SECRET"):
        assert f"FATAL APP_{name}" in broken.stdout
    assert "rzp_xxxxxxxx" not in broken.stdout and "hunter2" not in broken.stdout and "PASTE_KEY_HERE" not in broken.stdout
    assert "APP_CONFIG_CHECKS=warn: a start with these settings goes ahead" in broken.stdout
    enforced = subprocess.run(
        [sys.executable, "-m", "app.core.config_checks", "--env-file", "-"], cwd=BACKEND, env=env,
        input=dotenv + bad_lines + "APP_CONFIG_CHECKS=enforce\n", capture_output=True, text=True, timeout=60,
    )
    assert enforced.returncode == 1 and "APP_CONFIG_CHECKS=enforce: a start with these settings is refused" in enforced.stdout
