from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from job_notis.launchd import (
    LaunchdError,
    install_launch_agent,
    launch_agent_payload,
    launch_agent_status,
    render_launch_agent,
    uninstall_launch_agent,
)
from job_notis.paths import LAUNCH_AGENT_LABEL, AppPaths


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


def test_launch_agent_uses_calendar_schedule_and_absolute_executable(tmp_path: Path) -> None:
    payload = launch_agent_payload(paths_for(tmp_path), tmp_path)
    assert payload["Label"] == LAUNCH_AGENT_LABEL
    assert payload["StartCalendarInterval"] == [
        {"Minute": 7},
        {"Minute": 22},
        {"Minute": 37},
        {"Minute": 52},
    ]
    assert payload["ProgramArguments"] == [
        str(tmp_path / ".venv" / "bin" / "job-notis"),
        "check",
        "--scheduled",
    ]
    assert "RunAtLoad" not in payload


def test_rendered_launch_agent_is_valid_plist(tmp_path: Path) -> None:
    payload = plistlib.loads(render_launch_agent(paths_for(tmp_path), tmp_path))
    assert payload["Label"] == LAUNCH_AGENT_LABEL


def test_install_validates_and_loads_launch_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    executable = tmp_path / ".venv" / "bin" / "job-notis"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    launchctl_calls: list[tuple[str, ...]] = []
    monkeypatch.delenv("JOB_NOTIS_HOME", raising=False)
    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)
    monkeypatch.setattr(
        "job_notis.launchd.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["plutil"],
            returncode=0,
            stdout="OK",
            stderr="",
        ),
    )

    def launchctl(*arguments: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        launchctl_calls.append(arguments)
        return subprocess.CompletedProcess(["launchctl"], 0, "", "")

    monkeypatch.setattr("job_notis.launchd._run", launchctl)

    assert install_launch_agent(paths, tmp_path) == paths.launch_agent
    assert paths.launch_agent.exists()
    assert [call[0] for call in launchctl_calls] == ["bootout", "bootstrap", "enable"]


def test_install_rejects_non_executable_entrypoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / ".venv" / "bin" / "job-notis"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o600)
    monkeypatch.delenv("JOB_NOTIS_HOME", raising=False)
    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)

    with pytest.raises(LaunchdError, match="not executable"):
        install_launch_agent(paths_for(tmp_path), tmp_path)


def test_install_removes_invalid_temporary_plist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    executable = tmp_path / ".venv" / "bin" / "job-notis"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    monkeypatch.delenv("JOB_NOTIS_HOME", raising=False)
    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)
    monkeypatch.setattr(
        "job_notis.launchd.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["plutil"],
            returncode=1,
            stdout="",
            stderr="invalid plist",
        ),
    )

    with pytest.raises(LaunchdError, match="invalid plist"):
        install_launch_agent(paths, tmp_path)

    assert not paths.launch_agent.exists()
    assert not paths.launch_agent.with_suffix(".plist.tmp").exists()


def test_scheduler_management_is_disabled_for_isolated_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JOB_NOTIS_HOME", str(tmp_path))
    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)
    paths = paths_for(tmp_path)

    assert launch_agent_status(paths) == (
        False,
        "disabled while JOB_NOTIS_HOME is set",
    )
    with pytest.raises(LaunchdError, match="JOB_NOTIS_HOME"):
        install_launch_agent(paths, tmp_path)
    with pytest.raises(LaunchdError, match="JOB_NOTIS_HOME"):
        uninstall_launch_agent(paths)


def test_status_reports_failed_last_run(monkeypatch, tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths.launch_agent.write_text("<plist/>")
    monkeypatch.setattr(
        "job_notis.launchd._run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["launchctl"],
            returncode=0,
            stdout="state = not running\nlast exit code = 1\n",
            stderr="",
        ),
    )

    healthy, state = launch_agent_status(paths)

    assert healthy is False
    assert state.endswith("last run exited 1")


def test_status_detects_loaded_agent_with_missing_plist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "job_notis.launchd._run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["launchctl"],
            returncode=0,
            stdout="state = waiting\n",
            stderr="",
        ),
    )

    healthy, state = launch_agent_status(paths_for(tmp_path))

    assert healthy is False
    assert state == "loaded but plist is missing"


def test_status_distinguishes_missing_agent_from_launchctl_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    result = subprocess.CompletedProcess(
        args=["launchctl"],
        returncode=113,
        stdout="",
        stderr='Could not find service "com.pri.job-notis"',
    )
    monkeypatch.setattr("job_notis.launchd._run", lambda *_args, **_kwargs: result)
    assert launch_agent_status(paths_for(tmp_path)) == (False, "not installed")

    failed = subprocess.CompletedProcess(
        args=["launchctl"],
        returncode=1,
        stdout="",
        stderr="permission denied",
    )
    monkeypatch.setattr("job_notis.launchd._run", lambda *_args, **_kwargs: failed)
    assert launch_agent_status(paths_for(tmp_path)) == (
        False,
        "unable to query launchd: permission denied",
    )


def test_uninstall_boots_out_orphaned_agent_by_label(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*arguments: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(
            args=["launchctl", *arguments],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)
    monkeypatch.setattr("job_notis.launchd._run", run)

    assert uninstall_launch_agent(paths_for(tmp_path)) is True
    assert calls[0][0] == "print"
    assert calls[1][0] == "bootout"
    assert calls[0][1] == calls[1][1]
    assert calls[0][1].endswith(f"/{LAUNCH_AGENT_LABEL}")


def test_uninstall_preserves_plist_when_loaded_agent_cannot_be_stopped(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    paths.launch_agent.write_text("<plist/>")
    responses = iter(
        (
            subprocess.CompletedProcess(["launchctl"], 0, "", ""),
            subprocess.CompletedProcess(["launchctl"], 5, "", "permission denied"),
        )
    )
    monkeypatch.setattr("job_notis.launchd._require_macos", lambda: None)
    monkeypatch.setattr(
        "job_notis.launchd._run",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(LaunchdError, match="permission denied"):
        uninstall_launch_agent(paths)

    assert paths.launch_agent.exists()
