from datetime import timedelta
from pathlib import Path

import pytest

from nightowl.config import (
    SkipIfOpenCheck,
    load_config,
    parse_frontmatter,
    parse_interval,
    parse_weekday,
)


def _make_project(tmp_path, tasks: dict[str, str], schedule: str | None = None) -> Path:
    """Create a nightowl/ directory with a schedule and task files."""
    nightowl_dir = tmp_path / "nightowl"
    nightowl_dir.mkdir()
    if schedule is None:
        schedule = (
            "---\n"
            'window_start: "22:00"\n'
            'window_end: "06:00"\n'
            "---\n"
        )
    (nightowl_dir / "_schedule.md").write_text(schedule)
    for name, content in tasks.items():
        (nightowl_dir / name).write_text(content)
    return nightowl_dir


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


class TestParseWeekday:
    def test_lowercase(self):
        assert parse_weekday("monday") == 0

    def test_capitalized(self):
        assert parse_weekday("Sunday") == 6

    def test_uppercase(self):
        assert parse_weekday("SATURDAY") == 5

    def test_invalid(self):
        with pytest.raises(ValueError, match="Invalid weekday"):
            parse_weekday("someday")


class TestParseFrontmatter:
    def test_basic(self):
        fm, body = parse_frontmatter("---\nfoo: bar\n---\nhello\n")
        assert fm == {"foo": "bar"}
        assert body == "hello\n"

    def test_empty_body(self):
        fm, body = parse_frontmatter("---\nfoo: bar\n---\n")
        assert fm == {"foo": "bar"}
        assert body == ""

    def test_no_opening_fence(self):
        with pytest.raises(ValueError, match="must start with"):
            parse_frontmatter("just some text")

    def test_unclosed_fence(self):
        with pytest.raises(ValueError, match="fence not closed"):
            parse_frontmatter("---\nfoo: bar\nno closing fence\n")

    def test_non_mapping_frontmatter(self):
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            parse_frontmatter("---\n- a\n- b\n---\n")


