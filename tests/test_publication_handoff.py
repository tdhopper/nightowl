"""Exercise publication recovery against real repositories, without network access."""

import json
import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nightowl import runner
from nightowl.config import Task

LOGGER = logging.getLogger(__name__)
BRANCH = "nightowl/test-publication"


def git(directory, *args):
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    # Ignore user signing, hooks, aliases, and global push configuration.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "origin.git"
    local = tmp_path / "local"
    other = tmp_path / "other"
    remote.mkdir()
    git(remote, "init", "--bare")
    local.mkdir()
    git(local, "init", "-b", "main")
    git(local, "config", "user.name", "Nightowl Test")
    git(local, "config", "user.email", "nightowl@example.invalid")
    git(local, "config", "commit.gpgsign", "false")
    git(local, "remote", "add", "origin", str(remote))
    (local / "content.txt").write_text("base\n")
    git(local, "add", "content.txt")
    git(local, "commit", "-m", "Base")
    git(local, "push", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    git(tmp_path, "clone", str(remote), str(other))
    git(other, "config", "user.name", "Nightowl Test")
    git(other, "config", "user.email", "nightowl@example.invalid")
    git(other, "config", "commit.gpgsign", "false")
    git(local, "checkout", "-b", BRANCH)
    git(other, "checkout", "-b", BRANCH)
    return local, other, remote


def commit_content(directory, text, message):
    (directory / "content.txt").write_text(text)
    git(directory, "add", "content.txt")
    git(directory, "commit", "-m", message)
    return git(directory, "rev-parse", "HEAD")


def test_push_new_branch(repositories):
    local, _, remote = repositories
    head = commit_content(local, "validated work\n", "Runner commit")
    assert runner._push_branch(local, BRANCH, LOGGER) is None
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == head


def test_rejected_push_accepts_identical_tree_without_rewriting_remote(repositories):
    local, other, remote = repositories
    local_head = commit_content(local, "validated work\n", "Runner commit")
    remote_head = commit_content(other, "validated work\n", "Agent commit")
    assert local_head != remote_head
    assert git(local, "rev-parse", "HEAD^{tree}") == git(
        other, "rev-parse", "HEAD^{tree}"
    )
    git(other, "push", "origin", BRANCH)

    assert runner._push_branch(local, BRANCH, LOGGER) is None

    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == remote_head
    assert git(local, "rev-parse", "HEAD") == local_head


def test_rejected_push_refuses_different_tree_without_rewriting_remote(repositories):
    local, other, remote = repositories
    local_head = commit_content(local, "validated work\n", "Runner commit")
    remote_head = commit_content(other, "different work\n", "Other commit")
    git(other, "push", "origin", BRANCH)

    with pytest.raises(RuntimeError):
        runner._push_branch(local, BRANCH, LOGGER)

    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == remote_head
    assert git(local, "rev-parse", "HEAD") == local_head


@pytest.mark.parametrize("outcome", ["completed", None])
def test_codex_publication_handoff_requires_validated_outcome(tmp_path, outcome):
    task = Task(
        "task", "Task", timedelta(days=1), "Fix the issue and open a PR.", agent="codex"
    )

    def invoke(cmd, **kwargs):
        prompt = cmd[-1]
        assert task.prompt in prompt
        assert "runner" in prompt.lower()
        assert "commit" in prompt.lower()
        assert "push" in prompt.lower()
        assert "pr_body.md" in prompt
        assert "pr_title.txt" in prompt
        assert "validat" in prompt.lower()
        assert "completed" in prompt
        if outcome:
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                json.dumps(
                    {
                        "outcome": outcome,
                        "summary": "Validated changes ready for runner.",
                    }
                )
            )
        return subprocess.CompletedProcess(cmd, 0, "Process exited successfully", "")

    with patch("nightowl.runner.subprocess.run", side_effect=invoke):
        result = runner._run_agent_task(task, tmp_path, LOGGER, {})

    assert result.returncode == (0 if outcome == "completed" else 1)
