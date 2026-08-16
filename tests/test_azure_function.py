from __future__ import annotations

import logging
from dataclasses import replace

from job_notis.azure_function import run_timer_check
from job_notis.azure_state import StateLeaseUnavailable
from job_notis.config import Config
from job_notis.database import DatabaseStatus
from job_notis.service import CheckSummary


def check_summary() -> CheckSummary:
    return CheckSummary(
        sources_checked=4,
        sources_changed=1,
        source_failures=(),
        new_global_jobs=1,
        enqueued_notifications=1,
        delivered_notifications=1,
        pending_notifications=0,
        delivery_error=None,
    )


def database_status() -> DatabaseStatus:
    return DatabaseStatus(
        initialized=True,
        initialized_at="2026-08-01T05:33:16+00:00",
        source_count=4,
        observation_count=15210,
        global_job_count=14805,
        open_job_count=1555,
        matching_open_job_count=393,
        pending_notification_count=0,
        sent_notification_count=1,
        cancelled_notification_count=0,
        source_cursors=(),
    )


class FakeStore:
    def __init__(self, *, leased: bool = False) -> None:
        self.leased = leased

    def run_check(self, config: Config):
        assert config.discord_webhook_url.endswith("/token")
        if self.leased:
            raise StateLeaseUnavailable("busy")
        return check_summary(), database_status()


def test_timer_dispatch_returns_summary(caplog) -> None:
    caplog.set_level(logging.INFO)
    result = run_timer_check(
        config_loader=lambda _paths: Config("https://discord.com/api/webhooks/1/token"),
        store_factory=FakeStore,
    )

    assert result == check_summary()
    assert "15210 observations persisted" in caplog.text


def test_timer_skips_lease_contention(caplog) -> None:
    result = run_timer_check(
        config_loader=lambda _paths: Config("https://discord.com/api/webhooks/1/token"),
        store_factory=lambda: FakeStore(leased=True),
    )

    assert result is None
    assert "another invocation" in caplog.text


def test_timer_logs_redacted_controlled_failures(caplog) -> None:
    secret = "https://discord.com/api/webhooks/123/super-secret"

    class FailingStore(FakeStore):
        def run_check(self, config: Config):
            result = replace(
                check_summary(),
                source_failures=(("source", secret),),
                delivery_error=secret,
            )
            return result, database_status()

    run_timer_check(
        config_loader=lambda _paths: Config("https://discord.com/api/webhooks/1/token"),
        store_factory=FailingStore,
    )

    assert "super-secret" not in caplog.text
    assert "REDACTED_DISCORD_WEBHOOK" in caplog.text
