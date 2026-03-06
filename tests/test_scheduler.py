from pathlib import Path

from nightowl.scheduler import generate_plist, _project_slug


class TestScheduler:
    def test_project_slug(self):
        assert _project_slug(Path("/Users/me/my-project")) == "my-project"
        assert _project_slug(Path("/Users/me/My Project")) == "my-project"

    def test_generate_plist_structure(self, tmp_path):
        plist = generate_plist(tmp_path, "22:00", "06:00")
        assert "Label" in plist
        assert plist["ProgramArguments"] == ["nightowl", "run"]
        assert plist["WorkingDirectory"] == str(tmp_path)
        interval = plist["StartCalendarInterval"]
        assert "Hour" in interval
        assert "Minute" in interval

    def test_generate_plist_hour_in_window(self, tmp_path):
        for _ in range(20):
            plist = generate_plist(tmp_path, "22:00", "06:00")
            hour = plist["StartCalendarInterval"]["Hour"]
            assert hour >= 22 or hour < 6

    def test_generate_plist_same_day_window(self, tmp_path):
        for _ in range(20):
            plist = generate_plist(tmp_path, "09:00", "17:00")
            hour = plist["StartCalendarInterval"]["Hour"]
            assert 9 <= hour < 17
