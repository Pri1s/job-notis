from __future__ import annotations

import stat
from pathlib import Path

import pytest

from job_notis.config import (
    DISCORD_WEBHOOK_ENV,
    Config,
    ConfigError,
    load_config,
    redact_secret,
    save_config,
    validate_webhook_url,
)
from job_notis.paths import AppPaths


def paths_for(root: Path) -> AppPaths:
    return AppPaths(
        app_support=root,
        config=root / "config.toml",
        database=root / "state.sqlite3",
        lock=root / "check.lock",
        log_dir=root / "logs",
        log_file=root / "logs" / "job-notis.log",
        launch_agent=root / "agent.plist",
    )


def test_save_and_load_config_with_private_permissions(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    config = Config("https://discord.com/api/webhooks/123456/secret-token")

    save_config(paths, config)

    assert load_config(paths) == config
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600


def test_load_missing_config_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configure"):
        load_config(paths_for(tmp_path))


def test_environment_webhook_takes_precedence_without_local_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        DISCORD_WEBHOOK_ENV,
        "https://discord.com/api/webhooks/123/environment-token",
    )
    paths = paths_for(tmp_path)

    assert load_config(paths).discord_webhook_url.endswith("/environment-token")


def test_load_rejects_non_table_request_section(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths.config.write_text(
        'request = []\n[discord]\nwebhook_url = "https://discord.com/api/webhooks/1/token"\n'
    )

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(paths)


@pytest.mark.parametrize(
    "request_value",
    [
        "timeout_seconds = true",
        'timeout_seconds = "20"',
        "retries = true",
        "retries = 3.9",
    ],
)
def test_load_rejects_coerced_request_values(
    tmp_path: Path,
    request_value: str,
) -> None:
    paths = paths_for(tmp_path)
    paths.config.write_text(
        '[discord]\nwebhook_url = "https://discord.com/api/webhooks/1/token"\n'
        f"[request]\n{request_value}\n"
    )

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(paths)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://discord.com/api/webhooks/123/token",
        "https://example.com/api/webhooks/123/token",
        "https://discord.com/channels/123/456",
        "https://discord.com:444/api/webhooks/123/token",
        "https://discord.com/api/webhooks/123/token#fragment",
        "https://discord.com/api/webhooks/123/to\nken",
        "https://discord.com/api/webhooks/123/to\tken",
    ],
)
def test_validate_webhook_rejects_non_webhook_urls(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_webhook_url(url)


def test_redact_secret_removes_webhook_token() -> None:
    value = (
        "failed HTTPS://DISCORD.COM/api/webhooks/123456/"
        "super.secret+token=!?thread_id=42&extra-secret=hidden"
    )
    redacted = redact_secret(value)
    assert "super.secret+token=!" not in redacted
    assert "extra-secret" not in redacted
    assert "REDACTED" in redacted
