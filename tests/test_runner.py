from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from nightowl import state
from nightowl.config import SkipIfOpenCheck, Task
from nightowl.runner import (
    _check_skip_if_open,
    _generate_pr_metadata,
    _get_working_diff,
    _parse_claude_envelope,
    _run_codex_fact_check,
    _run_fact_check_loop,
    _run_skip_check,
    _worktree_path,
    run_task,
)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    """Isolate state writes from real ~/.config/nightowl/state.json."""
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "state.json")


def _make_task(
    fact_check: bool = False,
    skip_if_open: list[SkipIfOpenCheck] | None = None,
) -> Task:
    return Task(
        id="test-task",
        name="Test Task",
        interval=timedelta(hours=24),
        prompt="Do something",
        fact_check=fact_check,
        skip_if_open=skip_if_open,
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


class TestRunTaskRecordsStart:
    def test_started_recorded_before_first_subprocess(self, tmp_path, logger):
        """``record_task_started`` must run before any subprocess so a kill
        between launchd fires and ``record_task_result`` still consumes the
        interval. Without this, a crashed task re-fires every hour."""
        task = _make_task()
        worktree = tmp_path / "wt"

        call_order: list[str] = []

        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree), \
             patch("nightowl.runner.record_task_started") as mock_started:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.side_effect = lambda *a, **kw: (call_order.append("_run"), proc)[1]
            mock_started.side_effect = lambda *a, **kw: call_order.append("started")

            run_task(task, tmp_path, logger)

            assert call_order.index("started") < call_order.index("_run"), (
                f"record_task_started must run before any subprocess: {call_order}"
            )
            mock_started.assert_called_once_with(str(tmp_path), task.id)


class TestRunTaskStateDir:
    def test_nightowl_state_dir_passed_to_claude(self, tmp_path, logger, monkeypatch):
        """The claude subprocess gets NIGHTOWL_STATE_DIR set to a per-task path
        that survives worktree teardown — tasks like reddit-scout need this
        for cross-run dedupe state."""
        from nightowl import runner

        task = _make_task()
        worktree = tmp_path / "wt"
        state_root = tmp_path / "task-state"
        monkeypatch.setattr(runner, "TASK_STATE_ROOT", state_root)

        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)

            # Find the claude call and check its env kwarg.
            claude_calls = [
                c for c in mock_run.call_args_list
                if c.args[0][0] == "claude"
            ]
            assert claude_calls, "expected at least one claude invocation"
            env = claude_calls[0].kwargs.get("env")
            assert env is not None, "claude must be invoked with explicit env"
            expected_dir = state_root / task.id
            assert env["NIGHTOWL_STATE_DIR"] == str(expected_dir)
            # Sanity check: PATH passes through so claude can find its deps
            assert "PATH" in env

    def test_state_dir_is_created(self, tmp_path, logger, monkeypatch):
        from nightowl import runner

        task = _make_task()
        worktree = tmp_path / "wt"
        state_root = tmp_path / "task-state"
        monkeypatch.setattr(runner, "TASK_STATE_ROOT", state_root)

        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            run_task(task, tmp_path, logger)

            assert (state_root / task.id).is_dir()


