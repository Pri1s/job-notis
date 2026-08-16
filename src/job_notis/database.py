"""SQLite persistence and snapshot reconciliation.

The database is deliberately the source of truth for both deduplication and
delivery.  A source snapshot and any resulting outbox rows are committed in the
same transaction, so a newly discovered posting cannot be recorded without also
recording its notification.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_notis.filters import route_matches
from job_notis.identity import identity_for, observation_fingerprint
from job_notis.models import (
    NotificationRoute,
    PendingNotification,
    SourceItem,
    SourceSnapshot,
    Sponsorship,
)

SCHEMA_VERSION = 2


class DatabaseError(RuntimeError):
    """Base class for persistence errors."""


class NotInitializedError(DatabaseError):
    """Raised when a normal check is attempted before the silent baseline."""


class InvalidSnapshotError(DatabaseError):
    """Raised when a snapshot is unsafe to reconcile."""


@dataclass(frozen=True, slots=True)
class SourceCursor:
    """HTTP validators and health information for one source."""

    source: str
    etag: str | None
    last_modified: str | None
    last_checked_at: str | None
    last_success_at: str | None
    last_error: str | None
    item_count: int


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Summary of a committed source reconciliation."""

    source: str
    total_items: int
    new_global_jobs: int
    enqueued_notifications: int
    updated_observations: int
    missing_observations: int
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """Aggregate state used by the CLI status command."""

    initialized: bool
    initialized_at: str | None
    source_count: int
    observation_count: int
    global_job_count: int
    open_job_count: int
    matching_open_job_count: int
    pending_notification_count: int
    sent_notification_count: int
    cancelled_notification_count: int
    source_cursors: tuple[SourceCursor, ...]
    route_statuses: tuple[RouteStatus, ...] = ()
    hardware_bootstrap_pending: bool = False


@dataclass(frozen=True, slots=True)
class RouteStatus:
    """Delivery counters for one notification route."""

    route: NotificationRoute
    pending: int
    sent: int
    failed: int


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    item: SourceItem
    global_key: str
    canonical_url: str
    provider: str | None
    external_id: str | None
    fingerprint: str
    software_matches: bool
    hardware_matches: bool


