"""Durable SQLite snapshots stored as a leased Azure block blob."""

from __future__ import annotations

import gzip
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from job_notis.config import Config
from job_notis.database import SCHEMA_VERSION, Database, DatabaseStatus
from job_notis.paths import AppPaths, isolated_paths
from job_notis.service import CheckSummary, check, database_status

logger = logging.getLogger(__name__)

STORAGE_ACCOUNT_URL_ENV = "JOB_NOTIS_STORAGE_ACCOUNT_URL"
STATE_CONTAINER_ENV = "JOB_NOTIS_STATE_CONTAINER"
STATE_BLOB_ENV = "JOB_NOTIS_STATE_BLOB"
DEFAULT_STATE_CONTAINER = "job-notis-state"
DEFAULT_STATE_BLOB = "state.sqlite3.gz"
DEFAULT_CLOUD_ROOT = Path(tempfile.gettempdir()) / "job-notis"
LEASE_DURATION_SECONDS = 60
LEASE_RENEW_INTERVAL_SECONDS = 30


class AzureStateError(RuntimeError):
    """Raised when durable cloud state cannot be used safely."""


class StateLeaseUnavailable(AzureStateError):
    """Raised when another invocation currently owns the state blob."""


class _Lease(Protocol):
    id: str

    def renew(self) -> Any: ...

    def release(self) -> Any: ...


class _Download(Protocol):
    def readall(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AzureStateSettings:
    """Environment-backed location of the durable state snapshot."""

    account_url: str
    container: str = DEFAULT_STATE_CONTAINER
    blob: str = DEFAULT_STATE_BLOB

    @classmethod
    def from_environment(cls) -> AzureStateSettings:
        account_url = os.environ.get(STORAGE_ACCOUNT_URL_ENV, "").strip().rstrip("/")
        if not account_url.startswith("https://") or not account_url.endswith(
            ".blob.core.windows.net"
        ):
            raise AzureStateError(
                f"{STORAGE_ACCOUNT_URL_ENV} must be an Azure Blob Storage HTTPS endpoint."
            )
        container = os.environ.get(STATE_CONTAINER_ENV, DEFAULT_STATE_CONTAINER).strip()
        blob = os.environ.get(STATE_BLOB_ENV, DEFAULT_STATE_BLOB).strip()
        if not container or not blob:
            raise AzureStateError("Azure state container and blob names cannot be empty.")
        return cls(account_url=account_url, container=container, blob=blob)


class RenewingBlobLease(AbstractContextManager[_Lease]):
    """Hold a finite blob lease and renew it until the operation completes."""

    def __init__(
        self,
        blob_client: Any,
        *,
        duration_seconds: int = LEASE_DURATION_SECONDS,
        renew_interval_seconds: float = LEASE_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self._blob_client = blob_client
        self._duration_seconds = duration_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease: _Lease | None = None
        self._renewal_error: Exception | None = None

    def __enter__(self) -> _Lease:
        try:
            self._lease = self._blob_client.acquire_lease(self._duration_seconds)
        except ResourceExistsError as exc:
            raise StateLeaseUnavailable(
                "Another Job Notis cloud invocation owns the state lease."
            ) from exc
        except ResourceNotFoundError as exc:
            raise AzureStateError(
                "The Azure state blob does not exist; migrate initialized state before enabling."
            ) from exc
        except HttpResponseError as exc:
            raise AzureStateError("Azure rejected the state lease request.") from exc

        self._thread = threading.Thread(
            target=self._renew_until_stopped,
            name="job-notis-blob-lease",
            daemon=True,
        )
        self._thread.start()
        return self._lease

    def _renew_until_stopped(self) -> None:
        while not self._stop.wait(self._renew_interval_seconds):
            assert self._lease is not None
            try:
                self._lease.renew()
            except Exception as exc:
                self._renewal_error = exc
                self._stop.set()
                return

    def ensure_valid(self) -> None:
        if self._renewal_error is not None:
            raise AzureStateError("The Azure state lease could not be renewed.") from (
                self._renewal_error
            )

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Literal[False]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._renew_interval_seconds * 2, 1))
        if self._lease is not None:
            try:
                self._lease.release()
            except HttpResponseError as release_error:
                if exc is None:
                    raise AzureStateError("The Azure state lease could not be released.") from (
                        release_error
                    )
                logger.warning("Azure state lease release failed after another error.")
        return False


