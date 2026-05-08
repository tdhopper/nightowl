from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from nightowl.config import Task
from nightowl.runner import (
    _get_working_diff,
    _run_codex_fact_check,
    _run_fact_check_loop,
    _worktree_path,
    run_task,
)


def _make_task(fact_check: bool = False) -> Task:
    return Task(
        id="test-task",
        name="Test Task",
        interval=timedelta(hours=24),
        prompt="Do something",
        fact_check=fact_check,
    )


@pytest.fixture
def logger():
    return logging.getLogger("test")


class TestGetWorkingDiff:
    def test_returns_diff_text(self, tmp_path, logger):
        with patch("nightowl.runner._run") as mock_run:
            diff_proc = MagicMock()
            diff_proc.stdout = "diff --git a/file.py\n+hello"
            untracked_proc = MagicMock()
            untracked_proc.stdout = ""
            mock_run.side_effect = [diff_proc, untracked_proc]

            result = _get_working_diff(tmp_path, logger)

            assert "diff --git a/file.py" in result
            assert "+hello" in result

    def test_truncates_to_12000(self, tmp_path, logger):
        with patch("nightowl.runner._run") as mock_run:
            diff_proc = MagicMock()
            diff_proc.stdout = "x" * 20000
            untracked_proc = MagicMock()
            untracked_proc.stdout = ""
            mock_run.side_effect = [diff_proc, untracked_proc]

            result = _get_working_diff(tmp_path, logger)
            assert len(result) == 12000


class TestRunCodexFactCheck:
    def test_pass_verdict(self, tmp_path, logger):
        with patch("nightowl.runner._run") as mock_run, \
             patch("pathlib.Path.read_text", return_value="VERDICT: PASS"):
            proc = MagicMock()
            proc.returncode = 0
            mock_run.return_value = proc

            passed, feedback = _run_codex_fact_check("some diff", tmp_path, logger)
            assert passed is True
            assert feedback == ""

    def test_issues_found_verdict(self, tmp_path, logger):
        codex_output = "VERDICT: ISSUES FOUND\n1. Wrong URL in docs"
        with patch("nightowl.runner._run") as mock_run, \
             patch("pathlib.Path.read_text", return_value=codex_output):
            proc = MagicMock()
            proc.returncode = 0
            mock_run.return_value = proc

            passed, feedback = _run_codex_fact_check("some diff", tmp_path, logger)
            assert passed is False
            assert "ISSUES FOUND" in feedback

    def test_codex_failure_treated_as_pass(self, tmp_path, logger):
        with patch("nightowl.runner._run") as mock_run:
            proc = MagicMock()
            proc.returncode = 1
            mock_run.return_value = proc

            passed, feedback = _run_codex_fact_check("some diff", tmp_path, logger)
            assert passed is True
            assert feedback == ""

    def test_codex_timeout_treated_as_pass(self, tmp_path, logger):
        with patch("nightowl.runner._run", side_effect=subprocess.TimeoutExpired("codex", 300)):
            passed, feedback = _run_codex_fact_check("some diff", tmp_path, logger)
            assert passed is True
            assert feedback == ""


class TestRunFactCheckLoop:
    def test_pass_on_first_iteration(self, tmp_path, logger):
        task = _make_task(fact_check=True)
        with patch("nightowl.runner._get_working_diff", return_value="some diff"), \
             patch("nightowl.runner._run_codex_fact_check", return_value=(True, "")), \
             patch("nightowl.runner._run") as mock_run:
            _run_fact_check_loop(task, tmp_path, logger)
            # Claude should NOT be re-invoked
            mock_run.assert_not_called()

    def test_issues_then_pass(self, tmp_path, logger):
        task = _make_task(fact_check=True)
        with patch("nightowl.runner._get_working_diff", return_value="some diff"), \
             patch("nightowl.runner._run_codex_fact_check", side_effect=[
                 (False, "VERDICT: ISSUES FOUND\n1. Bad URL"),
                 (True, ""),
             ]), \
             patch("nightowl.runner._run") as mock_run:
            proc = MagicMock()
            proc.returncode = 0
            mock_run.return_value = proc

            _run_fact_check_loop(task, tmp_path, logger)

            # Claude should be re-invoked once
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert "claude" in cmd

    def test_max_iterations_exhausted(self, tmp_path, logger):
        task = _make_task(fact_check=True)
        with patch("nightowl.runner._get_working_diff", return_value="some diff"), \
             patch("nightowl.runner._run_codex_fact_check", return_value=(False, "VERDICT: ISSUES FOUND\n1. Error")), \
             patch("nightowl.runner._run") as mock_run:
            proc = MagicMock()
            proc.returncode = 0
            mock_run.return_value = proc

            # Should not raise
            _run_fact_check_loop(task, tmp_path, logger)

            # Claude re-invoked 3 times (once per failed iteration)
            assert mock_run.call_count == 3

    def test_empty_diff_skips(self, tmp_path, logger):
        task = _make_task(fact_check=True)
        with patch("nightowl.runner._get_working_diff", return_value=""), \
             patch("nightowl.runner._run_codex_fact_check") as mock_codex:
            _run_fact_check_loop(task, tmp_path, logger)
            mock_codex.assert_not_called()


class TestWorktreePath:
    def test_path_under_cache_root(self, tmp_path):
        # Use a project name with mixed case + spaces to exercise slugging
        project = tmp_path / "My Project"
        path = _worktree_path(project, "some-task")
        assert path.parts[-2:] == ("my-project", "some-task")
        assert ".cache/nightowl/worktrees" in str(path)


class TestRunTaskUsesWorktree:
    def test_worktree_add_and_remove_called(self, tmp_path, logger):
        task = _make_task()
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)

            cmds = [c.args[0] for c in mock_run.call_args_list]
            # Worktree was created
            assert any(
                cmd[:3] == ["git", "worktree", "add"] and str(worktree) in cmd
                for cmd in cmds
            ), f"expected `git worktree add ... {worktree}` in {cmds}"
            # And removed at the end
            assert any(
                cmd[:3] == ["git", "worktree", "remove"] and str(worktree) in cmd
                for cmd in cmds
            ), f"expected `git worktree remove ... {worktree}` in {cmds}"

    def test_main_checkout_never_touched(self, tmp_path, logger):
        """Project working tree is never checked out, reset, or cleaned."""
        task = _make_task()
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)

            for c in mock_run.call_args_list:
                cmd = c.args[0]
                # No `git checkout` of any kind, no `git clean`, no `git reset`
                # against the project directory itself.
                if c.kwargs.get("cwd") == tmp_path:
                    assert cmd[:2] != ["git", "checkout"], (
                        f"Should not run `git checkout ...` in project dir: {cmd}"
                    )
                    assert cmd[:2] != ["git", "clean"], (
                        f"Should not run `git clean ...` in project dir: {cmd}"
                    )
                    assert cmd[:2] != ["git", "reset"], (
                        f"Should not run `git reset ...` in project dir: {cmd}"
                    )


class TestRunTaskFactCheck:
    def test_fact_check_called_when_enabled(self, tmp_path, logger):
        task = _make_task(fact_check=True)
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._run_fact_check_loop") as mock_fc, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)
            mock_fc.assert_called_once_with(task, worktree, logger)

    def test_fact_check_not_called_when_disabled(self, tmp_path, logger):
        task = _make_task(fact_check=False)
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._run_fact_check_loop") as mock_fc, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)
            mock_fc.assert_not_called()