class TestLoadConfig:
    def test_valid(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "test-task.md": (
                    "---\n"
                    'name: "Test Task"\n'
                    "interval: 24h\n"
                    "---\n"
                    "Do something\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        assert config.window_start == "22:00"
        assert config.window_end == "06:00"
        assert config.skip_weekdays == []
        assert len(config.tasks) == 1
        assert config.tasks[0].id == "test-task"
        assert config.tasks[0].name == "Test Task"
        assert config.tasks[0].interval == timedelta(hours=24)
        assert config.tasks[0].output == "pr"
        assert config.tasks[0].prompt == "Do something"

    def test_task_id_from_filename(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "my-cool-task.md": (
                    "---\n"
                    'name: "X"\n'
                    "interval: 1h\n"
                    "---\n"
                    "body\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        assert config.tasks[0].id == "my-cool-task"

    def test_underscore_prefixed_files_skipped(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "real-task.md": (
                    "---\n"
                    'name: "Real"\n'
                    "interval: 24h\n"
                    "---\n"
                    "do it\n"
                ),
                "_notes.md": "not a task at all",
            },
        )
        config = load_config(nightowl_dir)
        assert [t.id for t in config.tasks] == ["real-task"]

    def test_missing_schedule_file(self, tmp_path):
        nightowl_dir = tmp_path / "nightowl"
        nightowl_dir.mkdir()
        (nightowl_dir / "t.md").write_text(
            "---\nname: T\ninterval: 1h\n---\nx\n"
        )
        with pytest.raises(FileNotFoundError, match="Schedule file"):
            load_config(nightowl_dir)

    def test_missing_window_start(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 1h\n---\nx\n"},
            schedule='---\nwindow_end: "06:00"\n---\n',
        )
        with pytest.raises(ValueError, match="window_start"):
            load_config(nightowl_dir)

    def test_missing_task_field(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\n---\nx\n"},
        )
        with pytest.raises(ValueError, match="interval"):
            load_config(nightowl_dir)

    def test_empty_body_is_error(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 1h\n---\n   \n"},
        )
        with pytest.raises(ValueError, match="prompt"):
            load_config(nightowl_dir)

    def test_no_tasks(self, tmp_path):
        nightowl_dir = tmp_path / "nightowl"
        nightowl_dir.mkdir()
        (nightowl_dir / "_schedule.md").write_text(
            '---\nwindow_start: "22:00"\nwindow_end: "06:00"\n---\n'
        )
        with pytest.raises(ValueError, match="No task files"):
            load_config(nightowl_dir)

    def test_custom_output(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 12h\n"
                    "output: commit\n"
                    "---\n"
                    "Do it\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        assert config.tasks[0].output == "commit"

    def test_invalid_output(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 12h\n"
                    "output: email\n"
                    "---\n"
                    "Do it\n"
                ),
            },
        )
        with pytest.raises(ValueError, match="Invalid output"):
            load_config(nightowl_dir)

    def test_directory_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/nightowl"))

    def test_fact_check_defaults_false(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
        )
        config = load_config(nightowl_dir)
        assert config.tasks[0].fact_check is False

    def test_fact_check_true(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    "name: T\n"
                    "interval: 24h\n"
                    "fact_check: true\n"
                    "---\n"
                    "Do it\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        assert config.tasks[0].fact_check is True

    def test_skip_if_open_defaults_empty(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
        )
        config = load_config(nightowl_dir)
        assert config.tasks[0].skip_if_open == []

    def test_skip_if_open_list_of_strings(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open:\n"
                    "  - pr-branch-prefix\n"
                    "  - issue-title\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        checks = config.tasks[0].skip_if_open
        assert len(checks) == 2
        assert checks[0].type == "pr-branch-prefix"
        assert checks[0].value is None
        assert checks[0].threshold == 1
        assert checks[1].type == "issue-title"

    def test_skip_if_open_mapping_with_value_and_threshold(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open:\n"
                    "  - type: issue-label\n"
                    "    value: source:competitive-analysis\n"
                    "    threshold: 5\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        config = load_config(nightowl_dir)
        checks = config.tasks[0].skip_if_open
        assert len(checks) == 1
        assert checks[0].type == "issue-label"
        assert checks[0].value == "source:competitive-analysis"
        assert checks[0].threshold == 5

    def test_skip_if_open_invalid_type(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open: [made-up-type]\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        with pytest.raises(ValueError, match="skip_if_open"):
            load_config(nightowl_dir)

    def test_skip_if_open_not_a_list(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open: pr-branch-prefix\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        with pytest.raises(ValueError, match="must be a list"):
            load_config(nightowl_dir)

    def test_skip_if_open_missing_type_in_mapping(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open:\n"
                    "  - value: foo\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        with pytest.raises(ValueError, match="'type' is required"):
            load_config(nightowl_dir)

    def test_skip_if_open_threshold_zero_rejected(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "t.md": (
                    "---\n"
                    'name: "T"\n'
                    "interval: 24h\n"
                    "skip_if_open:\n"
                    "  - type: pr-branch-prefix\n"
                    "    threshold: 0\n"
                    "---\n"
                    "do it\n"
                ),
            },
        )
        with pytest.raises(ValueError, match="threshold"):
            load_config(nightowl_dir)

    def test_get_task(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {
                "alpha.md": "---\nname: Alpha\ninterval: 24h\n---\nA\n",
                "beta.md": "---\nname: Beta\ninterval: 48h\n---\nB\n",
            },
        )
        config = load_config(nightowl_dir)
        assert config.get_task("alpha").name == "Alpha"
        assert config.get_task("beta").name == "Beta"
        assert config.get_task("gamma") is None

    def test_skip_weekdays(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
            schedule=(
                "---\n"
                'window_start: "22:00"\n'
                'window_end: "06:00"\n'
                "skip_weekdays: [Sunday]\n"
                "---\n"
            ),
        )
        config = load_config(nightowl_dir)
        assert config.skip_weekdays == [6]

    def test_cadence_default_daily(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
        )
        config = load_config(nightowl_dir)
        assert config.cadence == "daily"

    def test_cadence_hourly(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
            schedule=(
                "---\n"
                'window_start: "22:00"\n'
                'window_end: "06:00"\n'
                "cadence: hourly\n"
                "---\n"
            ),
        )
        config = load_config(nightowl_dir)
        assert config.cadence == "hourly"

    def test_cadence_invalid(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
            schedule=(
                "---\n"
                'window_start: "22:00"\n'
                'window_end: "06:00"\n'
                "cadence: weekly\n"
                "---\n"
            ),
        )
        with pytest.raises(ValueError, match="cadence"):
            load_config(nightowl_dir)

    def test_skip_weekdays_invalid(self, tmp_path):
        nightowl_dir = _make_project(
            tmp_path,
            {"t.md": "---\nname: T\ninterval: 24h\n---\nDo it\n"},
            schedule=(
                "---\n"
                'window_start: "22:00"\n'
                'window_end: "06:00"\n'
                "skip_weekdays: [Funday]\n"
                "---\n"
            ),
        )
        with pytest.raises(ValueError, match="Invalid weekday"):
            load_config(nightowl_dir)
