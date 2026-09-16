import json
import logging
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nightowl import healthchecks as hc
from nightowl import pushover, runner
from nightowl.config import Config, Task


@pytest.fixture
def config(tmp_path):
    return {
        "project_path": str(tmp_path),
        "receipt_path": str(tmp_path / "receipt.json"),
        "scheduler_url": "https://healthchecks.hopperhosted.com/ping/secret",
        "task_urls": {"a": "https://healthchecks.hopperhosted.com/ping/other-secret"},
        "initialized_at": "2026-09-16T00:00:00+00:00",
        "scheduler_timeout_seconds": 5400,
        "max_run_seconds": 43200,
        "task_runtime_seconds": 10800,
        "dispatch_grace_seconds": 43200,
    }


@pytest.fixture
def health(monkeypatch):
    schedule = Config(
        "00:00", "23:59", [Task("a", "A", timedelta(days=1), "do it")], [6]
    )
    states = {}
    history = []
    monkeypatch.setattr(hc, "load_config", lambda _: schedule)
    monkeypatch.setattr(hc, "get_all_task_states", lambda _: states)
    monkeypatch.setattr(hc, "read_runs", lambda _: history)
    return states, history


def test_failure_skip_idle_recovery(config, health):
    states, history = health
    now = hc.stamp("2026-09-16T12:00:00+00:00")
    cache = {}
    states["a"] = {"result": "failure", "last_run": "2026-09-16T01:00:00+00:00"}
    assert hc.task_health(config, now, cache)["a"] == "task-failed"
    states["a"] = {
        "result": "skipped_open_artifact",
        "last_run": "2026-09-16T02:00:00+00:00",
    }
    assert hc.task_health(config, now, cache)["a"] == "task-failed"
    assert hc.task_health(config, now, cache)["a"] == "task-failed"
    states["a"] = {"result": "success", "last_run": "2026-09-16T03:00:00+00:00"}
    assert hc.task_health(config, now, cache)["a"] == "healthy"


def test_history_failure_survives_skip_without_cache(config, health):
    states, history = health
    history.append(
        {"task_id": "a", "result": "failure", "ended_at": config["initialized_at"]}
    )
    states["a"] = {
        "result": "skipped_open_artifact",
        "last_run": config["initialized_at"],
    }
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["a"]
        == "task-failed"
    )


def test_sunday_deadline():
    last = hc.stamp("2026-09-19T13:00:00")
    assert hc.deadline(last, timedelta(days=1), [6], 43200) == hc.stamp(
        "2026-09-21T12:17:00"
    )


def test_overdue_new_task_and_timeout(config, health):
    states, _ = health
    now = hc.stamp("2026-09-17T13:00:00+00:00")
    assert hc.task_health(config, now, {})["a"] == "task-overdue"
    states["a"] = {"result": "started", "last_run": "2026-09-17T11:00:00+00:00"}
    assert hc.task_health(config, now, {})["a"] == "running"
    states["a"]["last_run"] = "2026-09-17T01:00:00+00:00"
    assert hc.task_health(config, now, {})["a"] == "task-timeout"


def test_missing_task(config, health):
    config["task_urls"]["missing"] = config["scheduler_url"]
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["missing"]
        == "task-missing"
    )


def test_failed_fetch_receipt(config):
    assert hc.run(config, [sys.executable, "-c", "raise SystemExit(7)"]) == 7
    assert (
        hc.scheduler_health(config, datetime.now().astimezone()) == "execution-failed"
    )
    receipt = json.loads(__import__("pathlib").Path(config["receipt_path"]).read_text())
    assert not receipt["running"] and receipt["finished_at"]


def test_missing_stale_running_receipt(config):
    now = hc.stamp(config["initialized_at"])
    assert hc.scheduler_health(config, now) == "receipt-unavailable"
    from pathlib import Path

    hc.write_json(
        Path(config["receipt_path"]),
        {
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "running": False,
            "exit_code": 0,
            "finished_at": (now - timedelta(hours=2)).isoformat(),
        },
    )
    assert hc.scheduler_health(config, now) == "scheduler-overdue"
    hc.write_json(
        Path(config["receipt_path"]),
        {"started_at": (now - timedelta(hours=13)).isoformat(), "running": True},
    )
    assert hc.scheduler_health(config, now) == "run-timeout"


def test_network_failure_no_secrets(config, health, monkeypatch, capsys):
    opener = MagicMock()
    opener.open.side_effect = RuntimeError(config["scheduler_url"])
    monkeypatch.setattr(hc.urllib.request, "build_opener", lambda *args: opener)
    assert hc.report(config) == 1
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert "delivery-failed" in output.err


