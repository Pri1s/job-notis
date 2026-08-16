from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from job_notis.config import Config
from job_notis.database import Database, DatabaseError
from job_notis.discord import DeliveryReceipt, TransientDiscordError
from job_notis.models import SourceItem, SourceSnapshot, Sponsorship
from job_notis.paths import AppPaths
from job_notis.service import InitializationError, check, initialize
from job_notis.sources import DEFAULT_ADAPTERS, FetchResult


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


def item(
    source: str,
    source_id: str,
    *,
    job_id: int,
    title: str = "Software Engineer Intern",
) -> SourceItem:
    return SourceItem(
        source=source,
        source_id=source_id,
        company="Example",
        title=title,
        locations=("Chicago, IL",),
        apply_url=f"https://job-boards.greenhouse.io/example/jobs/{job_id}",
        season="Summer 2027",
        sponsorship=Sponsorship.UNKNOWN,
        active=True,
    )


def snapshot(source: str, items: tuple[SourceItem, ...], *, etag: str = '"one"') -> SourceSnapshot:
    return SourceSnapshot(
        source=source,
        items=items,
        etag=etag,
        last_modified="Fri, 31 Jul 2026 00:00:00 GMT",
    )


def baseline_results() -> dict[str, FetchResult]:
    return {
        adapter.source: FetchResult(
            source=adapter.source,
            snapshot=snapshot(
                adapter.source,
                (item(adapter.source, f"{adapter.source}-baseline", job_id=100),),
            ),
        )
        for adapter in DEFAULT_ADAPTERS
    }


class FakeNotifier:
    sent_batches: list[tuple[int, ...]] = []
    error: Exception | None = None

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def __enter__(self) -> FakeNotifier:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def send_notification_batch(
        self, jobs: tuple[Any, ...], *, embeds: tuple[dict[str, Any], ...]
    ) -> DeliveryReceipt:
        if self.error:
            raise self.error
        identifiers = tuple(job.id for job in jobs)
        self.sent_batches.append(identifiers)
        return DeliveryReceipt(identifiers, "discord-id")


@pytest.fixture(autouse=True)
def reset_notifier() -> None:
    FakeNotifier.sent_batches = []
    FakeNotifier.error = None


def test_initialize_silently_baselines_every_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: baseline_results())
    paths = paths_for(tmp_path)

    summary = initialize(Config("https://discord.com/api/webhooks/1/token"), paths)

    assert summary.already_initialized is False
    assert summary.source_items == len(DEFAULT_ADAPTERS)
    with Database(paths.database) as database:
        status = database.status()
        assert status.initialized is True
        assert status.observation_count == len(DEFAULT_ADAPTERS)
        assert status.global_job_count == 1
        assert status.matching_open_job_count == 1
        assert status.pending_notification_count == 0


def test_initialize_requires_all_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    results = baseline_results()
    failed_source = DEFAULT_ADAPTERS[-1].source
    results[failed_source] = FetchResult(source=failed_source, error="upstream unavailable")
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: results)
    paths = paths_for(tmp_path)

    with pytest.raises(InitializationError, match="every source"):
        initialize(Config("https://discord.com/api/webhooks/1/token"), paths)

    with Database(paths.database) as database:
        assert database.is_initialized() is False
        assert database.status().observation_count == 0


def test_initialize_never_marks_partial_reconciliation_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: baseline_results())
    original = Database.reconcile_snapshot
    failed_source = DEFAULT_ADAPTERS[1].source

    def reconcile(
        database: Database,
        source_snapshot: SourceSnapshot,
        **kwargs: Any,
    ):
        if source_snapshot.source == failed_source:
            raise DatabaseError("simulated reconciliation failure")
        return original(database, source_snapshot, **kwargs)

    monkeypatch.setattr(Database, "reconcile_snapshot", reconcile)
    paths = paths_for(tmp_path)

    with pytest.raises(InitializationError, match="could not be reconciled"):
        initialize(Config("https://discord.com/api/webhooks/1/token"), paths)

    with Database(paths.database) as database:
        assert database.is_initialized() is False
        cursor = database.get_source_cursor(failed_source)
        assert cursor is not None
        assert "simulated reconciliation failure" in (cursor.last_error or "")


