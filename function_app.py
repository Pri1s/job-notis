"""Azure Functions Python v2 entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import azure.functions as func

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from job_notis.azure_function import run_timer_check  # noqa: E402

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%JOB_NOTIS_TIMER_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def job_notis_timer(timer: func.TimerRequest) -> None:
    """Run Job Notis on its durable Azure timer."""

    del timer
    run_timer_check()
