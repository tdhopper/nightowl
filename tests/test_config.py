from datetime import timedelta
from pathlib import Path

import pytest

from nightowl.config import load_config, parse_interval


class TestParseInterval:
    def test_hours(self):
        assert parse_interval("24h") == timedelta(hours=24)

    def test_days(self):
        assert parse_interval("7d") == timedelta(days=7)

    def test_72h(self):
        assert parse_interval("72h") == timedelta(hours=72)

    def test_invalid(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("5m")

    def test_empty(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("")


class TestLoadConfig:
    def test_valid(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: test-task
    name: "Test Task"
    interval: 24h
    prompt: "Do something"
""")
        config = load_config(cfg)
        assert config.window_start == "22:00"
        assert len(config.tasks) == 1
        assert config.tasks[0].id == "test-task"
        assert config.tasks[0].interval == timedelta(hours=24)
        assert config.tasks[0].output == "pr"

    def test_missing_schedule(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("tasks: []")
        with pytest.raises(ValueError, match="schedule"):
            load_config(cfg)

    def test_missing_task_field(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: test
    name: "Test"
""")
        with pytest.raises(ValueError, match="interval"):
            load_config(cfg)

    def test_empty_tasks(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks: []
""")
        with pytest.raises(ValueError, match="non-empty"):
            load_config(cfg)

    def test_custom_output(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: t1
    name: "T1"
    interval: 12h
    prompt: "Do it"
    output: commit
""")
        config = load_config(cfg)
        assert config.tasks[0].output == "commit"

    def test_invalid_output(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: t1
    name: "T1"
    interval: 12h
    prompt: "Do it"
    output: email
""")
        with pytest.raises(ValueError, match="Invalid output"):
            load_config(cfg)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/nightowl.yaml"))

    def test_get_task(self, tmp_path):
        cfg = tmp_path / "nightowl.yaml"
        cfg.write_text("""
schedule:
  window_start: "22:00"
  window_end: "06:00"
tasks:
  - id: alpha
    name: "Alpha"
    interval: 24h
    prompt: "Do alpha"
  - id: beta
    name: "Beta"
    interval: 48h
    prompt: "Do beta"
""")
        config = load_config(cfg)
        assert config.get_task("alpha").name == "Alpha"
        assert config.get_task("beta").name == "Beta"
        assert config.get_task("gamma") is None
