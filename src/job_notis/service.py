"""Application orchestration for baseline, polling, reconciliation, and delivery."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from job_notis.config import Config, redact_secret
from job_notis.database import Database, DatabaseStatus, ReconcileResult, RouteStatus
from job_notis.discord import (
    DiscordDeliveryError,
    DiscordNotifier,
    notification_batches,
)
from job_notis.locking import single_process_lock
from job_notis.models import NotificationRoute
from job_notis.paths import AppPaths
from job_notis.sources import DEFAULT_ADAPTERS, FetchResult, fetch_all

logger = logging.getLogger(__name__)


class InitializationError(RuntimeError):
    """Raised when a safe, complete silent baseline cannot be created."""


@dataclass(frozen=True, slots=True)
class InitializationSummary:
    already_initialized: bool
    source_items: int
    matching_open_jobs: int
    initialized_at: str


@dataclass(frozen=True, slots=True)
class CheckSummary:
    sources_checked: int
    sources_changed: int
    source_failures: tuple[tuple[str, str], ...]
    new_global_jobs: int
    enqueued_notifications: int
    delivered_notifications: int
    pending_notifications: int
    delivery_error: str | None
    route_statuses: tuple[RouteStatus, ...] = ()
    hardware_bootstrap_pending: bool = False


def _errors(results: dict[str, FetchResult]) -> dict[str, str]:
    return {
        name: result.error or "unknown fetch error"
        for name, result in results.items()
        if not result.ok
    }


def _http_cursors(database: Database) -> dict[str, dict[str, str | None]]:
    return {
        source: {
            "etag": cursor.etag,
            "last_modified": cursor.last_modified,
        }
        for source, cursor in database.get_source_cursors().items()
    }


def initialize(config: Config, paths: AppPaths) -> InitializationSummary:
    """Fetch all sources and commit a silent baseline only when all succeed."""

    paths.ensure_runtime_directories()
    with single_process_lock(paths.lock), Database(paths.database) as database:
        if database.is_initialized():
            status = database.status()
            return InitializationSummary(
                already_initialized=True,
                source_items=status.observation_count,
                matching_open_jobs=status.matching_open_job_count,
                initialized_at=status.initialized_at or "",
            )

        results = fetch_all(
            _http_cursors(database),
            timeout=config.request_timeout_seconds,
            retries=config.request_retries,
        )
        failures = _errors(results)
        if failures:
            for source, error in failures.items():
                database.record_source_failure(source, redact_secret(error))
            detail = "; ".join(f"{source}: {error}" for source, error in failures.items())
            raise InitializationError(
                "Silent baseline was not completed because every source must succeed: "
                f"{redact_secret(detail)}"
            )

        reconciled: list[ReconcileResult] = []
        for adapter in DEFAULT_ADAPTERS:
            snapshot = results[adapter.source].snapshot
            assert snapshot is not None
            try:
                reconciled.append(database.reconcile_snapshot(snapshot, baseline=True))
            except Exception as exc:
                error = redact_secret(f"{type(exc).__name__}: {exc}")
                database.record_source_failure(adapter.source, error)
                raise InitializationError(
                    "Silent baseline was not completed because "
                    f"{adapter.source} could not be reconciled: {error}"
                ) from exc
        initialized_at = database.mark_initialized()
        if all(
            results[adapter.source].ok for adapter in DEFAULT_ADAPTERS if adapter.hardware_capable
        ):
            database.complete_hardware_bootstrap(
                cutoff=(datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
            )
        status = database.status()

    logger.info(
        "Silent baseline complete: %d source items, %d open global jobs.",
        sum(result.total_items for result in reconciled),
        status.matching_open_job_count,
    )
    return InitializationSummary(
        already_initialized=False,
        source_items=sum(result.total_items for result in reconciled),
        matching_open_jobs=status.matching_open_job_count,
        initialized_at=initialized_at,
    )


def check(config: Config, paths: AppPaths) -> CheckSummary:
    """Run one complete check and checkpoint each successful Discord batch."""

    paths.ensure_runtime_directories()
    with single_process_lock(paths.lock), Database(paths.database) as database:
        database.require_initialized()
        saved_cursors = database.get_source_cursors()
        results = fetch_all(
            {
                source: {
                    "etag": cursor.etag,
                    "last_modified": cursor.last_modified,
                }
                for source, cursor in saved_cursors.items()
            },
            timeout=config.request_timeout_seconds,
            retries=config.request_retries,
        )
        failures = _errors(results)
        reconciled: list[ReconcileResult] = []
        silently_baselined_sources: set[str] = set()

        for adapter in DEFAULT_ADAPTERS:
            result = results[adapter.source]
            if not result.ok:
                error = redact_secret(result.error or "unknown fetch error")
                database.record_source_failure(adapter.source, error)
                logger.warning("Source %s failed: %s", adapter.source, error)
                continue
            assert result.snapshot is not None
            try:
                cursor = saved_cursors.get(adapter.source)
                is_new_source = cursor is None or cursor.last_success_at is None
                reconciled.append(
                    database.reconcile_snapshot(
                        result.snapshot,
                        baseline=is_new_source,
                    )
                )
                if is_new_source:
                    silently_baselined_sources.add(adapter.source)
                    logger.info(
                        "Silently baselined newly configured source %s.",
                        adapter.source,
                    )
            except Exception as exc:
                error = redact_secret(f"{type(exc).__name__}: {exc}")
                database.record_source_failure(adapter.source, error)
                failures[adapter.source] = error
                logger.exception("Could not reconcile source %s.", adapter.source)

        hardware_sources_ready = all(
            results[adapter.source].ok for adapter in DEFAULT_ADAPTERS if adapter.hardware_capable
        )
        if hardware_sources_ready:
            database.complete_hardware_bootstrap(
                cutoff=(datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
            )

        # Evaluate alert eligibility only after every source has had a chance to
        # update. This preserves a pending alert when open status moves from one
        # source to another during the same poll.
        database.finalize_outbox()

        delivered = 0
        delivery_errors: list[str] = []
        route_webhooks = {
            NotificationRoute.SOFTWARE: config.discord_webhook_url,
            NotificationRoute.HARDWARE: config.hardware_discord_webhook_url,
        }
        for route, webhook in route_webhooks.items():
            pending = database.pending_notifications(route=route)
            if not pending or not webhook:
                continue
            with DiscordNotifier(
                webhook,
                timeout=config.request_timeout_seconds,
                retries=config.request_retries,
            ) as notifier:
                for jobs, embeds in notification_batches(pending):
                    try:
                        if route is NotificationRoute.HARDWARE:
                            receipt = notifier.send_notification_batch(
                                jobs, embeds=embeds, route=route
                            )
                        else:
                            receipt = notifier.send_notification_batch(jobs, embeds=embeds)
                    except DiscordDeliveryError as exc:
                        safe_error = redact_secret(str(exc))
                        delivery_errors.append(
                            safe_error
                            if route is NotificationRoute.SOFTWARE
                            else f"{route.value}: {safe_error}"
                        )
                        for notification in jobs:
                            database.mark_notification_failed(notification.id, safe_error)
                        logger.error("Discord delivery stopped for %s: %s", route.value, safe_error)
                        break
                    else:
                        for notification_id in receipt.notification_ids:
                            database.mark_notification_sent(notification_id)
                            delivered += 1

        status = database.status()

    summary = CheckSummary(
        sources_checked=len(results),
        sources_changed=sum(not result.not_modified for result in reconciled),
        source_failures=tuple(sorted(failures.items())),
        new_global_jobs=sum(
            result.new_global_jobs
            for result in reconciled
            if result.source not in silently_baselined_sources
        ),
        enqueued_notifications=sum(result.enqueued_notifications for result in reconciled),
        delivered_notifications=delivered,
        pending_notifications=status.pending_notification_count,
        delivery_error="; ".join(delivery_errors) if delivery_errors else None,
        route_statuses=status.route_statuses,
        hardware_bootstrap_pending=status.hardware_bootstrap_pending,
    )
    logger.info(
        "Check complete: %d changed source(s), %d new global job(s), %d delivered, %d pending.",
        summary.sources_changed,
        summary.new_global_jobs,
        summary.delivered_notifications,
        summary.pending_notifications,
    )
    return summary


def database_status(paths: AppPaths) -> DatabaseStatus:
    paths.ensure_runtime_directories()
    with Database(paths.database) as database:
        return database.status()
