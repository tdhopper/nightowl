from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from nightowl import state


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    """Use a temporary state file for every test."""
    p = tmp_path / "state.json"
    monkeypatch.setattr(state, "STATE_PATH", p)
    return p


class TestState:
    def test_empty_state(self):
        assert state.get_task_state("/proj", "task1") is None

    def test_record_and_read(self):
        state.record_task_result("/proj", "task1", "success", pr_url="https://pr/1")
        s = state.get_task_state("/proj", "task1")
        assert s["result"] == "success"
        assert s["pr_url"] == "https://pr/1"

    def test_record_failure(self):
        state.record_task_result("/proj", "task1", "failure", error="boom")
        s = state.get_task_state("/proj", "task1")
        assert s["result"] == "failure"
        assert s["error"] == "boom"

    def test_is_eligible_no_state(self):
        assert state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_is_not_eligible_after_recent_failure(self):
        # Failures use the same interval as successes — otherwise hourly
        # cadence retries every failed task on every fire.
        state.record_task_result("/proj", "task1", "failure", error="err")
        assert not state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_is_eligible_after_failure_interval_elapsed(self):
        state.record_task_result("/proj", "task1", "failure", error="err")
        future = datetime.now() + timedelta(hours=25)
        with patch("nightowl.state.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat = datetime.fromisoformat
            assert state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_is_eligible_interval_elapsed(self):
        state.record_task_result("/proj", "task1", "success")
        # Patch datetime.now to simulate time passing
        future = datetime.now() + timedelta(hours=25)
        with patch("nightowl.state.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat = datetime.fromisoformat
            assert state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_is_not_eligible(self):
        state.record_task_result("/proj", "task1", "success")
        assert not state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_record_started_marks_ineligible(self):
        """A task marked as started consumes its interval even before completion.

        This is the crash-safety guarantee: if the runner dies between
        ``record_task_started`` and ``record_task_result``, the next launchd
        fire must not see the task as eligible — otherwise it re-runs and
        collides with the date-keyed branch from the doomed run.
        """
        state.record_task_started("/proj", "task1")
        assert not state.is_task_eligible("/proj", "task1", timedelta(hours=24))
        s = state.get_task_state("/proj", "task1")
        assert s["result"] == "started"
        assert "last_run" in s

    def test_record_result_overwrites_started(self):
        state.record_task_started("/proj", "task1")
        state.record_task_result("/proj", "task1", "success", pr_url="https://pr/1")
        s = state.get_task_state("/proj", "task1")
        assert s["result"] == "success"
        assert s["pr_url"] == "https://pr/1"

    def test_started_eligible_after_interval(self):
        """A task that started but never completed becomes eligible again
        once the interval elapses — a permanent "started" entry shouldn't
        wedge the task forever."""
        state.record_task_started("/proj", "task1")
        future = datetime.now() + timedelta(hours=25)
        with patch("nightowl.state.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat = datetime.fromisoformat
            assert state.is_task_eligible("/proj", "task1", timedelta(hours=24))

    def test_get_all_task_states(self):
        state.record_task_result("/proj", "t1", "success")
        state.record_task_result("/proj", "t2", "failure", error="err")
        all_states = state.get_all_task_states("/proj")
        assert "t1" in all_states
        assert "t2" in all_states
        assert state.get_all_task_states("/other") == {}
