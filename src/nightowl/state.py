from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "nightowl" / "state.json"


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def get_task_state(project_path: str, task_id: str) -> dict | None:
    state = _read_state()
    return state.get(project_path, {}).get(task_id)


def record_task_started(project_path: str, task_id: str) -> None:
    """Mark a task as started so a crash mid-run doesn't refire on the next launchd hour.

    Eligibility uses ``last_run`` regardless of result. Writing a marker
    here means a task that crashes or gets killed (launchd timeout, OOM,
    system reboot) still consumes its interval. Otherwise the same task
    fires every launchd hour until something records terminal state, which
    collides with the date-keyed branch name from the crashed run and
    produces a stream of ``gh pr create ... already exists`` failures.
    Terminal state from ``record_task_result`` overwrites this on completion.
    """
    state = _read_state()
    project = state.setdefault(project_path, {})
    project[task_id] = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "result": "started",
    }
    _write_state(state)


def record_task_result(
    project_path: str,
    task_id: str,
    result: str,
    pr_url: str | None = None,
    error: str | None = None,
) -> None:
    state = _read_state()
    project = state.setdefault(project_path, {})
    entry: dict = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "result": result,
    }
    if pr_url:
        entry["pr_url"] = pr_url
    if error:
        entry["error"] = error
    project[task_id] = entry
    _write_state(state)


def is_task_eligible(project_path: str, task_id: str, interval: timedelta) -> bool:
    """Check if enough time has elapsed since the last run.

    The same interval applies whether the previous run succeeded or failed —
    otherwise a failed task retries on every launchd fire, which storms the
    queue under hourly cadence. Failed runs surface in ``nightowl status``;
    use ``nightowl run --task <id>`` to retry manually before the interval
    elapses.
    """
    task_state = get_task_state(project_path, task_id)
    if task_state is None:
        return True
    last_run = datetime.fromisoformat(task_state["last_run"])
    return datetime.now() - last_run >= interval


def get_all_task_states(project_path: str) -> dict:
    state = _read_state()
    return state.get(project_path, {})
