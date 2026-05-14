from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from nightowl import email as email_mod


@pytest.fixture(autouse=True)
def tmp_resend_key(tmp_path, monkeypatch):
    """Isolate the resend key path from ~/.config/resend/key."""
    key_path = tmp_path / "key"
    monkeypatch.setattr(email_mod, "RESEND_KEY_PATH", key_path)
    return key_path


@pytest.fixture
def logger():
    return logging.getLogger("test")


class TestBuildSummary:
    def test_subject_counts(self):
        runs = [
            {"task_id": "a", "result": "success", "duration_s": 60},
            {"task_id": "b", "result": "failure", "duration_s": 10},
            {"task_id": "c", "result": "success", "duration_s": 0},
        ]
        now = datetime(2026, 5, 13, 23, 5)
        subject, _ = email_mod.build_summary(runs, now=now)
        assert subject == "[nightowl] run 2026-05-13 23:05 -- 3 ran, 1 failed"

    def test_body_includes_task_ids(self):
        runs = [
            {"task_id": "content-gap", "result": "success", "duration_s": 1164,
             "pr_url": "https://github.com/tdhopper/handbook/pull/42"},
            {"task_id": "reddit-scout", "result": "failure",
             "duration_s": 5, "error": "ENOTFOUND api.reddit.com\nstack trace"},
        ]
        _, body = email_mod.build_summary(runs, project_dir="/repos/handbook")
        assert "content-gap" in body
        assert "reddit-scout" in body
        assert "https://github.com/tdhopper/handbook/pull/42" in body
        # Only the first line of the multi-line error
        assert "ENOTFOUND api.reddit.com" in body
        assert "stack trace" not in body
        assert "/repos/handbook" in body

    def test_total_cost_in_footer_when_present(self):
        runs = [
            {"task_id": "a", "result": "success", "duration_s": 60,
             "claude_cost_usd": 0.50},
            {"task_id": "b", "result": "success", "duration_s": 120,
             "claude_cost_usd": 1.25},
        ]
        _, body = email_mod.build_summary(runs)
        assert "$1.75" in body

    def test_no_cost_footer_when_all_none(self):
        runs = [
            {"task_id": "a", "result": "failure", "duration_s": 1,
             "claude_cost_usd": None},
        ]
        _, body = email_mod.build_summary(runs)
        # Cost line should not appear at all when nothing was parsed
        assert "claude cost" not in body

    def test_error_html_escaped(self):
        runs = [
            {"task_id": "a", "result": "failure", "duration_s": 1,
             "error": "<script>alert(1)</script>"},
        ]
        _, body = email_mod.build_summary(runs)
        assert "<script>" not in body
        assert "&lt;script&gt;" in body


class TestSendSummaryEmail:
    def test_no_runs_skips(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("test-key")
        with patch("nightowl.email.subprocess.run") as mock_run:
            sent = email_mod.send_summary_email([], logger)
            assert sent is False
            mock_run.assert_not_called()

    def test_missing_key_skips(self, logger, tmp_resend_key):
        # Key file does not exist
        assert not tmp_resend_key.exists()
        with patch("nightowl.email.subprocess.run") as mock_run:
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is False
            mock_run.assert_not_called()

    def test_empty_key_file_skips(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("")
        with patch("nightowl.email.subprocess.run") as mock_run:
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is False
            mock_run.assert_not_called()

    def test_invokes_resend_cli(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("rk_test_123")
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        with patch("nightowl.email.subprocess.run", return_value=proc) as mock_run:
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is True
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert cmd[:3] == ["resend", "emails", "send"]
            assert "--from" in cmd
            assert "Nightowl <claude@ehop.me>" in cmd
            assert "--to" in cmd
            assert "t@ehop.me" in cmd
            # API key passed via env, not env var on subject line
            env = mock_run.call_args.kwargs["env"]
            assert env["RESEND_API_KEY"] == "rk_test_123"

    def test_resend_failure_does_not_raise(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("rk_test_123")
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "rate limited"
        with patch("nightowl.email.subprocess.run", return_value=proc):
            # Must NOT raise — email failures are not run failures.
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is False

    def test_resend_cli_missing_does_not_raise(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("rk_test_123")
        with patch(
            "nightowl.email.subprocess.run",
            side_effect=FileNotFoundError("resend"),
        ):
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is False

    def test_resend_timeout_does_not_raise(self, logger, tmp_resend_key):
        tmp_resend_key.write_text("rk_test_123")
        with patch(
            "nightowl.email.subprocess.run",
            side_effect=subprocess.TimeoutExpired("resend", 60),
        ):
            sent = email_mod.send_summary_email(
                [{"task_id": "a", "result": "success", "duration_s": 1}],
                logger,
            )
            assert sent is False