def test_dry_report_no_ping(config, health, monkeypatch):
    ping = MagicMock()
    monkeypatch.setattr(hc, "ping", ping)
    assert hc.report(config, dry_run=True) == 0
    ping.assert_not_called()


def test_reject_redirect():
    assert (
        hc.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere")
        is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://healthchecks.hopperhosted.com/ping/a",
        "https://evil.example/ping/a",
        "https://healthchecks.hopperhosted.com/ping/a?secret=x",
    ],
)
def test_reject_endpoint(config, tmp_path, url):
    config["scheduler_url"] = url
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        hc.read_config(path)


@pytest.mark.parametrize(
    "env",
    [
        {"NIGHTOWL_DISABLE_PUSHOVER": "1"},
        {"NIGHTOWL_MONITORING_BACKEND": "healthchecks"},
    ],
)
def test_disable_push(env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    credentials = MagicMock()
    monkeypatch.setattr(pushover, "_read_credentials", credentials)
    assert not pushover.send_failure_notification(
        [{"result": "failure"}], logging.getLogger()
    )
    credentials.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_monitoring_prompt_override(agent, tmp_path, monkeypatch):
    invoke = MagicMock()
    monkeypatch.setattr(runner, "_run", invoke)
    task = Task("a", "a", timedelta(days=1), "Email health status.", agent=agent)
    runner._run_agent_task(
        task,
        tmp_path,
        logging.getLogger(),
        {"NIGHTOWL_MONITORING_BACKEND": "healthchecks"},
    )
    prompt = invoke.call_args.args[0][-1]
    assert "overrides task instructions" in prompt
    assert "Substantive deliverables" in prompt
    assert task.prompt == "Email health status."


def test_completed_long_batch(config):
    from pathlib import Path

    now = datetime.now().astimezone()
    hc.write_json(
        Path(config["receipt_path"]),
        {
            "started_at": (now - timedelta(hours=8)).isoformat(),
            "finished_at": now.isoformat(),
            "running": False,
            "exit_code": 0,
        },
    )
    assert hc.scheduler_health(config, now) == "healthy"


@pytest.mark.parametrize(
    "body,expected",
    [(b"OK", True), (b"OK (not found)", False), (b"OK (rate limited)", False)],
)
def test_ping_response(body, expected, monkeypatch):
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = body
    monkeypatch.setattr(hc.urllib.request, "build_opener", lambda *args: opener)
    assert (
        hc.ping("https://healthchecks.hopperhosted.com/ping/secret", True) == expected
    )


def test_unmonitored_task(config, health):
    config["task_urls"] = {}
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["scheduler"]
        == "unmonitored-task"
    )


def test_lost_state_uses_existing_history(config, health):
    _, history = health
    history.append(
        {"task_id": "a", "result": "success", "ended_at": "2026-09-10T00:00:00+00:00"}
    )
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["a"]
        == "task-overdue"
    )


def test_future_state(config, health):
    states, _ = health
    states["a"] = {"result": "success", "last_run": "2099-01-01T00:00:00+00:00"}
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["a"]
        == "state-unavailable"
    )


def test_retry_state_write_race(config, health, monkeypatch):
    read = MagicMock(side_effect=[json.JSONDecodeError("partial", "", 0), {}])
    monkeypatch.setattr(hc, "get_all_task_states", read)
    monkeypatch.setattr(hc.time, "sleep", lambda _: None)
    assert (
        hc.task_health(config, hc.stamp(config["initialized_at"]), {})["a"] == "healthy"
    )
    assert read.call_count == 2


def test_saturday_after_final_dispatch(config, health):
    states, _ = health
    states["a"] = {"result": "success", "last_run": "2026-09-18T23:30:00"}
    assert hc.task_health(config, hc.stamp("2026-09-20T20:00:00"), {})["a"] == "healthy"
    assert hc.task_health(config, hc.stamp("2026-09-21T12:17:00"), {})["a"] == "healthy"
    assert (
        hc.task_health(config, hc.stamp("2026-09-21T12:17:01"), {})["a"]
        == "task-overdue"
    )


def test_timeout_skip_success(config, health):
    states, _ = health
    cache = {}
    now = hc.stamp("2026-09-16T12:00:00")
    states["a"] = {"result": "started", "last_run": "2026-09-16T01:00:00"}
    assert hc.task_health(config, now, cache)["a"] == "task-timeout"
    states["a"] = {"result": "skipped_open_artifact", "last_run": "2026-09-16T11:00:00"}
    assert hc.task_health(config, now, cache)["a"] == "task-failed"
    states["a"] = {"result": "success", "last_run": "2026-09-16T11:30:00"}
    assert hc.task_health(config, now, cache)["a"] == "healthy"
