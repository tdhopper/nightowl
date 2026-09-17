import json
import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nightowl import runner, state
from nightowl.config import Task


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"outcome": "completed", "summary": "Verified no eligible work."}, 0),
        ({"outcome": "blocked", "summary": "Missing prerequisite."}, 1),
        ({"outcome": "failed", "summary": "Checks failed."}, 1),
        ({"outcome": "completed"}, 1),
        ({"outcome": "completed", "summary": ""}, 1),
        ({"outcome": "unknown", "summary": "Unknown."}, 1),
        ([], 1),
        ("not json", 1),
        (None, 1),
    ],
)
def test_structured_outcomes(tmp_path, payload, expected):
    task = Task("task", "Task", timedelta(days=1), "Do work.", agent="codex")
    paths = []

    def invoke(cmd, **kwargs):
        schema = Path(cmd[cmd.index("--output-schema") + 1])
        output = Path(cmd[cmd.index("--output-last-message") + 1])
        paths.extend([schema, output])
        assert json.loads(schema.read_text())["required"] == ["outcome", "summary"]
        if payload is not None:
            output.write_text(
                payload if isinstance(payload, str) else json.dumps(payload)
            )
        return subprocess.CompletedProcess(cmd, 0, "agent transcript", "")

    with patch("nightowl.runner.subprocess.run", side_effect=invoke):
        result = runner._run_agent_task(task, tmp_path, logging.getLogger("test"), {})
    assert result.returncode == expected
    assert all(not path.exists() for path in paths)


def test_secret_free_outcome_logs(tmp_path, caplog):
    task = Task("task", "Task", timedelta(days=1), "Do work.", agent="codex")
    secret = "TOKEN_VALUE_MUST_NOT_BE_LOGGED"

    def invoke(cmd, **kwargs):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            json.dumps({"outcome": "blocked", "summary": secret})
        )
        return subprocess.CompletedProcess(cmd, 0, secret, secret)

    with (
        patch("nightowl.runner.subprocess.run", side_effect=invoke),
        caplog.at_level(logging.DEBUG),
    ):
        result = runner._run_agent_task(task, tmp_path, logging.getLogger("test"), {})
    assert secret not in caplog.text
    assert secret not in result.stdout + result.stderr
    assert "unavailable prerequisite" in caplog.text


@pytest.mark.parametrize(
    "outcome,expected",
    [("blocked", "failure"), ("failed", "failure"), ("completed", "success")],
)
def test_task_result_uses_outcome(tmp_path, monkeypatch, outcome, expected):
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "TASK_STATE_ROOT", tmp_path / "task-state")
    monkeypatch.setattr(runner, "_worktree_path", lambda *args: tmp_path / "worktree")

    def invoke(cmd, **kwargs):
        if cmd[0] == "codex":
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                json.dumps({"outcome": outcome, "summary": "Result."})
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("nightowl.runner.subprocess.run", side_effect=invoke):
        result = runner.run_task(
            Task("task", "Task", timedelta(days=1), "Do work.", agent="codex"),
            tmp_path,
            logging.getLogger("test"),
        )
    assert result["result"] == expected
    if outcome == "blocked":
        assert result["error"] == "Codex reported an unavailable prerequisite."
    elif outcome == "failed":
        assert result["error"] == "Codex reported that the requested work failed."


def test_factchecker_network_configuration(tmp_path):
    def invoke(cmd, **kwargs):
        assert cmd[cmd.index("-s") + 1] == "workspace-write"
        assert "sandbox_workspace_write.network_access=true" in cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("VERDICT: PASS")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("nightowl.runner.subprocess.run", side_effect=invoke):
        assert runner._run_codex_fact_check(
            "diff", tmp_path, logging.getLogger("test")
        ) == (True, "")


def test_scoped_writable_directories(tmp_path):
    one = tmp_path / "state"
    two = tmp_path / "cache"
    one.mkdir()
    two.mkdir()
    assert runner._codex_writable_args(
        {"NIGHTOWL_STATE_DIR": str(one), "NIGHTOWL_WRITABLE_DIRS": str(two)}
    ) == ["--add-dir", str(one), "--add-dir", str(two)]


@pytest.mark.parametrize("value", ["", ":", "/tmp:", "relative/path"])
def test_invalid_writable_directories(value):
    with pytest.raises(ValueError):
        runner._codex_writable_args({"NIGHTOWL_WRITABLE_DIRS": value})


@pytest.mark.parametrize(
    "output",
    [
        "blocked: no network",
        "",
        "VERDICT: PASS\nBut could not check",
        "VERDICT: ISSUES FOUND",
    ],
)
def test_factchecker_missing_verdict_fails(tmp_path, output):
    def invoke(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text(output)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("nightowl.runner.subprocess.run", side_effect=invoke):
        with pytest.raises(RuntimeError, match="valid verdict"):
            runner._run_codex_fact_check("diff", tmp_path, logging.getLogger("test"))


@pytest.mark.parametrize("valid", [True, False])
def test_private_outcome_replaces_stale_result(tmp_path, valid, caplog):
    destination = tmp_path / "last-codex-outcome.json"
    destination.write_text(
        json.dumps({"outcome": "completed", "summary": "Old result"})
    )
    destination.chmod(0o644)
    payload = {"outcome": "blocked", "summary": "Specific private prerequisite detail."}

    def invoke(cmd, **kwargs):
        current = json.loads(destination.read_text())
        assert current["outcome"] == "failed"
        assert destination.stat().st_mode & 0o777 == 0o600
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            json.dumps(payload) if valid else "invalid"
        )
        return subprocess.CompletedProcess(cmd, 0, "private transcript", "")

    task = Task("task", "Task", timedelta(days=1), "Do work.", agent="codex")
    with (
        patch("nightowl.runner.subprocess.run", side_effect=invoke),
        caplog.at_level(logging.DEBUG),
    ):
        runner._run_agent_task(
            task,
            tmp_path,
            logging.getLogger("test"),
            {"NIGHTOWL_STATE_DIR": str(tmp_path)},
        )
    saved = json.loads(destination.read_text())
    assert saved == (
        payload
        if valid
        else {
            "outcome": "failed",
            "summary": "Codex did not return a valid structured outcome.",
        }
    )
    assert destination.stat().st_mode & 0o777 == 0o600
    assert payload["summary"] not in caplog.text
    assert "private transcript" not in destination.read_text()
    assert not list(tmp_path.glob(".codex-outcome-*"))