class TestGeneratePrMetadata:
    """`_generate_pr_metadata` honors task-provided overrides from the state
    dir so structured-content tasks (handbook-article and friends) can supply
    their own PR body instead of getting a generic 2-5 sentence summary."""

    def test_body_override_used_when_present(self, tmp_path, logger, monkeypatch):
        from nightowl import runner

        state_root = tmp_path / "task-state"
        task = _make_task()
        state_dir = state_root / task.id
        state_dir.mkdir(parents=True)
        (state_dir / "pr_body.md").write_text("## Custom\n\nVerified body.")
        monkeypatch.setattr(runner, "TASK_STATE_ROOT", state_root)

        title, body = _generate_pr_metadata(task, tmp_path, logger)

        assert "Verified body." in body
        assert body.endswith("*Automated by nightowl*")
        assert title == task.name
        # Override files are consumed so a subsequent run starts fresh.
        assert not (state_dir / "pr_body.md").exists()

    def test_title_override_used_when_present(self, tmp_path, logger, monkeypatch):
        from nightowl import runner

        state_root = tmp_path / "task-state"
        task = _make_task()
        state_dir = state_root / task.id
        state_dir.mkdir(parents=True)
        (state_dir / "pr_body.md").write_text("body")
        (state_dir / "pr_title.txt").write_text("Add custom title\n")
        monkeypatch.setattr(runner, "TASK_STATE_ROOT", state_root)

        title, body = _generate_pr_metadata(task, tmp_path, logger)

        assert title == "Add custom title"
        assert not (state_dir / "pr_title.txt").exists()

    def test_falls_back_to_claude_when_no_override(self, tmp_path, logger, monkeypatch):
        from nightowl import runner

        state_root = tmp_path / "task-state"
        (state_root / "test-task").mkdir(parents=True)
        task = _make_task()
        monkeypatch.setattr(runner, "TASK_STATE_ROOT", state_root)

        with patch("nightowl.runner._run") as mock_run:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "TITLE: Generated title\nBODY: Generated body"
            mock_run.return_value = proc

            title, body = _generate_pr_metadata(task, tmp_path, logger)

            assert title == "Generated title"
            assert "Generated body" in body
            # Claude was actually invoked.
            assert any(
                c.args[0][0] == "claude" for c in mock_run.call_args_list
            )


class TestParseClaudeEnvelope:
    def test_none_stdout(self):
        result = _parse_claude_envelope(None)
        assert result["claude_cost_usd"] is None
        assert result["claude_input_tokens"] is None
        assert result["claude_output_tokens"] is None
        assert result["claude_cache_read_tokens"] is None

    def test_empty_string(self):
        result = _parse_claude_envelope("")
        assert all(v is None for v in result.values())

    def test_malformed_json(self):
        result = _parse_claude_envelope("not json at all")
        assert all(v is None for v in result.values())

    def test_parses_full_envelope(self):
        envelope = (
            '{"type":"result","total_cost_usd":9.98,'
            '"usage":{"input_tokens":156,"output_tokens":62085,'
            '"cache_read_input_tokens":12174649}}'
        )
        result = _parse_claude_envelope(envelope)
        assert result["claude_cost_usd"] == 9.98
        assert result["claude_input_tokens"] == 156
        assert result["claude_output_tokens"] == 62085
        assert result["claude_cache_read_tokens"] == 12174649

    def test_partial_envelope(self):
        # If claude only emits the result key and no usage, we get back
        # all Nones except cost (also None here).
        result = _parse_claude_envelope('{"result": "ok"}')
        assert all(v is None for v in result.values())


class TestRunTaskReturnsObservability:
    def test_success_carries_timing_and_envelope(self, tmp_path, logger):
        task = _make_task()
        worktree = tmp_path / "wt"
        claude_envelope = (
            '{"total_cost_usd":0.42,'
            '"usage":{"input_tokens":100,"output_tokens":200,'
            '"cache_read_input_tokens":300}}'
        )
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            def fake_run(cmd, *a, **kw):
                proc = MagicMock()
                proc.returncode = 0
                if cmd[0] == "claude" and "--output-format" in cmd:
                    proc.stdout = claude_envelope
                else:
                    proc.stdout = ""
                proc.stderr = ""
                return proc
            mock_run.side_effect = fake_run

            result = run_task(task, tmp_path, logger)

            assert result["task_id"] == task.id
            assert result["result"] == "success"
            assert "started_at" in result
            assert "ended_at" in result
            assert isinstance(result["duration_s"], int)
            assert result["claude_cost_usd"] == 0.42
            assert result["claude_input_tokens"] == 100
            assert result["claude_output_tokens"] == 200
            assert result["claude_cache_read_tokens"] == 300

    def test_failure_carries_timing_and_null_envelope(self, tmp_path, logger):
        task = _make_task()
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = ""
            proc.stderr = "boom"
            mock_run.return_value = proc

            result = run_task(task, tmp_path, logger)

            assert result["result"] == "failure"
            assert result["error"]
            assert result["task_id"] == task.id
            assert "started_at" in result
            assert "ended_at" in result
            assert isinstance(result["duration_s"], int)
            # Claude crashed before emitting JSON — envelope fields are None.
            assert result["claude_cost_usd"] is None
            assert result["claude_input_tokens"] is None


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


