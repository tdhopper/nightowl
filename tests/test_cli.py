from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nightowl.cli import main


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