class Database:
    """Own the application's SQLite connection and durable state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

        if str(self.path) != ":memory:":
            self.path.chmod(0o600)

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema version {version} is newer than supported version "
                f"{SCHEMA_VERSION}"
            )

        if version < 1:
            try:
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE source_cursors (
                        source TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        last_checked_at TEXT,
                        last_success_at TEXT,
                        last_error TEXT,
                        item_count INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE global_jobs (
                        id INTEGER PRIMARY KEY,
                        global_key TEXT NOT NULL UNIQUE,
                        canonical_url TEXT NOT NULL,
                        provider TEXT,
                        external_id TEXT,
                        company TEXT NOT NULL,
                        title TEXT NOT NULL,
                        locations_json TEXT NOT NULL,
                        apply_url TEXT NOT NULL,
                        season TEXT NOT NULL,
                        sponsorship TEXT NOT NULL,
                        posted_at TEXT,
                        discovered_after_init INTEGER NOT NULL
                            CHECK (discovered_after_init IN (0, 1)),
                        first_observed_at TEXT NOT NULL,
                        last_observed_at TEXT NOT NULL
                    );

                    CREATE TABLE job_aliases (
                        alias_key TEXT PRIMARY KEY,
                        global_job_id INTEGER NOT NULL
                            REFERENCES global_jobs(id) ON DELETE CASCADE,
                        first_observed_at TEXT NOT NULL
                    );

                    CREATE INDEX job_aliases_global_job_id_idx
                        ON job_aliases(global_job_id);

                    CREATE TABLE source_observations (
                        source TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        global_job_id INTEGER NOT NULL
                            REFERENCES global_jobs(id) ON DELETE CASCADE,
                        observation_fingerprint TEXT NOT NULL,
                        company TEXT NOT NULL,
                        title TEXT NOT NULL,
                        locations_json TEXT NOT NULL,
                        apply_url TEXT NOT NULL,
                        season TEXT NOT NULL,
                        sponsorship TEXT NOT NULL,
                        active INTEGER NOT NULL CHECK (active IN (0, 1)),
                        visible INTEGER NOT NULL CHECK (visible IN (0, 1)),
                        matches_preferences INTEGER NOT NULL
                            CHECK (matches_preferences IN (0, 1)),
                        present INTEGER NOT NULL CHECK (present IN (0, 1)),
                        posted_at TEXT,
                        source_first_seen_at TEXT,
                        category TEXT,
                        remote INTEGER NOT NULL CHECK (remote IN (0, 1)),
                        raw_json TEXT NOT NULL,
                        first_observed_at TEXT NOT NULL,
                        last_observed_at TEXT NOT NULL,
                        PRIMARY KEY (source, source_id)
                    );

                    CREATE INDEX source_observations_global_job_id_idx
                        ON source_observations(global_job_id);
                    CREATE INDEX source_observations_current_idx
                        ON source_observations(global_job_id, present, active, visible);
                    CREATE INDEX source_observations_rewrite_idx
                        ON source_observations(
                            source, observation_fingerprint, present
                        );

                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY,
                        global_job_id INTEGER NOT NULL
                            REFERENCES global_jobs(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        sent_at TEXT,
                        cancelled_at TEXT
                    );

                    CREATE INDEX notification_outbox_pending_idx
                        ON notification_outbox(sent_at, cancelled_at, id);

                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            version = 1

        if version == 1:
            self._migrate_v1_to_v2()

    def _migrate_v1_to_v2(self) -> None:
        """Migrate legacy software-only state without losing delivery history."""

        try:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE source_observations ADD COLUMN software_matches INTEGER
                    NOT NULL DEFAULT 0;
                ALTER TABLE source_observations ADD COLUMN hardware_matches INTEGER
                    NOT NULL DEFAULT 0;
                ALTER TABLE source_observations ADD COLUMN description TEXT;
                ALTER TABLE source_observations ADD COLUMN employment_type TEXT;
                ALTER TABLE source_observations ADD COLUMN upstream_updated_at TEXT;
                UPDATE source_observations
                SET software_matches = matches_preferences,
                    hardware_matches = 0;

                ALTER TABLE notification_outbox RENAME TO notification_outbox_v1;
                CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY,
                    global_job_id INTEGER NOT NULL
                        REFERENCES global_jobs(id) ON DELETE CASCADE,
                    route TEXT NOT NULL DEFAULT 'software'
                        CHECK (route IN ('software', 'hardware')),
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    sent_at TEXT,
                    cancelled_at TEXT
                );
                INSERT INTO notification_outbox(
                    id, global_job_id, route, created_at, attempts,
                    last_attempt_at, last_error, sent_at, cancelled_at
                )
                SELECT id, global_job_id, 'software', created_at, attempts,
                       last_attempt_at, last_error, sent_at, cancelled_at
                FROM notification_outbox_v1;
                DROP TABLE notification_outbox_v1;
                CREATE INDEX notification_outbox_pending_idx
                    ON notification_outbox(sent_at, cancelled_at, id);
                CREATE UNIQUE INDEX notification_outbox_job_route_idx
                    ON notification_outbox(global_job_id, route);

                UPDATE source_cursors SET etag = NULL, last_modified = NULL;
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def is_initialized(self) -> bool:
        """Return whether the initial silent baseline completed."""

        return self.initialized_at() is not None

    def initialized_at(self) -> str | None:
        """Return the baseline completion timestamp, if present."""

        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'initialized_at'"
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def mark_initialized(self, *, at: str | None = None) -> str:
        """Mark the silent baseline complete and return its timestamp."""

        timestamp = at or _utc_now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES ('initialized_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )
        return timestamp

    def require_initialized(self) -> None:
        """Fail clearly rather than turning a first scheduled check into alerts."""

        if not self.is_initialized():
            raise NotInitializedError(
                "job-notis has not been initialized; run `job-notis init` first"
            )

    def get_source_cursor(self, source: str) -> SourceCursor | None:
        """Return the saved HTTP validators for one source."""

        row = self._connection.execute(
            "SELECT * FROM source_cursors WHERE source = ?", (source,)
        ).fetchone()
        return _cursor_from_row(row) if row is not None else None

    def get_source_cursors(self) -> dict[str, SourceCursor]:
        """Return all source cursors keyed by source name."""

        rows = self._connection.execute("SELECT * FROM source_cursors ORDER BY source").fetchall()
        return {str(row["source"]): _cursor_from_row(row) for row in rows}

    def record_source_failure(
        self,
        source: str,
        error: str,
        *,
        checked_at: str | None = None,
    ) -> None:
        """Record a fetch/parse failure without changing the last good snapshot."""

        timestamp = checked_at or _utc_now()
        safe_error = error[:1000]
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO source_cursors(
                    source, last_checked_at, last_error, item_count
                )
                VALUES (?, ?, ?, 0)
                ON CONFLICT(source) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    last_error = excluded.last_error
                """,
                (source, timestamp, safe_error),
            )

    def reconcile_snapshot(
        self,
        snapshot: SourceSnapshot,
        *,
        baseline: bool = False,
        observed_at: str | None = None,
    ) -> ReconcileResult:
        """Atomically merge one complete source snapshot.

        Normal reconciliations are rejected until :meth:`mark_initialized` has
        been called.  Baseline reconciliations record all state but never create
        outbox rows.
        """

        if not baseline:
            self.require_initialized()

        timestamp = observed_at or _utc_now()
        self._validate_snapshot(snapshot)

        if snapshot.not_modified:
            return self._record_not_modified(snapshot, timestamp)

        prepared = tuple(self._prepare_item(item) for item in snapshot.items)
        incoming_ids = {entry.item.source_id for entry in prepared}
        initialization_timestamp = self.initialized_at()

        with self._transaction():
            previous_rows = self._connection.execute(
                """
                SELECT o.source_id, o.global_job_id, o.observation_fingerprint,
                       o.present, o.active, o.visible, j.provider, j.external_id
                FROM source_observations AS o
                JOIN global_jobs AS j ON j.id = o.global_job_id
                WHERE o.source = ?
                """,
                (snapshot.source,),
            ).fetchall()
            previous = {str(row["source_id"]): row for row in previous_rows}

            missing_rows = [
                row
                for source_id, row in previous.items()
                if bool(row["present"]) and source_id not in incoming_ids
            ]
            rewrite_targets = self._rewrite_targets(
                previous=previous,
                prepared=prepared,
                incoming_ids=incoming_ids,
            )

            newly_created_ids: set[int] = set()
            notification_candidates: set[tuple[int, NotificationRoute]] = set()
            updated_observations = 0

            for entry in prepared:
                existing = previous.get(entry.item.source_id)
                existing_job_id = int(existing["global_job_id"]) if existing else None
                rewrite_job_id = rewrite_targets.get(entry.item.source_id)
                job_id, created = self._resolve_global_job(
                    entry,
                    timestamp=timestamp,
                    preferred_job_id=existing_job_id or rewrite_job_id,
                    discovered_after_init=(
                        not baseline
                        and _item_first_seen_after_initialization(
                            entry.item,
                            initialization_timestamp,
                        )
                    ),
                )
                if created:
                    newly_created_ids.add(job_id)
                # A brand-new observation from a different/slower source can
                # make a post-baseline global job eligible for the first time.
                # Updates, reappearances, and same-source URL rewrites are not
                # candidates, which prevents edit/reopening alerts.
                source_already_observed = self._source_has_observed_global_job(
                    entry.item.source,
                    job_id,
                )
                if existing is None and rewrite_job_id is None and not source_already_observed:
                    route = (
                        NotificationRoute.HARDWARE
                        if entry.hardware_matches
                        else NotificationRoute.SOFTWARE
                        if entry.software_matches
                        else None
                    )
                    if route is not None:
                        notification_candidates.add((job_id, route))

                self._upsert_observation(entry, job_id=job_id, timestamp=timestamp)
                updated_observations += 1

            if missing_rows:
                placeholders = ", ".join("?" for _ in incoming_ids)
                if incoming_ids:
                    self._connection.execute(
                        f"""
                        UPDATE source_observations
                        SET present = 0
                        WHERE source = ? AND source_id NOT IN ({placeholders})
                        """,
                        (snapshot.source, *sorted(incoming_ids)),
                    )
                else:  # Defensive; empty complete snapshots are rejected above.
                    self._connection.execute(
                        "UPDATE source_observations SET present = 0 WHERE source = ?",
                        (snapshot.source,),
                    )

            enqueued = 0
            if not baseline:
                for job_id, route in sorted(
                    notification_candidates, key=lambda value: (value[0], value[1].value)
                ):
                    if self._job_was_discovered_after_init(
                        job_id
                    ) and self._job_is_eligible_and_open(job_id, route):
                        cursor = self._connection.execute(
                            """
                            INSERT INTO notification_outbox(global_job_id, route, created_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT(global_job_id, route) DO NOTHING
                            """,
                            (job_id, route.value, timestamp),
                        )
                        enqueued += cursor.rowcount

            self._connection.execute(
                """
                INSERT INTO source_cursors(
                    source, etag, last_modified, last_checked_at,
                    last_success_at, last_error, item_count
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(source) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL,
                    item_count = excluded.item_count
                """,
                (
                    snapshot.source,
                    snapshot.etag,
                    snapshot.last_modified,
                    timestamp,
                    timestamp,
                    len(prepared),
                ),
            )

        return ReconcileResult(
            source=snapshot.source,
            total_items=len(prepared),
            new_global_jobs=len(newly_created_ids),
            enqueued_notifications=enqueued,
            updated_observations=updated_observations,
            missing_observations=len(missing_rows),
        )

    def finalize_outbox(self, *, at: str | None = None) -> int:
        """Cancel pending alerts that are no longer eligible after a full poll.

        This deliberately runs after every successful source snapshot has been
        reconciled. Cancelling inside one source transaction could discard a
        valid alert during a status handoff where one source closes a role just
        before another source reports it open.
        """

        timestamp = at or _utc_now()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE notification_outbox
                SET cancelled_at = COALESCE(cancelled_at, ?),
                    last_error = NULL
                WHERE sent_at IS NULL
                  AND cancelled_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM source_observations
                      WHERE global_job_id = notification_outbox.global_job_id
                        AND present = 1
                        AND active = 1
                        AND visible = 1
                        AND CASE notification_outbox.route
                            WHEN 'hardware' THEN hardware_matches
                            ELSE software_matches
                        END = 1
                        AND (
                            notification_outbox.route = 'hardware'
                            OR NOT EXISTS(
                                SELECT 1 FROM source_observations AS hardware
                                WHERE hardware.global_job_id = notification_outbox.global_job_id
                                  AND hardware.present = 1
                                  AND hardware.active = 1
                                  AND hardware.visible = 1
                                  AND hardware.hardware_matches = 1
                            )
                        )
                  )
                """,
                (timestamp,),
            )
        return cursor.rowcount

    def _validate_snapshot(self, snapshot: SourceSnapshot) -> None:
        if not snapshot.source.strip():
            raise InvalidSnapshotError("snapshot source must not be blank")

        if snapshot.not_modified:
            cursor = self.get_source_cursor(snapshot.source)
            if cursor is None or cursor.last_success_at is None:
                raise InvalidSnapshotError(
                    f"{snapshot.source!r} returned not-modified without a saved snapshot"
                )
            return

        if not snapshot.items and not snapshot.authoritative_empty:
            raise InvalidSnapshotError(
                f"refusing to replace {snapshot.source!r} with an empty snapshot"
            )

        source_ids: set[str] = set()
        for item in snapshot.items:
            if item.source != snapshot.source:
                raise InvalidSnapshotError(
                    f"item source {item.source!r} does not match snapshot "
                    f"source {snapshot.source!r}"
                )
            if not item.source_id.strip():
                raise InvalidSnapshotError("source item id must not be blank")
            if item.source_id in source_ids:
                raise InvalidSnapshotError(
                    f"duplicate source item id {item.source_id!r} in {snapshot.source!r}"
                )
            source_ids.add(item.source_id)

    def _record_not_modified(self, snapshot: SourceSnapshot, timestamp: str) -> ReconcileResult:
        with self._transaction():
            cursor = self.get_source_cursor(snapshot.source)
            assert cursor is not None  # Validated immediately before the transaction.
            self._connection.execute(
                """
                UPDATE source_cursors
                SET etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified),
                    last_checked_at = ?,
                    last_success_at = ?,
                    last_error = NULL
                WHERE source = ?
                """,
                (
                    snapshot.etag,
                    snapshot.last_modified,
                    timestamp,
                    timestamp,
                    snapshot.source,
                ),
            )
        return ReconcileResult(
            source=snapshot.source,
            total_items=cursor.item_count,
            new_global_jobs=0,
            enqueued_notifications=0,
            updated_observations=0,
            missing_observations=0,
            not_modified=True,
        )

    @staticmethod
    def _prepare_item(item: SourceItem) -> _PreparedItem:
        identity = identity_for(item)
        return _PreparedItem(
            item=item,
            global_key=identity.global_key,
            canonical_url=identity.canonical_url,
            provider=identity.provider,
            external_id=identity.external_id,
            fingerprint=observation_fingerprint(item),
            software_matches=route_matches(item, NotificationRoute.SOFTWARE),
            hardware_matches=route_matches(item, NotificationRoute.HARDWARE),
        )

    @staticmethod
    def _rewrite_targets(
        *,
        previous: dict[str, sqlite3.Row],
        prepared: Sequence[_PreparedItem],
        incoming_ids: set[str],
    ) -> dict[str, int]:
        """Pair a URL rewrite with one unambiguous historical observation.

        Conflicting stable ATS requisition IDs are never merged. Historical
        observations are considered so a role removed in one poll and restored
        later under a rewritten URL remains the same global job.
        """

        old_by_fingerprint: dict[str, list[sqlite3.Row]] = {}
        incoming_existing_job_ids = {
            int(row["global_job_id"])
            for source_id, row in previous.items()
            if source_id in incoming_ids
        }
        for source_id, row in previous.items():
            if (
                source_id not in incoming_ids
                and int(row["global_job_id"]) not in incoming_existing_job_ids
            ):
                old_by_fingerprint.setdefault(str(row["observation_fingerprint"]), []).append(row)

        new_by_fingerprint: dict[str, list[_PreparedItem]] = {}
        for entry in prepared:
            if entry.item.source_id not in previous:
                new_by_fingerprint.setdefault(entry.fingerprint, []).append(entry)

        targets: dict[str, int] = {}
        for fingerprint, new_entries in new_by_fingerprint.items():
            old_rows = old_by_fingerprint.get(fingerprint, [])
            if len(new_entries) != 1:
                continue
            entry = new_entries[0]
            compatible_job_ids = {
                int(row["global_job_id"])
                for row in old_rows
                if not _stable_identity_conflicts(row, entry)
            }
            if len(compatible_job_ids) == 1:
                targets[entry.item.source_id] = compatible_job_ids.pop()
        return targets

    def _resolve_global_job(
        self,
        entry: _PreparedItem,
        *,
        timestamp: str,
        preferred_job_id: int | None,
        discovered_after_init: bool,
    ) -> tuple[int, bool]:
        alias_row = self._connection.execute(
            "SELECT global_job_id FROM job_aliases WHERE alias_key = ?",
            (entry.global_key,),
        ).fetchone()
        if alias_row is not None:
            job_id = int(alias_row["global_job_id"])
            created = False
        elif preferred_job_id is not None:
            job_id = preferred_job_id
            created = False
        else:
            cursor = self._connection.execute(
                """
                INSERT INTO global_jobs(
                    global_key, canonical_url, provider, external_id,
                    company, title, locations_json, apply_url, season,
                    sponsorship, posted_at, discovered_after_init,
                    first_observed_at, last_observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.global_key,
                    entry.canonical_url,
                    entry.provider,
                    entry.external_id,
                    entry.item.company,
                    entry.item.title,
                    _json_locations(entry.item.locations),
                    entry.item.apply_url,
                    entry.item.season,
                    entry.item.sponsorship.value,
                    entry.item.posted_at,
                    int(discovered_after_init),
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite contract.
                raise DatabaseError("SQLite did not return an id for a new global job")
            job_id = cursor.lastrowid
            created = True

        self._connection.execute(
            """
            INSERT INTO job_aliases(alias_key, global_job_id, first_observed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(alias_key) DO NOTHING
            """,
            (entry.global_key, job_id, timestamp),
        )
        self._connection.execute(
            """
            UPDATE global_jobs
            SET canonical_url = ?,
                provider = COALESCE(?, provider),
                external_id = COALESCE(?, external_id),
                company = ?,
                title = ?,
                locations_json = ?,
                apply_url = ?,
                season = ?,
                sponsorship = ?,
                posted_at = COALESCE(?, posted_at),
                last_observed_at = ?
            WHERE id = ?
            """,
            (
                entry.canonical_url,
                entry.provider,
                entry.external_id,
                entry.item.company,
                entry.item.title,
                _json_locations(entry.item.locations),
                entry.item.apply_url,
                entry.item.season,
                entry.item.sponsorship.value,
                entry.item.posted_at,
                timestamp,
                job_id,
            ),
        )
        return job_id, created

    def _upsert_observation(
        self,
        entry: _PreparedItem,
        *,
        job_id: int,
        timestamp: str,
    ) -> None:
        item = entry.item
        self._connection.execute(
            """
            INSERT INTO source_observations(
                source, source_id, global_job_id, observation_fingerprint,
                company, title, locations_json, apply_url, season, sponsorship,
                active, visible, matches_preferences, software_matches, hardware_matches,
                present, posted_at, source_first_seen_at, category, remote,
                description, employment_type, upstream_updated_at, raw_json,
                first_observed_at, last_observed_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(source, source_id) DO UPDATE SET
                global_job_id = excluded.global_job_id,
                observation_fingerprint = excluded.observation_fingerprint,
                company = excluded.company,
                title = excluded.title,
                locations_json = excluded.locations_json,
                apply_url = excluded.apply_url,
                season = excluded.season,
                sponsorship = excluded.sponsorship,
                active = excluded.active,
                visible = excluded.visible,
                matches_preferences = excluded.matches_preferences,
                software_matches = excluded.software_matches,
                hardware_matches = excluded.hardware_matches,
                present = 1,
                posted_at = excluded.posted_at,
                source_first_seen_at = excluded.source_first_seen_at,
                category = excluded.category,
                remote = excluded.remote,
                description = excluded.description,
                employment_type = excluded.employment_type,
                upstream_updated_at = excluded.upstream_updated_at,
                raw_json = excluded.raw_json,
                last_observed_at = excluded.last_observed_at
            """,
            (
                item.source,
                item.source_id,
                job_id,
                entry.fingerprint,
                item.company,
                item.title,
                _json_locations(item.locations),
                item.apply_url,
                item.season,
                item.sponsorship.value,
                int(item.active),
                int(item.visible),
                int(entry.software_matches),
                int(entry.software_matches),
                int(entry.hardware_matches),
                item.posted_at,
                item.first_seen_at,
                item.category,
                int(item.remote),
                item.description,
                item.employment_type,
                item.upstream_updated_at,
                json.dumps(item.raw, sort_keys=True, separators=(",", ":"), default=str),
                timestamp,
                timestamp,
            ),
        )

    def _global_job_id_for_source_item(self, source: str, source_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT global_job_id
            FROM source_observations
            WHERE source = ? AND source_id = ?
            """,
            (source, source_id),
        ).fetchone()
        if row is None:
            raise DatabaseError(f"observation disappeared during reconciliation: {source}")
        return int(row["global_job_id"])

    def _source_has_observed_global_job(self, source: str, job_id: int) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM source_observations
                WHERE source = ? AND global_job_id = ?
            )
            """,
            (source, job_id),
        ).fetchone()
        return bool(row[0])

    def _job_is_eligible_and_open(self, job_id: int, route: NotificationRoute) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM source_observations
                WHERE global_job_id = ?
                  AND present = 1
                  AND active = 1
                  AND visible = 1
                  AND CASE ? WHEN 'hardware' THEN hardware_matches ELSE software_matches END = 1
                  AND (
                      ? = 'hardware'
                      OR NOT EXISTS(
                          SELECT 1 FROM source_observations AS hardware
                          WHERE hardware.global_job_id = ?
                            AND hardware.present = 1
                            AND hardware.active = 1
                            AND hardware.visible = 1
                            AND hardware.hardware_matches = 1
                      )
                  )
            )
            """,
            (job_id, route.value, route.value, job_id),
        ).fetchone()
        return bool(row[0])

    def _job_was_discovered_after_init(self, job_id: int) -> bool:
        row = self._connection.execute(
            "SELECT discovered_after_init FROM global_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise DatabaseError(f"unknown global job id: {job_id}")
        return bool(row["discovered_after_init"])

    def _job_is_open(self, job_id: int) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM source_observations
                WHERE global_job_id = ?
                  AND present = 1
                  AND active = 1
                  AND visible = 1
            )
            """,
            (job_id,),
        ).fetchone()
        return bool(row[0])

    def pending_notifications(
        self,
        *,
        limit: int | None = None,
        route: NotificationRoute | None = None,
    ) -> tuple[PendingNotification, ...]:
        """Return pending, still-open notifications in durable order."""

        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")

        sql = """
            SELECT o.id AS notification_id, o.attempts, o.route,
                   j.id AS global_job_id, j.global_key, j.first_observed_at
            FROM notification_outbox AS o
            JOIN global_jobs AS j ON j.id = o.global_job_id
            WHERE o.sent_at IS NULL
              AND o.cancelled_at IS NULL
              AND EXISTS(
                  SELECT 1
                  FROM source_observations AS current
                  WHERE current.global_job_id = j.id
                    AND current.present = 1
                    AND current.active = 1
                    AND current.visible = 1
                    AND CASE o.route
                        WHEN 'hardware' THEN current.hardware_matches
                        ELSE current.software_matches
                    END = 1
                    AND (
                        o.route = 'hardware'
                        OR NOT EXISTS(
                            SELECT 1 FROM source_observations AS hardware
                            WHERE hardware.global_job_id = j.id
                              AND hardware.present = 1
                              AND hardware.active = 1
                              AND hardware.visible = 1
                              AND hardware.hardware_matches = 1
                        )
                    )
              )
            ORDER BY o.id
        """
        parameters: tuple[str | int, ...] = ()
        if route is not None:
            sql = sql.replace("ORDER BY o.id", "AND o.route = ?\n            ORDER BY o.id")
            parameters = (route.value,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (*parameters, limit)

        rows = self._connection.execute(sql, parameters).fetchall()
        pending: list[PendingNotification] = []
        for row in rows:
            representative = self._connection.execute(
                """
                SELECT company, title, locations_json, apply_url, season,
                       sponsorship, posted_at
                FROM source_observations
                WHERE global_job_id = ?
                  AND present = 1
                  AND active = 1
                  AND visible = 1
                  AND CASE ? WHEN 'hardware' THEN hardware_matches ELSE software_matches END = 1
                  AND (
                      ? = 'hardware'
                      OR NOT EXISTS(
                          SELECT 1 FROM source_observations AS hardware
                          WHERE hardware.global_job_id = ?
                            AND hardware.present = 1
                            AND hardware.active = 1
                            AND hardware.visible = 1
                            AND hardware.hardware_matches = 1
                      )
                  )
                ORDER BY last_observed_at DESC, source, source_id
                LIMIT 1
                """,
                (
                    int(row["global_job_id"]),
                    str(row["route"]),
                    str(row["route"]),
                    int(row["global_job_id"]),
                ),
            ).fetchone()
            if representative is None:  # State may change on another connection.
                continue
            sources = self._connection.execute(
                """
                SELECT DISTINCT source
                FROM source_observations
                WHERE global_job_id = ?
                  AND present = 1
                  AND active = 1
                  AND visible = 1
                ORDER BY source
                """,
                (int(row["global_job_id"]),),
            ).fetchall()
            pending.append(
                PendingNotification(
                    id=int(row["notification_id"]),
                    global_key=str(row["global_key"]),
                    company=str(representative["company"]),
                    title=str(representative["title"]),
                    locations=tuple(json.loads(str(representative["locations_json"]))),
                    apply_url=str(representative["apply_url"]),
                    season=str(representative["season"]),
                    sponsorship=Sponsorship(str(representative["sponsorship"])),
                    posted_at=(
                        str(representative["posted_at"])
                        if representative["posted_at"] is not None
                        else None
                    ),
                    first_observed_at=str(row["first_observed_at"]),
                    sources=tuple(str(source["source"]) for source in sources),
                    attempts=int(row["attempts"]),
                    route=NotificationRoute(str(row["route"])),
                )
            )
        return tuple(pending)

    def pending_notification_batches(
        self,
        *,
        batch_size: int = 10,
    ) -> tuple[tuple[PendingNotification, ...], ...]:
        """Group pending records into Discord's maximum ten-embed batches."""

        if not 1 <= batch_size <= 10:
            raise ValueError("batch_size must be between 1 and 10")
        pending = self.pending_notifications()
        return tuple(
            pending[index : index + batch_size] for index in range(0, len(pending), batch_size)
        )

    def mark_notification_sent(
        self,
        notification_id: int,
        *,
        sent_at: str | None = None,
    ) -> bool:
        """Mark one outbox record delivered; return whether it changed."""

        timestamp = sent_at or _utc_now()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1,
                    last_attempt_at = ?,
                    last_error = NULL,
                    sent_at = ?
                WHERE id = ?
                  AND sent_at IS NULL
                  AND cancelled_at IS NULL
                """,
                (timestamp, timestamp, notification_id),
            )
        return cursor.rowcount == 1

    def mark_notification_failed(
        self,
        notification_id: int,
        error: str,
        *,
        attempted_at: str | None = None,
    ) -> bool:
        """Record a failed delivery while leaving the outbox row pending."""

        timestamp = attempted_at or _utc_now()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1,
                    last_attempt_at = ?,
                    last_error = ?
                WHERE id = ?
                  AND sent_at IS NULL
                  AND cancelled_at IS NULL
                """,
                (timestamp, error[:1000], notification_id),
            )
        return cursor.rowcount == 1

    def hardware_bootstrap_pending(self) -> bool:
        """Return whether the initial hardware seed still needs a complete poll."""

        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'hardware_bootstrap_pending'"
        ).fetchone()
        return row is None or str(row["value"]) != "0"

    def complete_hardware_bootstrap(
        self,
        *,
        cutoff: str,
        at: str | None = None,
    ) -> int:
        """Seed recent open hardware matches exactly once after a complete poll."""

        timestamp = at or _utc_now()
        with self._transaction():
            pending = self.hardware_bootstrap_pending()
            if not pending:
                return 0
            rows = self._connection.execute(
                """
                SELECT DISTINCT o.global_job_id
                FROM source_observations AS o
                WHERE o.present = 1 AND o.active = 1 AND o.visible = 1
                  AND o.hardware_matches = 1
                  AND o.posted_at IS NOT NULL
                  AND o.posted_at >= ?
                """,
                (cutoff,),
            ).fetchall()
            seeded = 0
            for row in rows:
                cursor = self._connection.execute(
                    """
                    INSERT INTO notification_outbox(global_job_id, route, created_at)
                    VALUES (?, 'hardware', ?)
                    ON CONFLICT(global_job_id, route) DO NOTHING
                    """,
                    (int(row["global_job_id"]), timestamp),
                )
                seeded += cursor.rowcount
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('hardware_bootstrap_pending', '0')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('hardware_bootstrap_completed_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )
            return seeded

    def status(self) -> DatabaseStatus:
        """Return aggregate database state for diagnostics."""

        counts = self._connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM source_observations) AS observations,
                (SELECT COUNT(*) FROM global_jobs) AS jobs,
                (
                    SELECT COUNT(*)
                    FROM global_jobs AS j
                    WHERE EXISTS(
                        SELECT 1
                        FROM source_observations AS o
                        WHERE o.global_job_id = j.id
                          AND o.present = 1
                          AND o.active = 1
                          AND o.visible = 1
                    )
                ) AS open_jobs,
                (
                    SELECT COUNT(*)
                    FROM global_jobs AS j
                    WHERE EXISTS(
                        SELECT 1
                        FROM source_observations AS o
                        WHERE o.global_job_id = j.id
                          AND o.present = 1
                          AND o.active = 1
                          AND o.visible = 1
                          AND o.software_matches = 1
                          AND NOT EXISTS(
                              SELECT 1 FROM source_observations AS hardware
                              WHERE hardware.global_job_id = j.id
                                AND hardware.present = 1
                                AND hardware.active = 1
                                AND hardware.visible = 1
                                AND hardware.hardware_matches = 1
                          )
                    )
                ) AS matching_open_jobs,
                (
                    SELECT COUNT(*)
                    FROM notification_outbox
                    WHERE sent_at IS NULL AND cancelled_at IS NULL
                ) AS pending,
                (
                    SELECT COUNT(*)
                    FROM notification_outbox
                    WHERE sent_at IS NOT NULL
                ) AS sent,
                (
                    SELECT COUNT(*)
                    FROM notification_outbox
                    WHERE cancelled_at IS NOT NULL
                ) AS cancelled
            """
        ).fetchone()
        cursors = tuple(self.get_source_cursors().values())
        initialized_at = self.initialized_at()
        route_statuses = tuple(
            RouteStatus(
                route=route,
                pending=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_outbox "
                        "WHERE route = ? AND sent_at IS NULL AND cancelled_at IS NULL",
                        (route.value,),
                    ).fetchone()[0]
                ),
                sent=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_outbox "
                        "WHERE route = ? AND sent_at IS NOT NULL",
                        (route.value,),
                    ).fetchone()[0]
                ),
                failed=int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_outbox "
                        "WHERE route = ? AND sent_at IS NULL AND cancelled_at IS NULL "
                        "AND last_error IS NOT NULL",
                        (route.value,),
                    ).fetchone()[0]
                ),
            )
            for route in NotificationRoute
        )
        return DatabaseStatus(
            initialized=initialized_at is not None,
            initialized_at=initialized_at,
            source_count=len(cursors),
            observation_count=int(counts["observations"]),
            global_job_count=int(counts["jobs"]),
            open_job_count=int(counts["open_jobs"]),
            matching_open_job_count=int(counts["matching_open_jobs"]),
            pending_notification_count=int(counts["pending"]),
            sent_notification_count=int(counts["sent"]),
            cancelled_notification_count=int(counts["cancelled"]),
            source_cursors=cursors,
            route_statuses=route_statuses,
            hardware_bootstrap_pending=self.hardware_bootstrap_pending(),
        )


def _cursor_from_row(row: sqlite3.Row) -> SourceCursor:
    return SourceCursor(
        source=str(row["source"]),
        etag=str(row["etag"]) if row["etag"] is not None else None,
        last_modified=(str(row["last_modified"]) if row["last_modified"] is not None else None),
        last_checked_at=(
            str(row["last_checked_at"]) if row["last_checked_at"] is not None else None
        ),
        last_success_at=(
            str(row["last_success_at"]) if row["last_success_at"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        item_count=int(row["item_count"]),
    )


def _item_first_seen_after_initialization(
    item: SourceItem,
    initialized_at: str | None,
) -> bool:
    if item.first_seen_at is None or initialized_at is None:
        return True
    try:
        first_seen = datetime.fromisoformat(item.first_seen_at.replace("Z", "+00:00"))
        initialized = datetime.fromisoformat(initialized_at.replace("Z", "+00:00"))
    except ValueError:
        # Adapters validate upstream timestamps. If a custom caller violates
        # that contract, favor alerting over silently losing a real new job.
        return True
    if first_seen.tzinfo is None or initialized.tzinfo is None:
        return True
    return first_seen.astimezone(UTC) > initialized.astimezone(UTC)


def _stable_identity_conflicts(row: sqlite3.Row, entry: _PreparedItem) -> bool:
    old_provider = row["provider"]
    old_external_id = row["external_id"]
    if (
        old_provider is None
        or old_external_id is None
        or entry.provider is None
        or entry.external_id is None
    ):
        return False
    return (str(old_provider), str(old_external_id)) != (
        entry.provider,
        entry.external_id,
    )


def _json_locations(locations: Sequence[str]) -> str:
    return json.dumps(list(locations), ensure_ascii=False, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
