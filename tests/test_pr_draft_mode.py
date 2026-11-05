"""PR mode selection without creating or changing external pull requests."""

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


def task_for(agent="codex", **kwargs):
    return Task(
        "versions",
        "Versions",
        timedelta(days=3),
        "Open one draft PR after validation, with required review labels.",
        agent=agent,
        **kwargs,
    )


@pytest.mark.parametrize("agent,expected", [("codex", True), ("claude", False)])
def test_unset_override_preserves_agent_default(agent, expected):
    assert runner._pr_draft_mode(task_for(agent), {}) is expected


@pytest.mark.parametrize("agent", ["codex", "claude"])
@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_explicit_mode(agent, value, expected):
    assert (
        runner._pr_draft_mode(task_for(agent), {"NIGHTOWL_PR_DRAFT": value}) is expected
    )


@pytest.mark.parametrize("value", ["", "0", "1", "False", "yes", " false "])
def test_invalid_override_is_rejected(value):
    with pytest.raises(ValueError, match="NIGHTOWL_PR_DRAFT"):
        runner._pr_draft_mode(task_for(), {"NIGHTOWL_PR_DRAFT": value})


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("actual", [True, False, None, 0, 1])
def test_verification_requires_exact_selected_mode(draft, actual):
    pr = {
        "url": "https://example.invalid/pr/1",
        "isDraft": actual,
        "labels": [{"name": "needs-codex-review"}],
    }
    if actual is draft:
        assert runner._check_pr(pr, ["needs-codex-review"], draft=draft) == pr["url"]
    else:
        with pytest.raises(RuntimeError, match="Task PR"):
            runner._check_pr(pr, ["needs-codex-review"], draft=draft)


def test_ready_pr_still_requires_review_labels():
    with pytest.raises(RuntimeError, match="missing required labels"):
        runner._check_pr(
            {"url": "https://example.invalid/pr/1", "isDraft": False, "labels": []},
            ["needs-codex-review"],
            draft=False,
        )


@pytest.mark.parametrize("actual_draft", [False, True])
def test_direct_publication_ready_mode_is_verified_without_converting_pr(
    tmp_path, actual_draft
):
    task = task_for(output="none", git_write=True, require_pr=True)
    branch = "nightowl/test-versions"
    sha = "a" * 40

    def invoke(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            value = ""
        elif command[:3] == ["git", "branch", "--show-current"]:
            value = branch
        elif command[:3] == ["git", "rev-parse", "HEAD"]:
            value = sha
        elif command[:3] == ["git", "ls-remote", "--heads"]:
            value = f"{sha}\trefs/heads/{branch}\n"
        elif command[:3] == ["gh", "pr", "list"]:
            value = json.dumps(
                [{"url": "https://example.invalid/1", "isDraft": actual_draft}]
            )
        else:
            raise AssertionError(f"Unexpected command, possibly a mutation: {command}")
        return subprocess.CompletedProcess(command, 0, value, "")

    with patch("nightowl.runner._run", side_effect=invoke):
        if actual_draft:
            with pytest.raises(RuntimeError, match="not ready for review"):
                runner._verify_direct_pr(task, tmp_path, branch, LOGGER, draft=False)
        else:
            assert (
                runner._verify_direct_pr(task, tmp_path, branch, LOGGER, draft=False)
                == "https://example.invalid/1"
            )


@pytest.mark.parametrize(
    "agent,direct", [("codex", True), ("codex", False), ("claude", False)]
)
@pytest.mark.parametrize("override", [None, "false", "true"])
def test_agent_receives_mode_only_for_authorized_new_prs(
    tmp_path, agent, override, direct
):
    task = task_for(
        agent,
        output="none" if direct else "pr",
        git_write=direct,
        require_pr=direct,
    )
    env = {} if override is None else {"NIGHTOWL_PR_DRAFT": override}
    prompts = []

    def invoke(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, str(tmp_path), "")
        prompts.append(command[-1])
        if command[0] == "codex":
            Path(command[command.index("--output-last-message") + 1]).write_text(
                json.dumps({"outcome": "completed", "summary": "Validated."})
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("nightowl.runner._run", side_effect=invoke):
        result = runner._run_agent_task(task, tmp_path, LOGGER, env)
    assert result.returncode == 0
    prompt = prompts[0]
    assert task.prompt in prompt
    if override is None:
        assert "Nightowl PR mode" not in prompt
    else:
        mode = (
            "as drafts with --draft"
            if override == "true"
            else "ready for review without --draft"
        )
        assert mode in prompt
        assert "Keep all required validation and review labels" in prompt
        assert "Do not change the draft status of existing PRs" in prompt
        assert "does not authorize additional publication" in prompt
    if agent == "codex" and not direct:
        assert "The Nightowl runner owns staging" in prompt


def test_review_task_receives_no_new_pr_authorization(tmp_path):
    task = task_for(output="none", git_write=True)
    prompts = []

    def invoke(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, str(tmp_path), "")
        prompts.append(command[-1])
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"outcome": "completed", "summary": "Reviewed."})
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("nightowl.runner._run", side_effect=invoke):
        runner._run_agent_task(task, tmp_path, LOGGER, {"NIGHTOWL_PR_DRAFT": "false"})
    assert "Nightowl PR mode" not in prompts[0]
