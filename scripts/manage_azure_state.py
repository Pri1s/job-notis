#!/usr/bin/env python3
"""Safely upload, inspect, or restore the Job Notis Azure state snapshot."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from job_notis.azure_state import backup_sqlite, compress_sqlite, restore_sqlite
from job_notis.config import redact_secret
from job_notis.database import Database


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--account-url", required=True)
    result.add_argument("--container", default="job-notis-state")
    result.add_argument("--blob", default="state.sqlite3.gz")
    subparsers = result.add_subparsers(dest="command", required=True)

    upload = subparsers.add_parser("upload", help="upload an initialized local database")
    upload.add_argument("--database", type=Path, required=True)
    upload.add_argument("--replace", action="store_true")

    download = subparsers.add_parser("download", help="restore cloud state to a new local file")
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--force", action="store_true")

    subparsers.add_parser("status", help="validate cloud state and print aggregate status")
    return result


def blob_client(namespace: argparse.Namespace) -> BlobClient:
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return BlobClient(
        account_url=namespace.account_url.rstrip("/"),
        container_name=namespace.container,
        blob_name=namespace.blob,
        credential=credential,
    )


def status_document(database_path: Path) -> dict[str, object]:
    with Database(database_path) as database:
        status = database.status()
    return {
        "initialized": status.initialized,
        "initialized_at": status.initialized_at,
        "sources": status.source_count,
        "observations": status.observation_count,
        "global_jobs": status.global_job_count,
        "open_jobs": status.open_job_count,
        "matching_open_jobs": status.matching_open_job_count,
        "pending_notifications": status.pending_notification_count,
        "sent_notifications": status.sent_notification_count,
        "hardware_bootstrap_pending": status.hardware_bootstrap_pending,
        "routes": {
            route_status.route.value: {
                "pending": route_status.pending,
                "sent": route_status.sent,
                "failed": route_status.failed,
            }
            for route_status in status.route_statuses
        },
    }


def upload(namespace: argparse.Namespace, client: BlobClient) -> int:
    with tempfile.TemporaryDirectory(prefix="job-notis-state-") as temporary:
        backup = Path(temporary) / "state.sqlite3"
        backup_sqlite(namespace.database.expanduser().resolve(), backup)
        with Database(backup) as database:
            status = database.status()
        if not status.initialized:
            raise RuntimeError("Refusing to upload an uninitialized database.")
        payload = compress_sqlite(backup)
        metadata = {
            "schema_version": "2",
            "initialized_at": status.initialized_at or "",
            "observations": str(status.observation_count),
            "pending": str(status.pending_notification_count),
            "migration": "true",
        }
        if namespace.replace:
            try:
                lease = client.acquire_lease(lease_duration=60)
            except ResourceNotFoundError:
                client.upload_blob(payload, overwrite=False, metadata=metadata)
            else:
                try:
                    client.upload_blob(
                        payload,
                        overwrite=True,
                        lease=lease,
                        metadata=metadata,
                    )
                finally:
                    lease.release()
        else:
            try:
                client.upload_blob(payload, overwrite=False, metadata=metadata)
            except ResourceExistsError as exc:
                raise RuntimeError(
                    "Cloud state already exists; use --replace only while the timer is disabled."
                ) from exc
        print(json.dumps(status_document(backup), sort_keys=True))
    return 0


def download(namespace: argparse.Namespace, client: BlobClient) -> int:
    output = namespace.output.expanduser().resolve()
    if output.exists() and not namespace.force:
        raise RuntimeError(f"Refusing to replace existing file {output}; pass --force.")
    try:
        payload = client.download_blob().readall()
    except ResourceNotFoundError as exc:
        raise RuntimeError("Cloud state does not exist.") from exc
    restore_sqlite(payload, output)
    print(json.dumps(status_document(output), sort_keys=True))
    return 0


def show_status(client: BlobClient) -> int:
    with tempfile.TemporaryDirectory(prefix="job-notis-state-") as temporary:
        database_path = Path(temporary) / "state.sqlite3"
        try:
            payload = client.download_blob().readall()
        except ResourceNotFoundError as exc:
            raise RuntimeError("Cloud state does not exist.") from exc
        restore_sqlite(payload, database_path)
        document = status_document(database_path)
        properties = client.get_blob_properties()
        document["blob_etag"] = str(properties.etag)
        document["last_check_at"] = properties.metadata.get("last_check_at")
        print(json.dumps(document, sort_keys=True))
    return 0


def run(arguments: Sequence[str] | None = None) -> int:
    namespace = parser().parse_args(arguments)
    client = blob_client(namespace)
    if namespace.command == "upload":
        return upload(namespace, client)
    if namespace.command == "download":
        return download(namespace, client)
    if namespace.command == "status":
        return show_status(client)
    raise AssertionError(namespace.command)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        message = redact_secret(str(exc))
        print(f"Error: {message}", file=__import__("sys").stderr)
        raise SystemExit(1) from None
