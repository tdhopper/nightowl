"""Codex Git access and runner-owned PR publication."""

import json
import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nightowl import runner
from nightowl.config import Task, _load_task

LOGGER = logging.getLogger(__name__)


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_git_write_requires_explicit_codex_none_task(tmp_path):
    path = tmp_path / "review.md"
    path.write_text(
        "---\nname: Review\ninterval: 12h\nagent: codex\n"
        "output: none\ngit_write: true\n---\nReview PRs.\n"
    )
    assert _load_task(path).git_write is True

    with pytest.raises(ValueError, match="git_write"):
        Task(
            "review",
            "Review",
            timedelta(hours=12),
            "Review PRs.",
            agent="claude",
            output="none",
            git_write=True,
        )
    with pytest.raises(ValueError, match="git_write"):
        Task(
            "review",
            "Review",
            timedelta(hours=12),
            "Review PRs.",
            agent="codex",
            output="pr",
            git_write=True,
        )
    with pytest.raises(ValueError, match="git_write"):
        Task(
            "review",
            "Review",
            timedelta(hours=12),
            "Review PRs.",
            agent="codex",
            output="none",
            git_write="yes",  # type: ignore[arg-type]
        )


def test_codex_gets_linked_worktree_git_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    (project / "file.txt").write_text("base\n")
    git(project, "add", "file.txt")
    git(
        project,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "base",
    )
    worktree = tmp_path / "task"
    git(project, "worktree", "add", "-b", "task", str(worktree), "main")
    state = tmp_path / "state"
    state.mkdir()
    task = Task(
        "review",
        "Review",
        timedelta(hours=12),
        "Review PRs.",
        agent="codex",
        output="none",
        git_write=True,
    )
    original_run = runner._run
    commands = []

    def invoke(command, **kwargs):
        if command[0] != "codex":
            return original_run(command, **kwargs)
        commands.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"outcome": "completed", "summary": "Reviewed PRs."})
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("nightowl.runner._run", side_effect=invoke):
        result = runner._run_agent_task(
            task, worktree, LOGGER, {"NIGHTOWL_STATE_DIR": str(state)}
        )

    assert result.returncode == 0
    command = commands[0]
    grants = [command[i + 1] for i, item in enumerate(command) if item == "--add-dir"]
    assert grants == [str(state), str(project / ".git")]
    assert "sandbox_workspace_write.network_access=true" in command
    assert "danger-full-access" not in command


def test_codex_pr_needs_metadata_instead_of_claude_fallback(tmp_path):
    task = Task(
        "versions", "Versions", timedelta(days=3), "Update versions.", agent="codex"
    )
    with patch("nightowl.runner._task_state_dir", return_value=tmp_path):
        with patch("nightowl.runner._run") as run:
            with pytest.raises(RuntimeError, match="did not provide PR title and body"):
                runner._generate_pr_metadata(task, tmp_path, LOGGER)
    run.assert_not_called()


