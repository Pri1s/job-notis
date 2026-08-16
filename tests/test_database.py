from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from job_notis.database import (
    Database,
    DatabaseError,
    InvalidSnapshotError,
    NotInitializedError,
)
from job_notis.models import SourceItem, SourceSnapshot, Sponsorship


def _item(
    source: str = "source-a",
    source_id: str = "one",
    *,
    apply_url: str = "https://example.com/jobs/one",
    active: bool = True,
    visible: bool = True,
    title: str = "Software Engineering Intern",
    locations: tuple[str, ...] = ("Chicago, IL",),
    season: str = "Summer 2027",
    first_seen_at: str | None = None,
) -> SourceItem:
    return SourceItem(
        source=source,
        source_id=source_id,
        company="Example",
        title=title,
        locations=locations,
        apply_url=apply_url,
        season=season,
        sponsorship=Sponsorship.UNKNOWN,
        active=active,
        visible=visible,
        posted_at="2027-01-02",
        first_seen_at=first_seen_at,
        category="Software Engineering",
        raw={"source_id": source_id},
    )


def _snapshot(
    source: str,
    *items: SourceItem,
    etag: str | None = '"v1"',
    last_modified: str | None = "Wed, 03 Jan 2027 00:00:00 GMT",
    not_modified: bool = False,
) -> SourceSnapshot:
    return SourceSnapshot(
        source=source,
        items=tuple(items),
        etag=etag,
        last_modified=last_modified,
        not_modified=not_modified,
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "state.sqlite3")
    yield db
    db.close()


