from pathlib import Path

from nightowl.scheduler import generate_plist, _project_slug


class TestScheduler:
    def test_project_slug(self):
        assert _project_slug(Path("/Users/me/my-project")) == "my-project"
        assert _project_slug(Path("/Users/me/My Project")) == "my-project"

    def test_generate_plist_structure(self, tmp_path):
        plist = generate_plist(tmp_path, "22:00", "06:00")
        assert "Label" in plist
        assert len(plist["ProgramArguments"]) == 2
        assert plist["ProgramArguments"][0].endswith("nightowl")
        assert plist["ProgramArguments"][1] == "run"
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

    def test_generate_plist_hourly_omits_hour(self, tmp_path):
        for _ in range(20):
            plist = generate_plist(tmp_path, "22:00", "06:00", cadence="hourly")
            interval = plist["StartCalendarInterval"]
            assert "Hour" not in interval
            assert "Minute" in interval
            assert 0 <= interval["Minute"] <= 59

    def test_generate_plist_daily_default(self, tmp_path):
        plist = generate_plist(tmp_path, "22:00", "06:00")
        assert "Hour" in plist["StartCalendarInterval"]
