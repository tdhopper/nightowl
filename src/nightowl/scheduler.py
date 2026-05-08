from __future__ import annotations

import plistlib
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


def _project_slug(project_dir: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", project_dir.name).strip("-").lower()


def _plist_path(project_dir: Path) -> Path:
    slug = _project_slug(project_dir)
    return PLIST_DIR / f"com.nightowl.{slug}.plist"


def _parse_hour(time_str: str) -> int:
    return int(time_str.split(":")[0])


def generate_plist(
    project_dir: Path,
    window_start: str,
    window_end: str,
    cadence: str = "daily",
) -> dict:
    """Generate a launchd plist dict for the project.

    With ``cadence='daily'`` (default), the job fires once per day at a random
    hour:minute inside ``[window_start, window_end)``.

    With ``cadence='hourly'``, the job fires every hour at a random minute. The
    window is ignored in this mode — eligibility is governed by per-task
    intervals and ``skip_weekdays``.
    """
    slug = _project_slug(project_dir)
    label = f"com.nightowl.{slug}"

    minute = random.randint(0, 59)

    if cadence == "hourly":
        start_calendar_interval: dict = {"Minute": minute}
    else:
        start_hour = _parse_hour(window_start)
        end_hour = _parse_hour(window_end)
        # Handle overnight windows (e.g. 22:00 - 06:00)
        if end_hour <= start_hour:
            hours = list(range(start_hour, 24)) + list(range(0, end_hour))
        else:
            hours = list(range(start_hour, end_hour))
        hour = random.choice(hours)
        start_calendar_interval = {"Hour": hour, "Minute": minute}

    # Resolve full path so launchd can find the binary (its PATH is minimal)
    nightowl_bin = shutil.which("nightowl") or str(Path(sys.prefix) / "bin" / "nightowl")

    # Capture current PATH so launchd subprocesses (git, claude, gh) are findable
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    plist = {
        "Label": label,
        "ProgramArguments": [nightowl_bin, "run"],
        "WorkingDirectory": str(project_dir),
        "EnvironmentVariables": {
            "PATH": path,
            "HOME": str(Path.home()),
        },
        "StartCalendarInterval": start_calendar_interval,
        "StandardOutPath": str(
            Path.home() / ".local" / "share" / "nightowl" / "logs" / f"{slug}-stdout.log"
        ),
        "StandardErrorPath": str(
            Path.home() / ".local" / "share" / "nightowl" / "logs" / f"{slug}-stderr.log"
        ),
    }
    return plist


def install(
    project_dir: Path,
    window_start: str,
    window_end: str,
    cadence: str = "daily",
) -> Path:
    """Generate and install the launchd plist."""
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _plist_path(project_dir)
    plist = generate_plist(project_dir, window_start, window_end, cadence=cadence)

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    return plist_path


def uninstall(project_dir: Path) -> None:
    """Unload and remove the launchd plist."""
    plist_path = _plist_path(project_dir)
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        plist_path.unlink()
