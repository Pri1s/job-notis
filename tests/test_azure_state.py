from __future__ import annotations

import time
from pathlib import Path

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

from job_notis.azure_state import (
    STORAGE_ACCOUNT_URL_ENV,
    AzureBlobStateStore,
    AzureStateError,
    AzureStateSettings,
    RenewingBlobLease,
    StateLeaseUnavailable,
    backup_sqlite,
    compress_sqlite,
    restore_sqlite,
    validate_sqlite_snapshot,
)
from job_notis.config import Config
from job_notis.database import Database
from job_notis.paths import AppPaths
from job_notis.service import CheckSummary


class FakeLease:
    id = "lease-id"

    def __init__(self, *, renewal_error: Exception | None = None) -> None:
        self.renewal_error = renewal_error
        self.renewals = 0
        self.released = False

    def renew(self) -> None:
        self.renewals += 1
        if self.renewal_error:
            raise self.renewal_error

    def release(self) -> None:
        self.released = True


class FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def readall(self) -> bytes:
        return self.payload


class FakeBlobClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.lease = FakeLease()
        self.acquire_error: Exception | None = None
        self.download_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.uploaded: bytes | None = None
        self.metadata: dict[str, str] | None = None

    def acquire_lease(self, lease_duration: int) -> FakeLease:
        assert lease_duration == 60
        if self.acquire_error:
            raise self.acquire_error
        return self.lease

    def download_blob(self, *, lease: FakeLease) -> FakeDownload:
        assert lease is self.lease
        if self.download_error:
            raise self.download_error
        return FakeDownload(self.payload)

    def upload_blob(
        self,
        data: bytes,
        *,
        overwrite: bool,
        lease: FakeLease,
        metadata: dict[str, str],
    ) -> None:
        assert overwrite is True
        assert lease is self.lease
        if self.upload_error:
            raise self.upload_error
        self.uploaded = data
        self.metadata = metadata


def initialized_database(path: Path) -> str:
    with Database(path) as database:
        initialized_at = database.mark_initialized()
    return initialized_at


def summary() -> CheckSummary:
    return CheckSummary(
        sources_checked=4,
        sources_changed=0,
        source_failures=(),
        new_global_jobs=0,
        enqueued_notifications=0,
        delivered_notifications=0,
        pending_notifications=0,
        delivery_error=None,
    )


def test_settings_require_valid_storage_endpoint(monkeypatch) -> None:
    monkeypatch.delenv(STORAGE_ACCOUNT_URL_ENV, raising=False)
    with pytest.raises(AzureStateError, match=STORAGE_ACCOUNT_URL_ENV):
        AzureStateSettings.from_environment()

    monkeypatch.setenv(
        STORAGE_ACCOUNT_URL_ENV,
        "https://jobnotis.blob.core.windows.net/",
    )
    settings = AzureStateSettings.from_environment()
    assert settings.account_url == "https://jobnotis.blob.core.windows.net"
    assert settings.blob == "state.sqlite3.gz"


def test_sqlite_backup_compression_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    initialized_at = initialized_database(source)
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"

    backup_sqlite(source, backup)
    restore_sqlite(compress_sqlite(backup), restored)

    assert validate_sqlite_snapshot(restored) == 2
    with Database(restored) as database:
        assert database.initialized_at() == initialized_at


def test_restore_rejects_corrupt_payload_without_replacing_state(tmp_path: Path) -> None:
    destination = tmp_path / "state.sqlite3"
    initialized_database(destination)
    original = destination.read_bytes()

    with pytest.raises(AzureStateError, match="not a valid"):
        restore_sqlite(b"not gzip", destination)

    assert destination.read_bytes() == original


def test_state_store_round_trip_persists_initialized_database(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    initialized_at = initialized_database(source)
    blob = FakeBlobClient(compress_sqlite(source))
    store = AzureBlobStateStore(blob, root=tmp_path / "runtime")

    result, status = store.run_check(
        Config("https://discord.com/api/webhooks/1/token"),
        check_runner=lambda _config, _paths: summary(),
    )

    assert result == summary()
    assert status.initialized_at == initialized_at
    assert blob.uploaded is not None
    assert blob.metadata is not None
    assert blob.metadata["initialized_at"] == initialized_at
    assert blob.lease.released is True
    restored = tmp_path / "uploaded.sqlite3"
    restore_sqlite(blob.uploaded, restored)
    with Database(restored) as database:
        assert database.initialized_at() == initialized_at


def test_state_store_preserves_blob_when_check_crashes(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    initialized_database(source)
    blob = FakeBlobClient(compress_sqlite(source))
    store = AzureBlobStateStore(blob, root=tmp_path / "runtime")

    def crash(_config: Config, _paths: AppPaths) -> CheckSummary:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        store.run_check(
            Config("https://discord.com/api/webhooks/1/token"),
            check_runner=crash,
        )

    assert blob.uploaded is None
    assert blob.lease.released is True


def test_state_store_fails_closed_for_missing_or_corrupt_blob(tmp_path: Path) -> None:
    missing = FakeBlobClient(b"")
    missing.download_error = ResourceNotFoundError("missing")
    missing_store = AzureBlobStateStore(missing, root=tmp_path / "missing")
    with pytest.raises(AzureStateError, match="missing"):
        missing_store.run_check(
            Config("https://discord.com/api/webhooks/1/token"),
            check_runner=lambda _config, _paths: summary(),
        )
    assert missing.uploaded is None
    assert missing.lease.released is True

    corrupt = FakeBlobClient(b"corrupt")
    corrupt_store = AzureBlobStateStore(corrupt, root=tmp_path / "corrupt")
    with pytest.raises(AzureStateError, match="not a valid"):
        corrupt_store.run_check(
            Config("https://discord.com/api/webhooks/1/token"),
            check_runner=lambda _config, _paths: summary(),
        )
    assert corrupt.uploaded is None
    assert corrupt.lease.released is True


def test_lease_contention_is_actionable() -> None:
    blob = FakeBlobClient(b"")
    blob.acquire_error = ResourceExistsError("leased")

    with pytest.raises(StateLeaseUnavailable, match="Another"), RenewingBlobLease(blob):
        pass


def test_lease_renews_and_releases() -> None:
    blob = FakeBlobClient(b"")
    with RenewingBlobLease(blob, renew_interval_seconds=0.01) as lease:
        assert lease is blob.lease
        time.sleep(0.03)

    assert blob.lease.renewals >= 1
    assert blob.lease.released is True


def test_upload_failure_releases_lease(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    initialized_database(source)
    blob = FakeBlobClient(compress_sqlite(source))
    blob.upload_error = HttpResponseError("upload failed")
    store = AzureBlobStateStore(blob, root=tmp_path / "runtime")

    with pytest.raises(AzureStateError, match="upload"):
        store.run_check(
            Config("https://discord.com/api/webhooks/1/token"),
            check_runner=lambda _config, _paths: summary(),
        )

    assert blob.lease.released is True
