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
    """Check if enough time has elapsed since the last successful run."""
    task_state = get_task_state(project_path, task_id)
    if task_state is None:
        return True
    if task_state.get("result") != "success":
        return True
    last_run = datetime.fromisoformat(task_state["last_run"])
    return datetime.now() - last_run >= interval


def get_all_task_states(project_path: str) -> dict:
    state = _read_state()
    return state.get(project_path, {})
