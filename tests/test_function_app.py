from __future__ import annotations

import function_app


def test_timer_entrypoint_dispatches(monkeypatch) -> None:
    calls = 0

    def run() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(function_app, "run_timer_check", run)

    function_app.job_notis_timer(object())

    assert calls == 1