def test_runner_publishes_codex_changes_as_labeled_draft(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "remote", "add", "origin", str(origin))
    (seed / "file.txt").write_text("base\n")
    git(seed, "add", "file.txt")
    git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "base",
    )
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    project = tmp_path / "project"
    git(tmp_path, "clone", str(origin), str(project))
    git(project, "config", "user.name", "Nightowl Test")
    git(project, "config", "user.email", "test@example.invalid")
    git(project, "config", "commit.gpgsign", "false")
    monkeypatch.setattr(runner, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(runner, "TASK_STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(runner, "record_task_started", lambda *args: None)
    task = Task(
        "versions",
        "Version bump",
        timedelta(days=3),
        "Update versions.",
        agent="codex",
        output="pr",
        pr_labels=["needs-codex-review"],
    )

    def agent(_task, worktree, _logger, env):
        assert not (Path(env["NIGHTOWL_STATE_DIR"]) / "pr_body.md").exists()
        (worktree / "file.txt").write_text("updated\n")
        Path(env["NIGHTOWL_STATE_DIR"], "pr_title.txt").write_text(
            "Bump tool version\n"
        )
        Path(env["NIGHTOWL_STATE_DIR"], "pr_body.md").write_text("Verified commands.\n")
        return subprocess.CompletedProcess(["codex"], 0, "", "")

    state = tmp_path / "state" / "versions"
    state.mkdir(parents=True)
    (state / "pr_body.md").write_text("stale body")
    original_run = runner._run
    created = {}

    def invoke(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "pr", "create"]:
            created["command"] = command
            created["body"] = Path(
                command[command.index("--body-file") + 1]
            ).read_text()
            return subprocess.CompletedProcess(
                command, 0, "https://example.invalid/pr/1\n", ""
            )
        return original_run(command, **kwargs)

    with patch("nightowl.runner._run_agent_task", side_effect=agent):
        with patch("nightowl.runner._run", side_effect=invoke):
            outcome = runner.run_task(task, project, LOGGER)

    assert outcome["result"] == "success"
    assert outcome["pr_url"] == "https://example.invalid/pr/1"
    assert "--draft" in created["command"]
    assert created["command"][-2:] == ["--label", "needs-codex-review"]
    assert "Verified commands." in created["body"]
    assert "stale body" not in created["body"]
    assert (
        git(
            origin,
            "show",
            "refs/heads/nightowl/"
            + outcome["started_at"][:10].replace("-", "")
            + "-versions:file.txt",
        )
        == "updated"
    )


@pytest.mark.parametrize(
    ("pr_json", "error"),
    [
        ("[]", "did not open a PR"),
        (
            '[{"url":"https://example.invalid/1","isDraft":false,"labels":[]}]',
            "not a draft",
        ),
        (
            '[{"url":"https://example.invalid/1","isDraft":true,"labels":[]}]',
            "missing required labels",
        ),
        (
            '[{"url":"https://example.invalid/1","isDraft":true,'
            '"labels":[{"name":"needs-codex-review"}]}]',
            None,
        ),
    ],
)
def test_direct_task_rejects_missing_or_unreviewable_pr(tmp_path, pr_json, error):
    task = Task(
        "versions",
        "Versions",
        timedelta(days=3),
        "Update versions.",
        agent="codex",
        output="none",
        git_write=True,
        require_pr=True,
        pr_labels=["needs-codex-review"],
    )
    branch = "nightowl/20261009-versions"

    def invoke(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            value = ""
        elif command[:3] == ["git", "branch", "--show-current"]:
            value = branch + "\n"
        elif command[:3] == ["git", "rev-parse", "HEAD"]:
            value = "a" * 40 + "\n"
        elif command[:3] == ["git", "ls-remote", "--heads"]:
            value = "a" * 40 + f"\trefs/heads/{branch}\n"
        elif command[:3] == ["gh", "pr", "list"]:
            value = pr_json
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, value, "")

    with patch("nightowl.runner._run", side_effect=invoke):
        if error is None:
            assert (
                runner._verify_direct_pr(task, tmp_path, branch, LOGGER)
                == "https://example.invalid/1"
            )
        else:
            with pytest.raises(RuntimeError, match=error):
                runner._verify_direct_pr(task, tmp_path, branch, LOGGER)


def test_direct_task_rejects_uncommitted_work(tmp_path):
    task = Task(
        "versions",
        "Versions",
        timedelta(days=3),
        "Update versions.",
        agent="codex",
        output="none",
        git_write=True,
        require_pr=True,
    )

    def invoke(command, **kwargs):
        values = {
            "status": " M data/tool_versions.yaml\n",
            "branch": "nightowl/20261009-versions\n",
            "rev-parse": "a" * 40 + "\n",
            "ls-remote": "a" * 40 + "\trefs/heads/nightowl/20261009-versions\n",
        }
        return subprocess.CompletedProcess(command, 0, values[command[1]], "")

    with patch("nightowl.runner._run", side_effect=invoke):
        with pytest.raises(RuntimeError, match="uncommitted changes"):
            runner._verify_direct_pr(
                task, tmp_path, "nightowl/20261009-versions", LOGGER
            )


def test_run_task_fails_when_direct_pr_is_unverified(tmp_path, monkeypatch):
    task = Task(
        "versions",
        "Versions",
        timedelta(days=3),
        "Update versions.",
        agent="codex",
        output="none",
        git_write=True,
        require_pr=True,
    )
    monkeypatch.setattr(runner, "TASK_STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(runner, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(runner, "record_task_started", lambda *args: None)
    proc = subprocess.CompletedProcess([], 0, "", "")

    def invoke(command, **kwargs):
        if command[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        return proc

    with patch("nightowl.runner._run", side_effect=invoke):
        with patch("nightowl.runner._run_agent_task", return_value=proc):
            with patch(
                "nightowl.runner._verify_direct_pr",
                side_effect=RuntimeError("Task did not open a PR"),
            ):
                outcome = runner.run_task(task, tmp_path, LOGGER)

    assert outcome["result"] == "failure"
    assert outcome["error"] == "Task did not open a PR"
