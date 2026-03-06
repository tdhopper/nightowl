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

    def test_is_eligible_after_failure(self):
        state.record_task_result("/proj", "task1", "failure", error="err")
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

    def test_get_all_task_states(self):
        state.record_task_result("/proj", "t1", "success")
        state.record_task_result("/proj", "t2", "failure", error="err")
        all_states = state.get_all_task_states("/proj")
        assert "t1" in all_states
        assert "t2" in all_states
        assert state.get_all_task_states("/other") == {}
