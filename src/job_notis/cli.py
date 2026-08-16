"""Command-line interface."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from job_notis.config import (
    Config,
    ConfigError,
    config_exists,
    load_config,
    redact_secret,
    save_config,
    validate_webhook_url,
)
from job_notis.database import DatabaseError, NotInitializedError
from job_notis.discord import DiscordDeliveryError, DiscordNotifier
from job_notis.launchd import (
    LaunchdError,
    install_launch_agent,
    launch_agent_status,
    uninstall_launch_agent,
)
from job_notis.locking import AlreadyRunningError
from job_notis.logging_setup import configure_logging
from job_notis.models import NotificationRoute
from job_notis.paths import AppPaths, default_paths
from job_notis.service import InitializationError, check, database_status, initialize
from job_notis.sources import DEFAULT_ADAPTERS

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-notis",
        description="Notify Discord about newly listed SWE internships.",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("configure", help="save the Discord webhook securely")
    subparsers.add_parser("init", help="silently baseline current internships")

    check_parser = subparsers.add_parser("check", help="check sources and deliver alerts")
    check_parser.add_argument(
        "--scheduled",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    subparsers.add_parser("status", help="show source, state, and scheduler health")
    test_parser = subparsers.add_parser("test-notification", help="send a Discord connection test")
    test_parser.add_argument(
        "--channel",
        choices=("software", "hardware", "all"),
        default="all",
        help="channel(s) to test",
    )
    subparsers.add_parser("install", help="install the per-user macOS LaunchAgent")
    subparsers.add_parser(
        "uninstall",
        help="unload the LaunchAgent while preserving configuration and state",
    )
    return parser


def _configure(paths: AppPaths) -> int:
    print("Paste the software Discord incoming-webhook URL. Input is hidden.")
    webhook_url = getpass.getpass("Webhook URL: ")
    hardware_url = getpass.getpass("Hardware webhook URL (optional; press Enter to skip): ").strip()
    config = Config(
        discord_webhook_url=validate_webhook_url(webhook_url),
        hardware_discord_webhook_url=(validate_webhook_url(hardware_url) if hardware_url else None),
    )
    save_config(paths, config)
    print(f"Configuration saved securely at {paths.config}")
    return 0


def _initialize(paths: AppPaths) -> int:
    config = load_config(paths)
    summary = initialize(config, paths)
    if summary.already_initialized:
        print(
            "Already initialized "
            f"({summary.source_items} observations; initialized {summary.initialized_at})."
        )
    else:
        print(
            "Silent baseline complete: "
            f"{summary.source_items} source observations recorded. No job alerts were sent."
        )
    return 0


def _check(paths: AppPaths, *, scheduled: bool) -> int:
    config = load_config(paths)
    try:
        summary = check(config, paths)
    except AlreadyRunningError:
        if not scheduled:
            print("Another check is already running.")
        return 0
    if not scheduled:
        print(
            f"Checked {summary.sources_checked} sources; "
            f"{summary.new_global_jobs} new jobs, "
            f"{summary.delivered_notifications} alerts delivered, "
            f"{summary.pending_notifications} pending."
        )
        for source, error in summary.source_failures:
            print(f"Warning: {source}: {error}", file=sys.stderr)
        if summary.delivery_error:
            print(f"Warning: Discord: {summary.delivery_error}", file=sys.stderr)
    return 1 if summary.source_failures or summary.delivery_error else 0


def _format_path(path: Path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


def _status(paths: AppPaths) -> int:
    status = database_status(paths)
    scheduler_ok, scheduler_state = launch_agent_status(paths)
    config_ok = False
    config_state = "no"
    if config_exists(paths):
        try:
            load_config(paths)
        except ConfigError as exc:
            config_state = f"invalid ({exc})"
        else:
            config_ok = True
            config_state = "yes"
    print(f"Configured: {config_state}")
    print(
        f"Initialized: {'yes' if status.initialized else 'no'}"
        + (f" ({status.initialized_at})" if status.initialized_at else "")
    )
    print(f"Scheduler: {scheduler_state}")
    print(
        f"Jobs: {status.global_job_count} global / "
        f"{status.open_job_count} open / "
        f"{status.matching_open_job_count} matching / "
        f"{status.observation_count} observations"
    )
    print(
        f"Outbox: {status.pending_notification_count} pending / "
        f"{status.sent_notification_count} sent / "
        f"{status.cancelled_notification_count} cancelled"
    )
    for route_status in status.route_statuses:
        print(
            f"  {route_status.route.value}: {route_status.pending} pending / "
            f"{route_status.sent} delivered / {route_status.failed} failed"
        )
    print("Hardware bootstrap: " + ("pending" if status.hardware_bootstrap_pending else "complete"))
    if status.source_cursors:
        print("Sources:")
        for cursor in status.source_cursors:
            health = f"error: {cursor.last_error}" if cursor.last_error else "healthy"
            print(
                f"  {cursor.source}: {cursor.item_count} items, {health}; "
                f"last success {cursor.last_success_at or 'never'}"
            )
    print(f"Database: {_format_path(paths.database)}")
    print(f"Log: {_format_path(paths.log_file)}")
    expected_sources = {adapter.source for adapter in DEFAULT_ADAPTERS}
    current_sources = {cursor.source for cursor in status.source_cursors}
    sources_ok = current_sources == expected_sources and all(
        cursor.last_error is None for cursor in status.source_cursors
    )
    return 0 if config_ok and status.initialized and scheduler_ok and sources_ok else 1


def _test_notification(paths: AppPaths, channel: str) -> int:
    config = load_config(paths)
    routes = (
        (NotificationRoute.SOFTWARE, config.discord_webhook_url),
        (NotificationRoute.HARDWARE, config.hardware_discord_webhook_url),
    )
    for route, webhook in routes:
        if channel not in {route.value, "all"}:
            continue
        if not webhook:
            raise ConfigError(f"No {route.value} Discord webhook is configured.")
        with DiscordNotifier(
            webhook,
            timeout=config.request_timeout_seconds,
            retries=config.request_retries,
        ) as notifier:
            notifier.send_test(route=route)
    print("Test notification delivered.")
    return 0


def _install(paths: AppPaths) -> int:
    load_config(paths)
    status = database_status(paths)
    if not status.initialized:
        raise NotInitializedError("Run `job-notis init` before installing the scheduler.")
    installed = install_launch_agent(paths)
    print(f"LaunchAgent installed at {installed}")
    print("Job Notis will check at minutes 7, 22, 37, and 52 of every hour.")
    return 0


def _uninstall(paths: AppPaths) -> int:
    existed = uninstall_launch_agent(paths)
    if existed:
        print("LaunchAgent removed. Configuration and SQLite state were preserved.")
    else:
        print("LaunchAgent was not installed. Configuration and state were unchanged.")
    return 0


def run(arguments: Sequence[str] | None = None, *, paths: AppPaths | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(arguments)
    active_paths = paths or default_paths()
    scheduled = namespace.command == "check" and bool(namespace.scheduled)
    configure_logging(
        active_paths,
        console=not scheduled,
        verbose=bool(namespace.verbose),
    )

    try:
        if namespace.command == "configure":
            return _configure(active_paths)
        if namespace.command == "init":
            return _initialize(active_paths)
        if namespace.command == "check":
            return _check(active_paths, scheduled=scheduled)
        if namespace.command == "status":
            return _status(active_paths)
        if namespace.command == "test-notification":
            return _test_notification(active_paths, namespace.channel)
        if namespace.command == "install":
            return _install(active_paths)
        if namespace.command == "uninstall":
            return _uninstall(active_paths)
    except (
        ConfigError,
        DatabaseError,
        DiscordDeliveryError,
        InitializationError,
        LaunchdError,
    ) as exc:
        logger.error("%s", exc)
        if scheduled:
            return 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("Unexpected Job Notis failure.")
        if not scheduled:
            detail = redact_secret(f"{type(exc).__name__}: {exc}")
            print(f"Unexpected error: {detail}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {namespace.command}")
    return 2


def main() -> int:
    return run()
