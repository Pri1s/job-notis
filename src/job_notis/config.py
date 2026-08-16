"""Secure local configuration."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from job_notis.paths import AppPaths

DISCORD_WEBHOOK_ENV = "JOB_NOTIS_DISCORD_WEBHOOK_URL"
HARDWARE_DISCORD_WEBHOOK_ENV = "JOB_NOTIS_HARDWARE_DISCORD_WEBHOOK_URL"

_WEBHOOK_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/\d+/[^/]+/?$")
_DISCORD_HOSTS = {
    "discord.com",
    "canary.discord.com",
    "ptb.discord.com",
    "discordapp.com",
}


class ConfigError(RuntimeError):
    """Raised when local configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime settings that are intentionally small for a personal notifier."""

    discord_webhook_url: str
    request_timeout_seconds: float = 20.0
    request_retries: int = 3
    hardware_discord_webhook_url: str | None = None


def validate_webhook_url(value: str) -> str:
    """Validate and normalize an incoming Discord webhook URL."""

    value = value.strip()
    if any(character.isspace() for character in value):
        raise ConfigError("Expected an HTTPS Discord incoming-webhook URL.")
    parsed = urlsplit(value)
    try:
        has_port = parsed.port is not None
    except ValueError as exc:
        raise ConfigError("Expected an HTTPS Discord incoming-webhook URL.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DISCORD_HOSTS
        or has_port
        or not _WEBHOOK_PATH.fullmatch(parsed.path)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ConfigError("Expected an HTTPS Discord incoming-webhook URL.")
    return value


def load_config(paths: AppPaths) -> Config:
    """Read environment or local configuration and validate every value."""

    environment_webhook = os.environ.get(DISCORD_WEBHOOK_ENV)
    if environment_webhook is not None:
        hardware_webhook = os.environ.get(HARDWARE_DISCORD_WEBHOOK_ENV)
        return Config(
            discord_webhook_url=validate_webhook_url(environment_webhook),
            hardware_discord_webhook_url=(
                validate_webhook_url(hardware_webhook) if hardware_webhook else None
            ),
        )

    try:
        raw_bytes = paths.config.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError("Not configured. Run `job-notis configure` first.") from exc

    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
        webhook_url = validate_webhook_url(str(raw["discord"]["webhook_url"]))
        hardware_value = os.environ.get(
            HARDWARE_DISCORD_WEBHOOK_ENV,
            raw["discord"].get("hardware_webhook_url"),
        )
        hardware_webhook = (
            None if hardware_value in (None, "") else validate_webhook_url(str(hardware_value))
        )
        request = raw.get("request", {})
        if not isinstance(request, dict):
            raise TypeError("[request] must be a TOML table")
        timeout_value = request.get("timeout_seconds", 20.0)
        retries_value = request.get("retries", 3)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
            raise TypeError("request.timeout_seconds must be numeric")
        if isinstance(retries_value, bool) or not isinstance(retries_value, int):
            raise TypeError("request.retries must be an integer")
        timeout = float(timeout_value)
        retries = retries_value
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid configuration at {paths.config}.") from exc

    if not 1 <= timeout <= 120:
        raise ConfigError("request.timeout_seconds must be between 1 and 120.")
    if not 0 <= retries <= 10:
        raise ConfigError("request.retries must be between 0 and 10.")

    # This file contains the webhook token. Tighten permissions if an editor changed them.
    os.chmod(paths.config, 0o600)
    return Config(
        discord_webhook_url=webhook_url,
        hardware_discord_webhook_url=hardware_webhook,
        request_timeout_seconds=timeout,
        request_retries=retries,
    )


def save_config(paths: AppPaths, config: Config) -> None:
    """Atomically write configuration with owner-only permissions."""

    paths.ensure_runtime_directories()
    webhook_url = validate_webhook_url(config.discord_webhook_url)
    document = (
        "[discord]\n"
        f"webhook_url = {json.dumps(webhook_url)}\n\n"
        "[request]\n"
        f"timeout_seconds = {config.request_timeout_seconds:g}\n"
        f"retries = {config.request_retries}\n"
    ).encode()
    if config.hardware_discord_webhook_url:
        hardware_value = json.dumps(validate_webhook_url(config.hardware_discord_webhook_url))
        hardware_line = f"hardware_webhook_url = {hardware_value}"
        document = document.replace(
            b"\n\n[request]",
            f"\n{hardware_line}\n\n[request]".encode(),
        )
    temporary = paths.config.with_suffix(".toml.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, paths.config)
        os.chmod(paths.config, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def config_exists(paths: AppPaths) -> bool:
    return DISCORD_WEBHOOK_ENV in os.environ or paths.config.is_file()


def redact_secret(value: str) -> str:
    """Remove Discord webhook tokens from diagnostic strings."""

    return re.sub(
        r"https://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)"
        r"/api(?:/v\d+)?/webhooks/\d+/[^/?#\s]+(?:\?[^\s#]*)?",
        "[REDACTED_DISCORD_WEBHOOK]",
        value,
        flags=re.IGNORECASE,
    )


def assert_private(path: Path) -> None:
    """Raise if a secret-bearing path is accessible by group or other users."""

    if path.stat().st_mode & 0o077:
        raise ConfigError(f"{path} must have permissions 0600.")
