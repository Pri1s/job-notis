"""Install and manage the per-user macOS LaunchAgent."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any

from job_notis.paths import LAUNCH_AGENT_LABEL, AppPaths, project_root

_SCHEDULE_MINUTES = (7, 22, 37, 52)


class LaunchdError(RuntimeError):
    """Raised when the LaunchAgent cannot be installed or managed."""


def executable_path(root: Path | None = None) -> Path:
    root = root or project_root()
    return root / ".venv" / "bin" / "job-notis"


def launch_agent_payload(paths: AppPaths, root: Path | None = None) -> dict[str, Any]:
    """Return the property-list object used for the LaunchAgent."""

    root = (root or project_root()).resolve()
    executable = executable_path(root)
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(executable), "check", "--scheduled"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": [{"Minute": minute} for minute in _SCHEDULE_MINUTES],
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }


def render_launch_agent(paths: AppPaths, root: Path | None = None) -> bytes:
    return plistlib.dumps(launch_agent_payload(paths, root), fmt=plistlib.FMT_XML, sort_keys=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_domain()}/{LAUNCH_AGENT_LABEL}"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise LaunchdError("LaunchAgent installation is only supported on macOS.")


def _require_default_home() -> None:
    if os.environ.get("JOB_NOTIS_HOME"):
        raise LaunchdError("LaunchAgent management is disabled while JOB_NOTIS_HOME is set.")


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _launchctl_message(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip()


def _service_is_missing(result: subprocess.CompletedProcess[str]) -> bool:
    return "could not find service" in f"{result.stderr}\n{result.stdout}".casefold()


def install_launch_agent(paths: AppPaths, root: Path | None = None) -> Path:
    """Write, validate, and bootstrap the user's LaunchAgent."""

    _require_macos()
    _require_default_home()
    root = (root or project_root()).resolve()
    executable = executable_path(root)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LaunchdError(
            f"{executable} is missing or not executable; run `uv sync --locked` first."
        )

    paths.launch_agent.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.launch_agent.with_suffix(".plist.tmp")
    temporary.write_bytes(render_launch_agent(paths, root))
    temporary.chmod(0o600)
    validation = subprocess.run(
        ["/usr/bin/plutil", "-lint", str(temporary)],
        check=False,
        capture_output=True,
        text=True,
    )
    if validation.returncode:
        temporary.unlink(missing_ok=True)
        raise LaunchdError(validation.stderr.strip() or validation.stdout.strip())
    os.replace(temporary, paths.launch_agent)

    # Reinstall cleanly when the command is run more than once.
    _run("bootout", _service_target(), check=False)
    result = _run("bootstrap", _domain(), str(paths.launch_agent), check=False)
    if result.returncode:
        raise LaunchdError(result.stderr.strip() or "launchctl bootstrap failed")
    _run("enable", _service_target())
    return paths.launch_agent


def uninstall_launch_agent(paths: AppPaths) -> bool:
    """Unload and remove the LaunchAgent while preserving config and state."""

    _require_macos()
    _require_default_home()
    existed = paths.launch_agent.exists()
    probe = _run("print", _service_target(), check=False)
    if probe.returncode and not _service_is_missing(probe):
        raise LaunchdError(_launchctl_message(probe) or "launchctl status query failed")
    loaded = probe.returncode == 0
    if loaded:
        result = _run("bootout", _service_target(), check=False)
        if result.returncode:
            raise LaunchdError(result.stderr.strip() or "launchctl bootout failed")
    paths.launch_agent.unlink(missing_ok=True)
    return existed or loaded


def launch_agent_status(paths: AppPaths) -> tuple[bool, str]:
    if os.environ.get("JOB_NOTIS_HOME"):
        return False, "disabled while JOB_NOTIS_HOME is set"
    result = _run("print", _service_target(), check=False)
    if result.returncode and not _service_is_missing(result):
        detail = _launchctl_message(result) or "unknown launchctl error"
        return False, f"unable to query launchd: {detail}"
    if not paths.launch_agent.exists():
        if result.returncode == 0:
            return False, "loaded but plist is missing"
        return False, "not installed"
    if result.returncode:
        return False, "installed but not loaded"
    exit_code = re.search(r"last exit code = (-?\d+)", result.stdout)
    if exit_code and int(exit_code.group(1)) != 0:
        return False, f"installed and loaded; last run exited {exit_code.group(1)}"
    return True, "installed and loaded"
