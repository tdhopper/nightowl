from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import yaml


def parse_interval(s: str) -> timedelta:
    """Parse an interval string like '24h', '72h', or '7d' into a timedelta."""
    m = re.fullmatch(r"(\d+)([hd])", s.strip())
    if not m:
        raise ValueError(f"Invalid interval format: {s!r}. Expected e.g. '24h' or '7d'.")
    value, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


class Task:
    def __init__(self, id: str, name: str, interval: timedelta, prompt: str, output: str = "pr"):
        self.id = id
        self.name = name
        self.interval = interval
        self.prompt = prompt
        if output not in ("pr", "commit", "none"):
            raise ValueError(f"Invalid output type: {output!r}. Must be 'pr', 'commit', or 'none'.")
        self.output = output


class Config:
    def __init__(self, window_start: str, window_end: str, tasks: list[Task]):
        self.window_start = window_start
        self.window_end = window_end
        self.tasks = tasks

    def get_task(self, task_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None


def load_config(path: Path | None = None) -> Config:
    """Load and validate nightowl.yaml from the given path or CWD."""
    if path is None:
        path = Path.cwd() / "nightowl.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping.")

    schedule = data.get("schedule")
    if not isinstance(schedule, dict):
        raise ValueError("Config must have a 'schedule' mapping.")
    for key in ("window_start", "window_end"):
        if key not in schedule:
            raise ValueError(f"schedule.{key} is required.")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) == 0:
        raise ValueError("Config must have a non-empty 'tasks' list.")

    tasks = []
    for i, t in enumerate(raw_tasks):
        for key in ("id", "name", "interval", "prompt"):
            if key not in t:
                raise ValueError(f"tasks[{i}].{key} is required.")
        tasks.append(
            Task(
                id=t["id"],
                name=t["name"],
                interval=parse_interval(t["interval"]),
                prompt=t["prompt"],
                output=t.get("output", "pr"),
            )
        )

    return Config(
        window_start=schedule["window_start"],
        window_end=schedule["window_end"],
        tasks=tasks,
    )
