from __future__ import annotations

import json

import pytest

from nightowl import runs


@pytest.fixture(autouse=True)
def tmp_runs_root(tmp_path, monkeypatch):
    """Isolate runs writes from real ~/.local/share/nightowl/runs/."""
    monkeypatch.setattr(runs, "RUNS_ROOT", tmp_path / "runs")
    return tmp_path / "runs"


class TestProjectSlug:
    def test_slug_lowercases_and_dashes_special_chars(self):
        # Match runner._worktree_path's slugging so the runs.jsonl path
        # is predictable from the project name.
        assert runs._project_slug("/Users/tim/My Project") == "my-project"

    def test_slug_strips_outer_dashes(self):
        assert runs._project_slug("/p/--weird--") == "weird"


class TestRunsPath:
    def test_path_is_per_project(self, tmp_runs_root):
        p = runs.runs_path("/Users/tim/handbook")
        assert p == tmp_runs_root / "handbook.jsonl"


class TestAppendRun:
    def test_appends_one_jsonl_line(self, tmp_runs_root):
        record = {
            "task_id": "content-gap",
            "result": "success",
            "duration_s": 1164,
        }
        runs.append_run("/proj", record)

        path = tmp_runs_root / "proj.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record

    def test_multiple_appends_grow_file(self, tmp_runs_root):
        runs.append_run("/proj", {"task_id": "a", "result": "success"})
        runs.append_run("/proj", {"task_id": "b", "result": "failure"})
        runs.append_run("/proj", {"task_id": "c", "result": "success"})

        path = tmp_runs_root / "proj.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["task_id"] for line in lines] == ["a", "b", "c"]

    def test_append_creates_parent_dirs(self, tmp_runs_root):
        # The runs directory does not exist yet — append should create it.
        assert not tmp_runs_root.exists()
        runs.append_run("/proj", {"task_id": "a"})
        assert tmp_runs_root.is_dir()


class TestReadRuns:
    def test_empty_when_no_file(self):
        assert runs.read_runs("/never-ran") == []

    def test_reads_back_appended(self):
        runs.append_run("/proj", {"task_id": "a", "result": "success"})
        runs.append_run("/proj", {"task_id": "b", "result": "failure"})
        result = runs.read_runs("/proj")
        assert len(result) == 2
        assert result[0]["task_id"] == "a"
        assert result[1]["result"] == "failure"

    def test_skips_malformed_lines(self, tmp_runs_root):
        # If a line gets corrupted (e.g. partial write during a kill),
        # the reader skips it instead of wedging.
        path = tmp_runs_root / "proj.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"task_id": "a"}\nNOT JSON\n{"task_id": "b"}\n'
        )
        result = runs.read_runs("/proj")
        assert [r["task_id"] for r in result] == ["a", "b"]
