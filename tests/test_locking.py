from __future__ import annotations

from pathlib import Path

import pytest

from job_notis.locking import AlreadyRunningError, single_process_lock


def test_lock_prevents_overlapping_runs(tmp_path: Path) -> None:
    lock_path = tmp_path / "check.lock"
    with (
        single_process_lock(lock_path),
        pytest.raises(AlreadyRunningError),
        single_process_lock(lock_path),
    ):
        raise AssertionError("unreachable")

    with single_process_lock(lock_path):
        assert lock_path.read_text().strip().isdigit()
