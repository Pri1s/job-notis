"""Filesystem paths for local runtime data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "job-notis"
LAUNCH_AGENT_LABEL = "com.pri.job-notis"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All user-specific paths used by the application."""

    app_support: Path
    config: Path
    database: Path
    lock: Path
    log_dir: Path
    log_file: Path
    launch_agent: Path

    def ensure_runtime_directories(self) -> None:
        self.app_support.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def default_paths() -> AppPaths:
    """Return macOS-native paths, with an isolated test/development override."""

    override = os.environ.get("JOB_NOTIS_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        app_support = root
        log_dir = root / "logs"
        launch_agent = root / f"{LAUNCH_AGENT_LABEL}.plist"
    else:
        home = Path.home()
        app_support = home / "Library" / "Application Support" / APP_NAME
        log_dir = home / "Library" / "Logs" / APP_NAME
        launch_agent = home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    return AppPaths(
        app_support=app_support,
        config=app_support / "config.toml",
        database=app_support / "state.sqlite3",
        lock=app_support / "check.lock",
        log_dir=log_dir,
        log_file=log_dir / "job-notis.log",
        launch_agent=launch_agent,
    )


def isolated_paths(root: Path) -> AppPaths:
    """Return portable paths rooted in one private runtime directory."""

    root = root.expanduser().resolve()
    return AppPaths(
        app_support=root,
        config=root / "config.toml",
        database=root / "state.sqlite3",
        lock=root / "check.lock",
        log_dir=root / "logs",
        log_file=root / "logs" / "job-notis.log",
        launch_agent=root / f"{LAUNCH_AGENT_LABEL}.plist",
    )


def project_root() -> Path:
    """Locate the checked-out project root."""

    return Path(__file__).resolve().parents[2]
