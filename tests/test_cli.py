from __future__ import annotations

import stat
from pathlib import Path

from job_notis.cli import run
from job_notis.config import load_config
from job_notis.paths import AppPaths


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


def test_configure_prompts_without_command_line_secret(monkeypatch, tmp_path: Path, capsys) -> None:
    paths = paths_for(tmp_path)
    monkeypatch.setattr(
        "job_notis.cli.getpass.getpass",
        lambda _: "https://discord.com/api/webhooks/123/token",
    )

    assert run(["configure"], paths=paths) == 0

    assert load_config(paths).discord_webhook_url.endswith("/token")
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600
    assert "token" not in capsys.readouterr().out


def test_status_before_setup_is_actionable(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        "job_notis.cli.launch_agent_status",
        lambda _paths: (False, "not installed"),
    )
    result = run(["status"], paths=paths_for(tmp_path))
    output = capsys.readouterr().out
    assert result == 1
    assert "Configured: no" in output
    assert "Initialized: no" in output
    assert "Scheduler: not installed" in output


def test_unexpected_error_redacts_webhook_from_stderr(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    secret = "https://discord.com/api/webhooks/123/super.secret-token"

    def fail(_paths: AppPaths) -> int:
        raise RuntimeError(f"request failed for {secret}")

    monkeypatch.setattr("job_notis.cli._status", fail)

    assert run(["status"], paths=paths_for(tmp_path)) == 1
    error = capsys.readouterr().err
    assert "super.secret-token" not in error
    assert "REDACTED_DISCORD_WEBHOOK" in error
