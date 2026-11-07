"""The settings file: what it must hold, its mode, and the token never shown."""

import os

import pytest
from conftest import TOKEN

from opsbot import config


def write(tmp_path, text, mode=0o600):
    path = tmp_path / "ops-bot.env"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_a_complete_file_loads(tmp_path):
    path = write(
        tmp_path,
        f'# comment\nTELEGRAM_BOT_TOKEN="{TOKEN}"\nexport TELEGRAM_CHAT_ID=-1001234\n'
        "OPS_HOST_LABEL=moa-prod\nOPS_ERROR_IGNORE_SERVICES=postgres, caddy\n",
    )
    cfg = config.load(path)
    assert cfg.token == TOKEN and cfg.chat_id == -1001234 and cfg.host_label == "moa-prod"
    assert cfg.age_recipient == config.DEFAULT_AGE_RECIPIENT
    assert cfg.error_ignore_services == {"postgres", "caddy"}
    assert TOKEN not in repr(cfg) and TOKEN not in str(cfg)


def test_the_default_recipient_is_an_age_key():
    assert config.AGE_RECIPIENT_RE.match(config.DEFAULT_AGE_RECIPIENT)


@pytest.mark.parametrize(
    "text, unset, says",
    [
        ("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=1\n", True, "TELEGRAM_BOT_TOKEN is empty"),
        (f"TELEGRAM_BOT_TOKEN={TOKEN}\n", False, "TELEGRAM_CHAT_ID is empty"),
        ("TELEGRAM_BOT_TOKEN=nonsense\nTELEGRAM_CHAT_ID=1\n", False, "does not look like a bot token"),
        (f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=@owner\n", False, "must be a number"),
        (f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=1\nOPS_AGE_RECIPIENT=age1short\n", False, "not an age public key"),
        (f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=1\nOPS_WATCH_INTERVAL=1\n", False, "between"),
    ],
)
def test_incomplete_or_wrong_settings_say_what(tmp_path, text, unset, says):
    with pytest.raises(config.ConfigError) as caught:
        config.load(write(tmp_path, text))
    assert says in str(caught.value) and caught.value.unset is unset
    assert TOKEN not in str(caught.value)


def test_a_missing_file_is_not_set_up(tmp_path):
    with pytest.raises(config.ConfigError) as caught:
        config.load(tmp_path / "nope.env")
    assert caught.value.unset


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_the_file_must_be_the_owners_alone(tmp_path, mode):
    path = write(tmp_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=1\n", mode)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.load(path)


def test_nothing_is_expanded(tmp_path):
    values = config.parse_env("A=$(rm -rf /)\nB='x y'\nlower=no\nC\n")
    assert values == {"A": "$(rm -rf /)", "B": "x y"}
