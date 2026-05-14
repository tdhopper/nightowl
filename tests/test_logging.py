from __future__ import annotations

import logging
import sys

import pytest

from nightowl import logging as nl_logging


@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    """Redirect logs to a tmp directory and reset the logger between tests."""
    monkeypatch.setattr(nl_logging, "LOG_DIR", tmp_path / "logs")
    logger = logging.getLogger("nightowl")
    # Strip handlers so setup_logging re-attaches a fresh set.
    for h in list(logger.handlers):
        logger.removeHandler(h)
    yield tmp_path
    for h in list(logger.handlers):
        logger.removeHandler(h)


class TestSetupLogging:
    def test_attaches_stdout_stream_handler(self, tmp_log_dir):
        """A StreamHandler targeting stdout (not stderr) must be attached so
        launchd's StandardOutPath file is non-empty. The default
        logging.StreamHandler() targets stderr, which is why that file has
        been 0 bytes since March."""
        logger = nl_logging.setup_logging()

        stream_handlers = [
            h for h in logger.handlers if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) >= 1, (
            f"expected at least one stdout StreamHandler, got {logger.handlers}"
        )
        stdout_handlers = [h for h in stream_handlers if h.stream is sys.stdout]
        assert stdout_handlers, (
            "expected at least one StreamHandler bound to sys.stdout; "
            f"stream handlers point to: {[h.stream for h in stream_handlers]}"
        )

    def test_file_handler_attached(self, tmp_log_dir):
        logger = nl_logging.setup_logging()
        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert file_handlers, "expected a FileHandler for the daily log file"