def test_check_alerts_once_for_a_new_cross_source_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = paths_for(tmp_path)
    config = Config("https://discord.com/api/webhooks/1/token")
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: baseline_results())
    initialize(config, paths)

    vansh_source = DEFAULT_ADAPTERS[0].source
    changed = {
        adapter.source: FetchResult(
            source=adapter.source,
            snapshot=(
                snapshot(
                    adapter.source,
                    (
                        item(adapter.source, f"{adapter.source}-baseline", job_id=100),
                        item(adapter.source, "new-job", job_id=200),
                    ),
                    etag='"two"',
                )
                if adapter.source == vansh_source
                else SourceSnapshot(
                    source=adapter.source,
                    items=(),
                    etag='"one"',
                    last_modified="Fri, 31 Jul 2026 00:00:00 GMT",
                    not_modified=True,
                )
            ),
        )
        for adapter in DEFAULT_ADAPTERS
    }
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: changed)
    monkeypatch.setattr("job_notis.service.DiscordNotifier", FakeNotifier)

    first = check(config, paths)
    second = check(config, paths)

    assert first.enqueued_notifications == 1
    assert first.delivered_notifications == 1
    assert first.pending_notifications == 0
    assert second.enqueued_notifications == 0
    assert second.delivered_notifications == 0
    assert len(FakeNotifier.sent_batches) == 1


def test_check_silently_baselines_a_source_added_after_initialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    config = Config("https://discord.com/api/webhooks/1/token")
    new_adapter = DEFAULT_ADAPTERS[-1]
    with Database(paths.database) as database:
        for adapter in DEFAULT_ADAPTERS[:-1]:
            database.reconcile_snapshot(
                snapshot(
                    adapter.source,
                    (item(adapter.source, f"{adapter.source}-baseline", job_id=100),),
                ),
                baseline=True,
            )
        database.mark_initialized()

    results = {
        adapter.source: FetchResult(
            source=adapter.source,
            snapshot=(
                snapshot(
                    new_adapter.source,
                    (item(new_adapter.source, "existing-at-addition", job_id=500),),
                )
                if adapter is new_adapter
                else SourceSnapshot(
                    source=adapter.source,
                    items=(),
                    etag='"one"',
                    last_modified="Fri, 31 Jul 2026 00:00:00 GMT",
                    not_modified=True,
                )
            ),
        )
        for adapter in DEFAULT_ADAPTERS
    }
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: results)
    monkeypatch.setattr("job_notis.service.DiscordNotifier", FakeNotifier)

    summary = check(config, paths)

    assert summary.new_global_jobs == 0
    assert summary.enqueued_notifications == 0
    assert summary.delivered_notifications == 0
    assert summary.pending_notifications == 0
    assert FakeNotifier.sent_batches == []
    with Database(paths.database) as database:
        cursor = database.get_source_cursor(new_adapter.source)
        assert cursor is not None
        assert cursor.last_success_at is not None
        assert cursor.item_count == 1


def test_recovered_source_catches_up_on_still_open_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    config = Config("https://discord.com/api/webhooks/1/token")
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: baseline_results())
    initialize(config, paths)
    source = DEFAULT_ADAPTERS[0].source
    failed = baseline_results()
    failed[source] = FetchResult(source=source, error="offline")
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: failed)
    monkeypatch.setattr("job_notis.service.DiscordNotifier", FakeNotifier)

    missed = check(config, paths)

    assert missed.source_failures == ((source, "offline"),)
    assert missed.delivered_notifications == 0

    recovered = baseline_results()
    recovered[source] = FetchResult(
        source=source,
        snapshot=snapshot(
            source,
            (
                item(source, f"{source}-baseline", job_id=100),
                item(source, "new-after-wake", job_id=400),
            ),
            etag='"two"',
        ),
    )
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: recovered)

    catch_up = check(config, paths)
    repeated = check(config, paths)

    assert catch_up.enqueued_notifications == 1
    assert catch_up.delivered_notifications == 1
    assert repeated.enqueued_notifications == 0
    assert repeated.delivered_notifications == 0
    assert len(FakeNotifier.sent_batches) == 1


def test_delivery_failure_leaves_durable_pending_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = paths_for(tmp_path)
    config = Config("https://discord.com/api/webhooks/1/token", request_retries=0)
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: baseline_results())
    initialize(config, paths)

    source = DEFAULT_ADAPTERS[0].source
    results = baseline_results()
    results[source] = FetchResult(
        source=source,
        snapshot=snapshot(
            source,
            (
                item(source, f"{source}-baseline", job_id=100),
                item(source, "new-job", job_id=300),
            ),
            etag='"two"',
        ),
    )
    monkeypatch.setattr("job_notis.service.fetch_all", lambda *_args, **_kwargs: results)
    FakeNotifier.error = TransientDiscordError("temporary outage")
    monkeypatch.setattr("job_notis.service.DiscordNotifier", FakeNotifier)

    summary = check(config, paths)

    assert summary.delivered_notifications == 0
    assert summary.pending_notifications == 1
    assert summary.delivery_error == "temporary outage"
    with Database(paths.database) as database:
        pending = database.pending_notifications()
        assert len(pending) == 1
        assert pending[0].attempts == 1

    FakeNotifier.error = None
    recovered = check(config, paths)

    assert recovered.delivered_notifications == 1
    assert recovered.pending_notifications == 0
