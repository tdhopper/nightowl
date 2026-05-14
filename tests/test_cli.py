from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nightowl import state
from nightowl.cli import main


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    """Isolate state writes from the real state.json for every CLI test."""
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "cli-state.json")


@pytest.fixture(autouse=True)
def _isolate_nightowl_logger():
    """Strip handlers from the nightowl logger before & after each CLI test.

    `nightowl run` calls setup_logging() which attaches handlers; without
    teardown, those handlers persist across tests and emit into Click's
    captured stdout for unrelated invocations like `status`."""
    import logging as _logging
    lg = _logging.getLogger("nightowl")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    yield
    for h in list(lg.handlers):
        lg.removeHandler(h)


SCHEDULE = (
    "---\n"
    'window_start: "22:00"\n'
    'window_end: "06:00"\n'
    "---\n"
)

SKIP_SCHEDULE = (
    "---\n"
    'window_start: "22:00"\n'
    'window_end: "06:00"\n'
    "skip_weekdays: [Sunday]\n"
    "---\n"
)

TASK = (
    "---\n"
    'name: "Test Task"\n'
    "interval: 24h\n"
    "---\n"
    "Do something\n"
)


def _write_project(schedule: str = SCHEDULE, task_id: str = "test-task") -> None:
    Path("nightowl").mkdir()
    (Path("nightowl") / "_schedule.md").write_text(schedule)
    (Path("nightowl") / f"{task_id}.md").write_text(TASK)


class TestCli:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "nightowl" in result.output

    def test_run_no_config(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["run"])
            assert result.exit_code == 1
            assert "nightowl config directory not found" in result.output

    def test_dry_run(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            result = runner.invoke(main, ["run", "--dry-run"])
            assert result.exit_code == 0
            assert "test-task" in result.output

    def test_status_no_config(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 1

    def test_run_unknown_task(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project(task_id="real-task")
            result = runner.invoke(main, ["run", "--task", "nonexistent"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_run_skipped_weekday(self, tmp_path):
        """On a skipped weekday, `run` exits without running tasks."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project(schedule=SKIP_SCHEDULE)
            # Freeze datetime.now to a Sunday (2026-04-19 is a Sunday)
            sunday = datetime(2026, 4, 19, 0, 18)
            with patch("nightowl.cli.datetime") as mock_dt:
                mock_dt.now.return_value = sunday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                result = runner.invoke(main, ["run"])
            assert result.exit_code == 0

    def test_run_skipped_weekday_bypassed_by_task_flag(self, tmp_path):
        """Explicit --task bypasses the weekday skip."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project(schedule=SKIP_SCHEDULE, task_id="test-task")
            sunday = datetime(2026, 4, 19, 0, 18)
            with patch("nightowl.cli.datetime") as mock_dt:
                mock_dt.now.return_value = sunday
                # Invoking --task with a name that doesn't exist should still
                # reach the task-lookup code path (proving the skip didn't
                # short-circuit it).
                result = runner.invoke(main, ["run", "--task", "bogus"])
            assert result.exit_code == 1
            assert "not found" in result.output


class TestDisappearedDetection:
    def test_run_marks_disappeared_task(self, tmp_path):
        """A task in state but absent from loaded config gets marked disappeared."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project(task_id="real-task")
            project_path = str(Path.cwd())
            # Seed state with a now-deleted task id.
            state.record_task_result(project_path, "ghost", "success")

            # --task targets the real task so we don't actually invoke claude.
            with patch("nightowl.cli.run_task") as mock_run:
                mock_run.return_value = {"result": "success"}
                result = runner.invoke(main, ["run", "--task", "real-task"])

            assert result.exit_code == 0
            ghost = state.get_task_state(project_path, "ghost")
            assert ghost["result"] == "disappeared"
            assert "noticed_at" in ghost

    def test_status_shows_disappeared_tasks(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project(task_id="real-task")
            project_path = str(Path.cwd())
            state.record_task_result(project_path, "ghost", "success")
            state.record_task_disappeared(project_path, "ghost")

            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "ghost" in result.output
            assert "disappeared" in result.output


class TestStatusStale:
    def test_no_state_is_not_stale(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            result = runner.invoke(main, ["status", "--stale"])
            assert result.exit_code == 0
            assert result.output == ""

    def test_recent_run_is_not_stale(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            project_path = str(Path.cwd())
            state.record_task_result(project_path, "test-task", "success")
            result = runner.invoke(main, ["status", "--stale"])
            assert result.exit_code == 0
            assert result.output == ""

    def test_stale_run_exits_nonzero(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            project_path = str(Path.cwd())
            # Interval is 24h; record a run from 3 days ago (> 2 * 24h)
            old = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
            state._write_state({
                project_path: {
                    "test-task": {"last_run": old, "result": "success"}
                }
            })
            result = runner.invoke(main, ["status", "--stale"])
            assert result.exit_code == 1
            assert "test-task" in result.output

    def test_disappeared_task_is_stale(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            project_path = str(Path.cwd())
            # Disappeared task NOT in current config
            state.record_task_disappeared(project_path, "ghost")
            result = runner.invoke(main, ["status", "--stale"])
            assert result.exit_code == 1
            assert "ghost" in result.output
            assert "disappeared" in result.output

    def test_disappeared_loaded_task_is_stale(self, tmp_path):
        """A loaded task whose state says 'disappeared' (edge case) still flags."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _write_project()
            project_path = str(Path.cwd())
            state._write_state({
                project_path: {
                    "test-task": {"result": "disappeared", "noticed_at": "2026-01-01T00:00:00"},
                }
            })
            result = runner.invoke(main, ["status", "--stale"])
            assert result.exit_code == 1
            assert "test-task" in result.output
