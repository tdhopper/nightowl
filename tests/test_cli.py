from pathlib import Path

from click.testing import CliRunner

from nightowl.cli import main


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
            assert "Config file not found" in result.output

    def test_dry_run(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("nightowl.yaml").write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: test-task
    name: "Test Task"
    interval: 24h
    prompt: "Do something"
""")
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
            Path("nightowl.yaml").write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: real-task
    name: "Real Task"
    interval: 24h
    prompt: "Do something"
""")
            result = runner.invoke(main, ["run", "--task", "nonexistent"])
            assert result.exit_code == 1
            assert "not found" in result.output