def test_migration_creates_current_schema_and_private_database(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.sqlite3"
    with Database(path):
        pass

    connection = sqlite3.connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()

    assert version == 2
    assert {
        "metadata",
        "source_cursors",
        "global_jobs",
        "job_aliases",
        "source_observations",
        "notification_outbox",
    } <= tables
    assert path.stat().st_mode & 0o777 == 0o600


def test_newer_database_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(DatabaseError, match="newer than supported"):
        Database(path)


def test_normal_reconciliation_requires_completed_baseline(database: Database) -> None:
    snapshot = _snapshot("source-a", _item())

    with pytest.raises(NotInitializedError, match="job-notis init"):
        database.reconcile_snapshot(snapshot, observed_at="2027-01-04T00:00:00+00:00")

    assert database.status().source_count == 0


def test_baseline_is_silent_then_new_matching_job_is_durable(
    database: Database,
) -> None:
    original = _item()
    baseline = database.reconcile_snapshot(
        _snapshot("source-a", original),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )

    assert baseline.new_global_jobs == 1
    assert baseline.enqueued_notifications == 0
    assert database.pending_notifications() == ()
    assert not database.is_initialized()

    assert database.mark_initialized(at="2027-01-04T00:01:00+00:00")
    new_item = _item(
        source_id="two",
        apply_url="https://example.com/jobs/two?utm_source=github",
    )
    result = database.reconcile_snapshot(
        _snapshot("source-a", original, new_item, etag='"v2"'),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.new_global_jobs == 1
    assert result.enqueued_notifications == 1
    pending = database.pending_notifications()
    assert len(pending) == 1
    assert pending[0].company == "Example"
    assert pending[0].title == "Software Engineering Intern"
    assert pending[0].locations == ("Chicago, IL",)
    assert pending[0].apply_url == "https://example.com/jobs/two?utm_source=github"
    assert pending[0].sources == ("source-a",)
    assert pending[0].attempts == 0

    repeated = database.reconcile_snapshot(
        _snapshot("source-a", original, new_item, etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )
    assert repeated.new_global_jobs == 0
    assert repeated.enqueued_notifications == 0
    assert database.pending_notifications() == pending


def test_filtered_and_inactive_observations_are_retained_without_alerts(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    canada = _item(
        source_id="canada",
        apply_url="https://example.com/jobs/canada",
        locations=("Toronto, Canada",),
    )
    closed = _item(
        source_id="closed",
        apply_url="https://example.com/jobs/closed",
        active=False,
    )
    unrelated = _item(
        source_id="design",
        apply_url="https://example.com/jobs/design",
        title="Product Design Intern",
    )

    result = database.reconcile_snapshot(
        _snapshot("source-a", canada, closed, unrelated),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.new_global_jobs == 3
    assert result.enqueued_notifications == 0
    status = database.status()
    assert status.observation_count == 3
    assert status.global_job_count == 3
    assert status.open_job_count == 2
    assert status.pending_notification_count == 0


def test_upstream_backfill_predating_initialization_is_silently_recorded(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    backfill = _item(
        source="zshah101",
        source_id="stable-old-id",
        apply_url="https://example.com/jobs/backfill",
        first_seen_at="2027-01-03T23:59:59Z",
    )

    result = database.reconcile_snapshot(
        _snapshot("zshah101", backfill),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.new_global_jobs == 1
    assert result.enqueued_notifications == 0
    assert database.status().observation_count == 1
    assert database.pending_notifications() == ()


def test_upstream_first_seen_after_initialization_is_alerted(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    new = _item(
        source="zshah101",
        source_id="stable-new-id",
        apply_url="https://example.com/jobs/new",
        first_seen_at="2027-01-04T00:00:01Z",
    )

    result = database.reconcile_snapshot(
        _snapshot("zshah101", new),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.enqueued_notifications == 1
    assert len(database.pending_notifications()) == 1


def test_cross_source_ats_identity_enqueues_once_and_aggregates_sources(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    hosted = _item(
        source="source-a",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345?gh_src=repo",
    )
    custom = replace(
        hosted,
        source="source-b",
        source_id="other",
        apply_url="https://careers.acme.com/opening?gh_jid=12345&utm_source=repo",
    )

    first = database.reconcile_snapshot(
        _snapshot("source-a", hosted),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    second = database.reconcile_snapshot(
        _snapshot("source-b", custom),
        observed_at="2027-01-04T00:16:00+00:00",
    )

    assert first.enqueued_notifications == 1
    assert second.new_global_jobs == 0
    assert second.enqueued_notifications == 0
    assert database.status().global_job_count == 1
    assert database.status().observation_count == 2
    assert database.pending_notifications()[0].sources == ("source-a", "source-b")


def test_slower_duplicate_of_baseline_job_never_alerts(database: Database) -> None:
    hosted = _item(
        source="source-a",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345?gh_src=repo",
    )
    custom = replace(
        hosted,
        source="source-b",
        source_id="other",
        apply_url="https://careers.acme.com/opening?gh_jid=12345",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", hosted),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )
    database.mark_initialized(at="2027-01-04T00:01:00+00:00")

    result = database.reconcile_snapshot(
        _snapshot("source-b", custom),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.new_global_jobs == 0
    assert result.enqueued_notifications == 0
    assert database.pending_notifications() == ()


def test_slower_source_can_make_never_alerted_post_baseline_job_eligible(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    canada_only = _item(
        source="source-a",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
        locations=("Toronto, Canada",),
    )
    us_copy = replace(
        canada_only,
        source="source-b",
        source_id="other",
        apply_url="https://careers.acme.com/opening?gh_jid=12345",
        locations=("Chicago, IL",),
    )

    first = database.reconcile_snapshot(
        _snapshot("source-a", canada_only),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    second = database.reconcile_snapshot(
        _snapshot("source-b", us_copy),
        observed_at="2027-01-04T00:16:00+00:00",
    )

    assert first.new_global_jobs == 1
    assert first.enqueued_notifications == 0
    assert second.new_global_jobs == 0
    assert second.enqueued_notifications == 1
    pending = database.pending_notifications()
    assert len(pending) == 1
    assert pending[0].locations == ("Chicago, IL",)


def test_pending_requires_an_eligible_open_source_not_just_any_open_source(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    eligible = _item(
        source="source-a",
        apply_url="https://jobs.lever.co/acme/shared-requisition",
    )
    canada_only = replace(
        eligible,
        source="source-b",
        source_id="other",
        locations=("Toronto, Canada",),
    )
    database.reconcile_snapshot(
        _snapshot("source-a", eligible),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    database.reconcile_snapshot(
        _snapshot("source-b", canada_only),
        observed_at="2027-01-04T00:16:00+00:00",
    )

    database.reconcile_snapshot(
        _snapshot("source-a", replace(eligible, active=False), etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )
    database.finalize_outbox(at="2027-01-04T00:31:00+00:00")

    assert database.status().open_job_count == 1
    assert database.status().cancelled_notification_count == 1
    assert database.pending_notifications() == ()


def test_conflicting_source_status_stays_open_until_every_source_closes(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    source_a = _item(
        source="source-a",
        apply_url="https://jobs.lever.co/acme/shared-requisition",
    )
    source_b = replace(source_a, source="source-b", source_id="shared")
    database.reconcile_snapshot(
        _snapshot("source-a", source_a),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    database.reconcile_snapshot(
        _snapshot("source-b", source_b),
        observed_at="2027-01-04T00:16:00+00:00",
    )

    database.reconcile_snapshot(
        _snapshot("source-a", replace(source_a, active=False), etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )
    database.finalize_outbox(at="2027-01-04T00:30:30+00:00")
    assert database.status().open_job_count == 1
    assert len(database.pending_notifications()) == 1

    database.reconcile_snapshot(
        _snapshot("source-b", replace(source_b, active=False), etag='"v2"'),
        observed_at="2027-01-04T00:31:00+00:00",
    )
    database.finalize_outbox(at="2027-01-04T00:31:30+00:00")
    assert database.status().open_job_count == 0
    assert database.status().cancelled_notification_count == 1
    assert database.pending_notifications() == ()

    database.reconcile_snapshot(
        _snapshot("source-b", source_b, etag='"v3"'),
        observed_at="2027-01-04T00:46:00+00:00",
    )
    assert database.status().open_job_count == 1
    assert database.pending_notifications() == ()


def test_cross_source_status_handoff_does_not_cancel_pending_alert(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    source_a = _item(
        source="source-a",
        apply_url="https://jobs.lever.co/acme/shared-requisition",
    )
    source_b = replace(
        source_a,
        source="source-b",
        source_id="shared",
        active=False,
    )
    database.reconcile_snapshot(
        _snapshot("source-a", source_a),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    database.reconcile_snapshot(
        _snapshot("source-b", source_b),
        observed_at="2027-01-04T00:16:00+00:00",
    )

    database.reconcile_snapshot(
        _snapshot("source-a", replace(source_a, active=False), etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )
    database.reconcile_snapshot(
        _snapshot("source-b", replace(source_b, active=True), etag='"v2"'),
        observed_at="2027-01-04T00:31:00+00:00",
    )
    database.finalize_outbox(at="2027-01-04T00:32:00+00:00")

    assert database.status().open_job_count == 1
    assert database.status().cancelled_notification_count == 0
    assert len(database.pending_notifications()) == 1


def test_same_source_new_id_for_known_global_job_is_not_a_new_alert(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    filtered = _item(
        source_id="old-id",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
        locations=("Toronto, Canada",),
    )
    database.reconcile_snapshot(
        _snapshot("source-a", filtered),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    edited = replace(
        filtered,
        source_id="new-id",
        locations=("Chicago, IL",),
    )

    result = database.reconcile_snapshot(
        _snapshot("source-a", edited, etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )

    assert result.new_global_jobs == 0
    assert result.enqueued_notifications == 0
    assert database.pending_notifications() == ()


def test_exact_one_in_one_out_semantic_match_treats_url_change_as_alias(
    database: Database,
) -> None:
    old = _item(
        source_id="old-id",
        apply_url="https://old.example.com/jobs/software-intern",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", old),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )
    database.mark_initialized(at="2027-01-04T00:01:00+00:00")
    rewritten = replace(
        old,
        source_id="new-id",
        apply_url="https://new.example.com/openings/987",
    )

    result = database.reconcile_snapshot(
        _snapshot("source-a", rewritten, etag='"v2"'),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.new_global_jobs == 0
    assert result.missing_observations == 1
    assert database.status().global_job_count == 1
    assert database.status().observation_count == 2
    assert database.pending_notifications() == ()


def test_distinct_ats_requisition_is_not_suppressed_as_a_url_edit(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    old = _item(
        source_id="old",
        apply_url="https://jobs.lever.co/acme/requisition-old",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", old),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    old_notification = database.pending_notifications()[0]
    database.mark_notification_sent(
        old_notification.id,
        sent_at="2027-01-04T00:16:00+00:00",
    )
    new = replace(
        old,
        source_id="new",
        apply_url="https://jobs.lever.co/acme/requisition-new",
    )

    result = database.reconcile_snapshot(
        _snapshot("source-a", new, etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )

    assert result.new_global_jobs == 1
    assert result.enqueued_notifications == 1
    assert database.status().global_job_count == 2
    assert len(database.pending_notifications()) == 1


def test_removed_then_returned_url_rewrite_uses_historical_observation(
    database: Database,
) -> None:
    old = _item(
        source_id="old-id",
        apply_url="https://old.example.com/jobs/software-intern",
    )
    keeper = _item(
        source_id="keeper",
        apply_url="https://example.com/jobs/keeper",
        title="Backend Engineering Intern",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", old, keeper),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )
    database.mark_initialized(at="2027-01-04T00:01:00+00:00")
    database.reconcile_snapshot(
        _snapshot("source-a", keeper, etag='"v2"'),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    rewritten = replace(
        old,
        source_id="new-id",
        apply_url="https://new.example.com/openings/987",
    )

    result = database.reconcile_snapshot(
        _snapshot("source-a", keeper, rewritten, etag='"v3"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )

    assert result.new_global_jobs == 0
    assert result.enqueued_notifications == 0
    assert database.status().global_job_count == 2
    assert database.pending_notifications() == ()


def test_cancelled_alert_is_not_revived_by_a_slower_source(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    source_a = _item(
        source="source-a",
        apply_url="https://jobs.lever.co/acme/shared-requisition",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", source_a),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", replace(source_a, active=False), etag='"v2"'),
        observed_at="2027-01-04T00:30:00+00:00",
    )
    database.finalize_outbox(at="2027-01-04T00:31:00+00:00")
    slower = replace(source_a, source="source-b", source_id="slower")

    result = database.reconcile_snapshot(
        _snapshot("source-b", slower),
        observed_at="2027-01-04T00:45:00+00:00",
    )

    assert result.new_global_jobs == 0
    assert result.enqueued_notifications == 0
    assert database.status().cancelled_notification_count == 1
    assert database.pending_notifications() == ()


def test_removed_observation_is_retained_but_no_longer_open(database: Database) -> None:
    first = _item()
    second = _item(
        source_id="two",
        apply_url="https://example.com/jobs/two",
    )
    database.reconcile_snapshot(
        _snapshot("source-a", first, second),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )
    database.mark_initialized(at="2027-01-04T00:01:00+00:00")

    result = database.reconcile_snapshot(
        _snapshot("source-a", first, etag='"v2"'),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    assert result.missing_observations == 1
    status = database.status()
    assert status.observation_count == 2
    assert status.global_job_count == 2
    assert status.open_job_count == 1


def test_delivery_failure_retries_then_sent_is_idempotent(database: Database) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    database.reconcile_snapshot(
        _snapshot("source-a", _item()),
        observed_at="2027-01-04T00:15:00+00:00",
    )
    notification_id = database.pending_notifications()[0].id

    assert database.mark_notification_failed(
        notification_id,
        "Discord 500",
        attempted_at="2027-01-04T00:16:00+00:00",
    )
    assert database.pending_notifications()[0].attempts == 1
    assert database.mark_notification_sent(
        notification_id,
        sent_at="2027-01-04T00:17:00+00:00",
    )
    assert not database.mark_notification_sent(
        notification_id,
        sent_at="2027-01-04T00:18:00+00:00",
    )
    assert database.pending_notifications() == ()
    assert database.status().sent_notification_count == 1


def test_pending_notifications_are_grouped_in_ten_embed_batches(
    database: Database,
) -> None:
    database.mark_initialized(at="2027-01-04T00:00:00+00:00")
    items = tuple(
        _item(
            source_id=str(index),
            apply_url=f"https://example.com/jobs/{index}",
        )
        for index in range(11)
    )
    database.reconcile_snapshot(
        _snapshot("source-a", *items),
        observed_at="2027-01-04T00:15:00+00:00",
    )

    batches = database.pending_notification_batches()
    assert tuple(len(batch) for batch in batches) == (10, 1)
    with pytest.raises(ValueError, match="between 1 and 10"):
        database.pending_notification_batches(batch_size=11)


def test_conditional_cursor_and_failure_preserve_last_good_snapshot(
    database: Database,
) -> None:
    database.reconcile_snapshot(
        _snapshot("source-a", _item(), etag='"first"'),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )
    database.record_source_failure(
        "source-a",
        "temporary network error",
        checked_at="2027-01-04T00:05:00+00:00",
    )
    failed = database.get_source_cursor("source-a")
    assert failed is not None
    assert failed.etag == '"first"'
    assert failed.item_count == 1
    assert failed.last_success_at == "2027-01-04T00:00:00+00:00"
    assert failed.last_error == "temporary network error"

    result = database.reconcile_snapshot(
        _snapshot(
            "source-a",
            etag=None,
            last_modified=None,
            not_modified=True,
        ),
        baseline=True,
        observed_at="2027-01-04T00:15:00+00:00",
    )
    current = database.get_source_cursor("source-a")
    assert result.not_modified
    assert result.total_items == 1
    assert current is not None
    assert current.etag == '"first"'
    assert current.last_error is None
    assert current.last_success_at == "2027-01-04T00:15:00+00:00"


def test_invalid_or_empty_snapshot_does_not_replace_last_good_state(
    database: Database,
) -> None:
    database.reconcile_snapshot(
        _snapshot("source-a", _item()),
        baseline=True,
        observed_at="2027-01-04T00:00:00+00:00",
    )

    with pytest.raises(InvalidSnapshotError, match="empty snapshot"):
        database.reconcile_snapshot(
            _snapshot("source-a"),
            baseline=True,
            observed_at="2027-01-04T00:15:00+00:00",
        )
    with pytest.raises(InvalidSnapshotError, match="without a saved snapshot"):
        database.reconcile_snapshot(
            _snapshot("unknown", not_modified=True),
            baseline=True,
            observed_at="2027-01-04T00:15:00+00:00",
        )
    database.record_source_failure(
        "failure-only",
        "offline",
        checked_at="2027-01-04T00:16:00+00:00",
    )
    with pytest.raises(InvalidSnapshotError, match="without a saved snapshot"):
        database.reconcile_snapshot(
            _snapshot("failure-only", not_modified=True),
            baseline=True,
            observed_at="2027-01-04T00:17:00+00:00",
        )

    cursor = database.get_source_cursor("source-a")
    assert cursor is not None
    assert cursor.item_count == 1
    assert cursor.last_success_at == "2027-01-04T00:00:00+00:00"
