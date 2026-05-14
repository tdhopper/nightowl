from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path.home() / ".local" / "share" / "nightowl" / "logs"


class _StdoutStreamHandler(logging.StreamHandler):
    """StreamHandler that resolves ``sys.stdout`` at emit time.

    A plain ``logging.StreamHandler(stream=sys.stdout)`` captures the stdout
    object at construction. Under test runners (and any framework that
    swaps sys.stdout per call, like Click's CliRunner), that reference
    quickly becomes a closed file and every emit raises a logging error.
    Resolving the stream lazily keeps the handler valid across calls.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):
        # Ignore writes from the base class; we resolve dynamically.
        pass


def setup_logging() -> logging.Logger:
    """Set up file + stdout logging, clean old logs.

    The launchd plist's ``StandardOutPath`` / ``StandardErrorPath`` control
    where the subprocess's raw stdout/stderr go; that's a packaging concern,
    not a logging-module one, so this function does not rotate those files.
    The per-day file under ``LOG_DIR`` is what holds the structured log; the
    stdout handler attached here is what makes ``StandardOutPath`` non-empty
    so users see something when they tail the launchd stdout file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _clean_old_logs()

    log_file = LOG_DIR / f"nightowl-{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger("nightowl")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)

        # Explicitly target stdout so launchd's StandardOutPath captures runs.
        # The default StreamHandler() writes to stderr, which is why the
        # launchd stdout file has been 0 bytes since March.
        ch = _StdoutStreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)

    return logger


def _clean_old_logs(max_age_days: int = 30) -> None:
    cutoff = datetime.now() - timedelta(days=max_age_days)
    for f in LOG_DIR.glob("nightowl-*.log"):
        try:
            date_str = f.stem.removeprefix("nightowl-")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
        except ValueError:
            pass
