"""Per-run history log.

Each task run appends one JSON line to a per-project ``runs.jsonl`` under
``~/.local/share/nightowl/runs/<project-slug>.jsonl``. The file is
append-only and is independent from ``state.json`` so that history grows
without bloating the small last-run-per-task state that eligibility
checks read on every fire.

The schema is the dict written by ``append_run`` (see its docstring).
Consumers should treat unknown keys as forward-compatible additions and
``None`` values as "claude crashed before emitting JSON" — the runner
records the best-effort fields it has and moves on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

RUNS_ROOT = Path.home() / ".local" / "share" / "nightowl" / "runs"


def _project_slug(project_path: str) -> str:
    """Slug a project path the same way ``runner._worktree_path`` does."""
    name = Path(project_path).name
    return re.sub(r"[^a-zA-Z0-9]", "-", name).strip("-").lower()


def runs_path(project_path: str) -> Path:
    """Return the runs.jsonl path for a project."""
    return RUNS_ROOT / f"{_project_slug(project_path)}.jsonl"


def append_run(project_path: str, record: dict) -> None:
    """Append one task run to the per-project ``runs.jsonl`` file.

    ``record`` is written as a single JSON line. The caller owns the
    schema; the typical shape is::

        {
            "task_id": "...",
            "started_at": "2026-05-13T22:00:00",
            "ended_at":   "2026-05-13T22:19:24",
            "duration_s": 1164,
            "result": "success",        # or "failure"
            "pr_url": "https://...",    # or None
            "error":  "first line",     # or None
            "claude_cost_usd": 9.98,    # or None if parsing failed
            "claude_input_tokens": 156,
            "claude_output_tokens": 62085,
            "claude_cache_read_tokens": 12174649,
        }
    """
    path = runs_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_runs(project_path: str) -> list[dict]:
    """Read all runs for a project. Returns an empty list if no file exists."""
    path = runs_path(project_path)
    if not path.exists():
        return []
    runs: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate a malformed line rather than wedging the reader.
                continue
    return runs
