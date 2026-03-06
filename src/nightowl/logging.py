from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path.home() / ".local" / "share" / "nightowl" / "logs"


def setup_logging() -> logging.Logger:
    """Set up file + console logging, clean old logs."""
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

        ch = logging.StreamHandler()
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