def validate_sqlite_snapshot(path: Path, *, allow_legacy: bool = False) -> int:
    """Validate integrity and return the supported SQLite schema version."""

    if not path.is_file():
        raise AzureStateError(f"SQLite state is missing at {path}.")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise AzureStateError("The SQLite state snapshot could not be read.") from exc
    if result is None or str(result[0]) != "ok":
        raise AzureStateError("The SQLite state snapshot failed its integrity check.")
    supported = {SCHEMA_VERSION, 1} if allow_legacy else {SCHEMA_VERSION}
    if version not in supported:
        raise AzureStateError(f"Expected SQLite schema version {SCHEMA_VERSION}, found {version}.")
    return version


def backup_sqlite(source: Path, destination: Path) -> None:
    """Create and validate one consistent SQLite backup file."""

    validate_sqlite_snapshot(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        temporary.chmod(0o600)
        validate_sqlite_snapshot(temporary)
        os.replace(temporary, destination)
    except (OSError, sqlite3.Error, AzureStateError) as exc:
        raise AzureStateError("Could not create a consistent SQLite backup.") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def compress_sqlite(source: Path) -> bytes:
    """Return a validated gzip representation of a SQLite database."""

    validate_sqlite_snapshot(source)
    try:
        return gzip.compress(source.read_bytes(), compresslevel=6, mtime=0)
    except OSError as exc:
        raise AzureStateError("Could not compress the SQLite state snapshot.") from exc


def restore_sqlite(payload: bytes, destination: Path) -> None:
    """Atomically restore and validate a gzip-compressed SQLite snapshot."""

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(f"{destination.suffix}.restore")
    temporary.unlink(missing_ok=True)
    try:
        raw_database = gzip.decompress(payload)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw_database)
            handle.flush()
            os.fsync(handle.fileno())
        version = validate_sqlite_snapshot(temporary, allow_legacy=True)
        if version == 1:
            backup = destination.with_suffix(f"{destination.suffix}.v1-backup.gz")
            backup.write_bytes(gzip.compress(raw_database, compresslevel=6, mtime=0))
            backup.chmod(0o600)
            with Database(temporary):
                pass
        validate_sqlite_snapshot(temporary)
        for sidecar_suffix in ("-wal", "-shm"):
            Path(f"{destination}{sidecar_suffix}").unlink(missing_ok=True)
        os.replace(temporary, destination)
    except (OSError, sqlite3.Error, gzip.BadGzipFile, EOFError, AzureStateError) as exc:
        raise AzureStateError("The Azure state blob is not a valid SQLite snapshot.") from exc
    finally:
        temporary.unlink(missing_ok=True)


class AzureBlobStateStore:
    """Run one application check against a leased, durable state snapshot."""

    def __init__(
        self,
        blob_client: Any,
        *,
        root: Path = DEFAULT_CLOUD_ROOT,
        lease_factory: Callable[[Any], RenewingBlobLease] = RenewingBlobLease,
    ) -> None:
        self._blob_client = blob_client
        self._root = root
        self._lease_factory = lease_factory

    @classmethod
    def from_environment(cls, *, root: Path = DEFAULT_CLOUD_ROOT) -> Self:
        settings = AzureStateSettings.from_environment()
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        blob_client = BlobClient(
            account_url=settings.account_url,
            container_name=settings.container,
            blob_name=settings.blob,
            credential=credential,
        )
        return cls(blob_client, root=root)

    def run_check(
        self,
        config: Config,
        *,
        check_runner: Callable[[Config, AppPaths], CheckSummary] = check,
    ) -> tuple[CheckSummary, DatabaseStatus]:
        paths = isolated_paths(self._root)
        paths.ensure_runtime_directories()

        lease_guard = self._lease_factory(self._blob_client)
        with lease_guard as lease:
            try:
                compressed = self._blob_client.download_blob(lease=lease).readall()
            except ResourceNotFoundError as exc:
                raise AzureStateError(
                    "The Azure state blob is missing; automatic initialization is disabled."
                ) from exc
            except HttpResponseError as exc:
                raise AzureStateError("Could not download the Azure state blob.") from exc

            restore_sqlite(compressed, paths.database)
            with Database(paths.database) as database:
                database.require_initialized()

            summary = check_runner(config, paths)
            status = database_status(paths)

            backup_path = paths.app_support / "state.backup.sqlite3"
            backup_sqlite(paths.database, backup_path)
            upload = compress_sqlite(backup_path)
            lease_guard.ensure_valid()
            metadata = {
                "schema_version": str(SCHEMA_VERSION),
                "initialized_at": status.initialized_at or "",
                "last_check_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "observations": str(status.observation_count),
                "pending": str(status.pending_notification_count),
            }
            try:
                self._blob_client.upload_blob(
                    upload,
                    overwrite=True,
                    lease=lease,
                    metadata=metadata,
                )
            except HttpResponseError as exc:
                raise AzureStateError("Could not upload the updated Azure state blob.") from exc
            finally:
                backup_path.unlink(missing_ok=True)

        return summary, status
