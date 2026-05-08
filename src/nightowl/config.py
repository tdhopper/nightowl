from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import yaml


WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

WEEKDAY_DISPLAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_interval(s: str) -> timedelta:
    """Parse an interval string like '24h', '72h', or '7d' into a timedelta."""
    m = re.fullmatch(r"(\d+)([hd])", s.strip())
    if not m:
        raise ValueError(f"Invalid interval format: {s!r}. Expected e.g. '24h' or '7d'.")
    value, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def parse_weekday(name: object) -> int:
    """Parse a weekday name into a Python weekday int (Monday=0, Sunday=6)."""
    key = str(name).strip().lower()
    if key not in WEEKDAY_NAMES:
        raise ValueError(
            f"Invalid weekday name: {name!r}. Expected one of: "
            f"{', '.join(WEEKDAY_DISPLAY)}"
        )
    return WEEKDAY_NAMES[key]


def parse_frontmatter(text: str, source: str = "<string>") -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ValueError(f"{source}: file must start with '---' YAML frontmatter fence.")
    # Find closing fence
    lines = text.splitlines(keepends=True)
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(f"{source}: frontmatter fence not closed (expected '---' on its own line).")
    fm_text = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1:])
    frontmatter = yaml.safe_load(fm_text) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"{source}: frontmatter must be a YAML mapping.")
    return frontmatter, body


class Task:
    def __init__(
        self,
        id: str,
        name: str,
        interval: timedelta,
        prompt: str,
        output: str = "pr",
        fact_check: bool = False,
    ):
        self.id = id
        self.name = name
        self.interval = interval
        self.prompt = prompt
        if output not in ("pr", "commit", "none"):
            raise ValueError(f"Invalid output type: {output!r}. Must be 'pr', 'commit', or 'none'.")
        self.output = output
        self.fact_check = fact_check


CADENCES = ("daily", "hourly")


class Config:
    def __init__(
        self,
        window_start: str,
        window_end: str,
        tasks: list[Task],
        skip_weekdays: list[int] | None = None,
        cadence: str = "daily",
    ):
        self.window_start = window_start
        self.window_end = window_end
        self.tasks = tasks
        self.skip_weekdays = skip_weekdays or []
        if cadence not in CADENCES:
            raise ValueError(
                f"Invalid cadence: {cadence!r}. Must be one of: {', '.join(CADENCES)}."
            )
        self.cadence = cadence

    def get_task(self, task_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None


def _load_schedule(path: Path) -> dict:
    fm, _ = parse_frontmatter(path.read_text(), source=path.name)
    for key in ("window_start", "window_end"):
        if key not in fm:
            raise ValueError(f"{path.name}: '{key}' is required.")
    raw_skip = fm.get("skip_weekdays", [])
    if not isinstance(raw_skip, list):
        raise ValueError(f"{path.name}: 'skip_weekdays' must be a list.")
    skip_weekdays = [parse_weekday(d) for d in raw_skip]
    cadence = fm.get("cadence", "daily")
    if cadence not in CADENCES:
        raise ValueError(
            f"{path.name}: 'cadence' must be one of: {', '.join(CADENCES)}."
        )
    return {
        "window_start": fm["window_start"],
        "window_end": fm["window_end"],
        "skip_weekdays": skip_weekdays,
        "cadence": cadence,
    }


def _load_task(path: Path) -> Task:
    fm, body = parse_frontmatter(path.read_text(), source=path.name)
    for key in ("name", "interval"):
        if key not in fm:
            raise ValueError(f"{path.name}: '{key}' is required.")
    prompt = body.strip()
    if not prompt:
        raise ValueError(f"{path.name}: task prompt (markdown body) is empty.")
    return Task(
        id=path.stem,
        name=fm["name"],
        interval=parse_interval(fm["interval"]),
        prompt=prompt,
        output=fm.get("output", "pr"),
        fact_check=bool(fm.get("fact_check", False)),
    )


def load_config(path: Path | None = None) -> Config:
    """Load nightowl config from a `nightowl/` directory.

    The directory must contain:

    - `_schedule.md`: YAML frontmatter with `window_start`, `window_end`, and
      optional `skip_weekdays` (list of weekday names). Body is ignored.
    - `<task-id>.md`: one per task. Frontmatter has `name`, `interval`, and
      optional `output` (default "pr") and `fact_check` (default false). The
      markdown body is the task prompt. The filename stem becomes the task id.
    - Any other `_*.md` file is reserved and ignored.
    """
    if path is None:
        path = Path.cwd() / "nightowl"
    if not path.is_dir():
        raise FileNotFoundError(f"nightowl config directory not found: {path}")

    schedule_path = path / "_schedule.md"
    if not schedule_path.exists():
        raise FileNotFoundError(f"Schedule file not found: {schedule_path}")

    schedule = _load_schedule(schedule_path)

    tasks = []
    for md in sorted(path.glob("*.md")):
        if md.name.startswith("_"):
            continue
        tasks.append(_load_task(md))

    if not tasks:
        raise ValueError(f"No task files found in {path}")

    return Config(
        window_start=schedule["window_start"],
        window_end=schedule["window_end"],
        tasks=tasks,
        skip_weekdays=schedule["skip_weekdays"],
        cadence=schedule["cadence"],
    )
