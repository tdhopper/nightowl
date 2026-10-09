"""Per-task Codex model selection preserves existing CLI and Claude defaults."""

import json
import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nightowl.config import DEFAULT_MODEL, Task, _load_task
from nightowl.runner import _run_agent_task


def make_task(**kwargs):
    return Task("example", "Example", timedelta(hours=24), "Do the task", **kwargs)


def test_default_models_preserve_existing_behavior():
    task = make_task()
    assert task.codex_model is None
    assert task.model == DEFAULT_MODEL
    assert task.agent == "claude"


@pytest.mark.parametrize("value", ["", "  ", 7, False, [], {}])
def test_reject_invalid_codex_model(value):
    with pytest.raises(ValueError, match="Invalid codex_model"):
        make_task(codex_model=value)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("", None),
        ("codex_model: null\n", None),
        ("codex_model: gpt-5.6-sol\n", "gpt-5.6-sol"),
    ],
)
def test_load_codex_model_from_frontmatter(tmp_path, field, expected):
    path = tmp_path / "example.md"
    path.write_text(
        f"---\nname: Example\ninterval: 24h\nagent: codex\n{field}---\nDo the task\n"
    )
    task = _load_task(path)
    assert task.codex_model == expected
    assert task.model == DEFAULT_MODEL


@pytest.mark.parametrize("value", ["''", "'  '", "12", "false", "[]", "{}"])
def test_load_rejects_invalid_codex_model(tmp_path, value):
    path = tmp_path / "example.md"
    path.write_text(
        f"---\nname: Example\ninterval: 24h\ncodex_model: {value}\n---\nDo the task\n"
    )
    with pytest.raises(ValueError, match="Invalid codex_model"):
        _load_task(path)


@pytest.mark.parametrize("model", [None, "gpt-5.6-sol"])
def test_codex_subprocess_argv(tmp_path, model):
    task = make_task(agent="codex", codex_model=model, codex_reasoning_effort="medium")
    env = {"NIGHTOWL_STATE_DIR": str(tmp_path)}
    proc = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def invoke(cmd, **kwargs):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            json.dumps({"outcome": "completed", "summary": "No work needed."})
        )
        return proc

    with patch("nightowl.runner.subprocess.run", side_effect=invoke) as run:
        assert (
            _run_agent_task(task, tmp_path, logging.getLogger("test"), env).returncode
            == 0
        )
    cmd = run.call_args.args[0]
    assert cmd[:7] == [
        "codex",
        "exec",
        "--ephemeral",
        "--disable",
        "hooks",
        "-s",
        "workspace-write",
    ]
    assert 'model_reasoning_effort="medium"' in cmd
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--output-schema" in cmd
    assert cmd[-1].startswith("Do the task")
    if model is None:
        assert "--model" not in cmd
    else:
        assert cmd[cmd.index("--model") + 1] == model
    assert run.call_args.kwargs == {
        "cwd": tmp_path,
        "capture_output": True,
        "text": True,
        "timeout": 1800,
        "env": env,
        "stdin": subprocess.DEVNULL,
    }


def test_codex_model_does_not_override_claude(tmp_path):
    task = make_task(model="claude-sonnet-4-6", codex_model="gpt-5.6-sol")
    proc = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with patch("nightowl.runner.subprocess.run", return_value=proc) as run:
        _run_agent_task(task, tmp_path, logging.getLogger("test"), {})
    cmd = run.call_args.args[0]
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert "gpt-5.6-sol" not in cmd