def _gh_proc(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc


class TestRunSkipCheck:
    def test_pr_branch_prefix_match(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="pr-branch-prefix")
        # Two PRs: one matching, one not.
        gh_out = (
            '[{"headRefName": "nightowl/20260513-test-task", '
            '"url": "https://gh/pr/42"}, '
            '{"headRefName": "feature/unrelated", "url": "https://gh/pr/9"}]'
        )
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)):
            count, ref = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count == 1
        assert "pr/42" in ref

    def test_pr_branch_prefix_no_match(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="pr-branch-prefix")
        gh_out = '[{"headRefName": "nightowl/20260513-other-task", "url": "u"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)):
            count, _ = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count == 0

    def test_pr_branch_prefix_requires_nightowl_prefix(self, tmp_path, logger):
        """A branch containing the task id but not under `nightowl/` is ignored.

        Without this, unrelated topic branches (e.g. `feature/test-task-fix`)
        would suppress the run.
        """
        check = SkipIfOpenCheck(type="pr-branch-prefix")
        gh_out = '[{"headRefName": "feature/test-task-fix", "url": "u"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)):
            count, _ = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count == 0

    def test_pr_branch_prefix_custom_value(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="pr-branch-prefix", value="custom-slug")
        gh_out = '[{"headRefName": "nightowl/custom-slug-foo", "url": "u"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)):
            count, _ = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count == 1

    def test_issue_label_default_value(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="issue-label")
        gh_out = '[{"number": 12, "url": "https://gh/issue/12"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)) as mock_run:
            count, ref = _run_skip_check(check, "comp-analysis", tmp_path, logger)
            cmd = mock_run.call_args[0][0]
            assert "--label" in cmd
            assert "source:comp-analysis" in cmd
        assert count == 1
        assert "issue/12" in ref

    def test_issue_label_custom_value(self, tmp_path, logger):
        check = SkipIfOpenCheck(
            type="issue-label", value="source:competitive-analysis",
        )
        gh_out = '[{"number": 1, "url": "u"}, {"number": 2, "url": "u"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)) as mock_run:
            count, _ = _run_skip_check(check, "any-task", tmp_path, logger)
            cmd = mock_run.call_args[0][0]
            assert "source:competitive-analysis" in cmd
        assert count == 2

    def test_issue_title_search(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="issue-title")
        gh_out = '[{"number": 7, "url": "https://gh/issue/7"}]'
        with patch("nightowl.runner._run", return_value=_gh_proc(gh_out)) as mock_run:
            count, ref = _run_skip_check(check, "scope-audit", tmp_path, logger)
            cmd = mock_run.call_args[0][0]
            assert "--search" in cmd
            assert "scope-audit in:title" in cmd
        assert count == 1
        assert "issue/7" in ref

    def test_gh_failure_treated_as_no_match(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="pr-branch-prefix")
        with patch(
            "nightowl.runner._run",
            return_value=_gh_proc(stdout="", returncode=1),
        ):
            count, _ = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count is None  # signals "treat as no match"

    def test_gh_invalid_json_treated_as_no_match(self, tmp_path, logger):
        check = SkipIfOpenCheck(type="pr-branch-prefix")
        with patch("nightowl.runner._run", return_value=_gh_proc("not-json")):
            count, _ = _run_skip_check(check, "test-task", tmp_path, logger)
        assert count is None


