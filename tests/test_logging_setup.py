from __future__ import annotations

import logging
from pathlib import Path

from job_notis.logging_setup import configure_logging
from job_notis.paths import AppPaths


def paths_for(root: Path) -> AppPaths:
    return AppPaths(
        app_support=root,
        config=root / "config.toml",
        database=root / "state.sqlite3",
        lock=root / "check.lock",
        log_dir=root / "logs",
        log_file=root / "logs",
        launch_agent=root / "agent.plist",
    )


def test_unavailable_log_file_falls_back_to_stderr(
    tmp_path: Path,
    capsys,
) -> None:
    configure_logging(paths_for(tmp_path), console=False)

    assert "File logging is unavailable" in capsys.readouterr().err


def test_exception_traceback_is_webhook_redacted(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths = AppPaths(
        app_support=paths.app_support,
        config=paths.config,
        database=paths.database,
        lock=paths.lock,
        log_dir=paths.log_dir,
        log_file=paths.log_dir / "job-notis.log",
        launch_agent=paths.launch_agent,
    )
    configure_logging(paths, console=False)
    try:
        raise RuntimeError("failed https://discord.com/api/webhooks/123/super.secret+token=!")
    except RuntimeError:
        logging.getLogger("job_notis.test").exception("delivery failed")

    contents = paths.log_file.read_text()
    assert "super.secret+token=!" not in contents
    assert "REDACTED_DISCORD_WEBHOOK" in contents