class TestCheckSkipIfOpen:
    def test_no_checks_returns_none(self, tmp_path, logger):
        task = _make_task()
        assert _check_skip_if_open(task, tmp_path, logger) is None

    def test_match_returns_reason(self, tmp_path, logger):
        task = _make_task(
            skip_if_open=[SkipIfOpenCheck(type="pr-branch-prefix")],
        )
        with patch(
            "nightowl.runner._run_skip_check", return_value=(1, "https://gh/pr/1"),
        ):
            reason = _check_skip_if_open(task, tmp_path, logger)
        assert reason is not None
        assert "pr-branch-prefix" in reason
        assert "https://gh/pr/1" in reason

    def test_no_match_returns_none(self, tmp_path, logger):
        task = _make_task(
            skip_if_open=[SkipIfOpenCheck(type="pr-branch-prefix")],
        )
        with patch("nightowl.runner._run_skip_check", return_value=(0, "")):
            assert _check_skip_if_open(task, tmp_path, logger) is None

    def test_threshold_respected(self, tmp_path, logger):
        """A count below threshold does not skip."""
        task = _make_task(
            skip_if_open=[
                SkipIfOpenCheck(type="issue-label", threshold=5),
            ],
        )
        with patch("nightowl.runner._run_skip_check", return_value=(3, "u")):
            assert _check_skip_if_open(task, tmp_path, logger) is None
        with patch("nightowl.runner._run_skip_check", return_value=(5, "u")):
            assert _check_skip_if_open(task, tmp_path, logger) is not None

    def test_gh_failure_does_not_skip(self, tmp_path, logger):
        """A transient `gh` failure must not permanently wedge a task."""
        task = _make_task(
            skip_if_open=[SkipIfOpenCheck(type="pr-branch-prefix")],
        )
        with patch("nightowl.runner._run_skip_check", return_value=(None, "")):
            assert _check_skip_if_open(task, tmp_path, logger) is None

    def test_first_match_wins(self, tmp_path, logger):
        """Multiple checks: any match triggers a skip."""
        task = _make_task(
            skip_if_open=[
                SkipIfOpenCheck(type="pr-branch-prefix"),
                SkipIfOpenCheck(type="issue-title"),
            ],
        )
        with patch(
            "nightowl.runner._run_skip_check",
            side_effect=[(0, ""), (1, "https://gh/issue/9")],
        ):
            reason = _check_skip_if_open(task, tmp_path, logger)
        assert reason is not None
        assert "issue-title" in reason


class TestRunTaskSkipIfOpen:
    def test_skip_returns_early_without_subprocess(self, tmp_path, logger):
        """When a skip check matches, run_task must not invoke claude or
        touch the worktree."""
        task = _make_task(
            skip_if_open=[SkipIfOpenCheck(type="pr-branch-prefix")],
        )
        worktree = tmp_path / "wt"
        with patch(
            "nightowl.runner._check_skip_if_open",
            return_value="pr-branch-prefix matched 1: https://gh/pr/1",
        ), \
             patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree), \
             patch("nightowl.runner.record_task_started") as mock_started:
            result = run_task(task, tmp_path, logger)
            assert result["result"] == "skipped_open_artifact"
            assert "https://gh/pr/1" in result["skip_reason"]
            # No subprocesses, no worktree, no started marker.
            mock_run.assert_not_called()
            mock_started.assert_not_called()

    def test_no_skip_runs_normally(self, tmp_path, logger):
        task = _make_task(
            skip_if_open=[SkipIfOpenCheck(type="pr-branch-prefix")],
        )
        worktree = tmp_path / "wt"
        with patch("nightowl.runner._check_skip_if_open", return_value=None), \
             patch("nightowl.runner._run") as mock_run, \
             patch("nightowl.runner._worktree_path", return_value=worktree):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_run.return_value = proc

            result = run_task(task, tmp_path, logger)
            assert result["result"] == "success"
            # Subprocesses were called (fetch, worktree add, claude, etc.)
            assert mock_run.call_count > 0
