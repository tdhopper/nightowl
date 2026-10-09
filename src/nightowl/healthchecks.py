"""Report current Nightowl health independently of the task runner."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from nightowl.config import load_config
from nightowl.runs import read_runs
from nightowl.state import get_all_task_states


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    for key in (
        "project_path",
        "receipt_path",
        "scheduler_url",
        "task_urls",
        "initialized_at",
    ):
        if key not in config:
            raise ValueError("missing configuration field")
    stamp(config["initialized_at"])
    config.setdefault("scheduler_minute", 17)
    if (
        type(config["scheduler_minute"]) is not int
        or not 0 <= config["scheduler_minute"] <= 59
    ):
        raise ValueError("invalid scheduler minute")
    for key, default in {
        "scheduler_timeout_seconds": 5400,
        "max_run_seconds": 43200,
        "task_runtime_seconds": 10800,
        "dispatch_grace_seconds": 43200,
    }.items():
        config.setdefault(key, default)
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError("invalid duration")
    for url in [config["scheduler_url"], *config["task_urls"].values()]:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "healthchecks.hopperhosted.com"
            or not parsed.path.startswith("/ping/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Healthchecks endpoint")
    return config


def run(config: dict, command: list[str]) -> int:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("command required")
    path = Path(config["receipt_path"])
    receipt = {
        "started_at": datetime.now().astimezone().isoformat(),
        "running": True,
        "finished_at": None,
        "exit_code": None,
    }
    write_json(path, receipt)
    code = 127
    previous = {}
    try:
        child = subprocess.Popen(command, start_new_session=True)

        def forward(signum, frame):
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, forward)
        code = child.wait()
        if code < 0:
            code = 128 - code
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        receipt.update(
            running=False,
            finished_at=datetime.now().astimezone().isoformat(),
            exit_code=code,
        )
        write_json(path, receipt)
    return code


def scheduler_health(config: dict, now: datetime) -> str:
    try:
        receipt = json.loads(Path(config["receipt_path"]).read_text())
        age = (now - stamp(receipt["started_at"])).total_seconds()
        if receipt["running"]:
            return "running" if 0 <= age <= config["max_run_seconds"] else "run-timeout"
        if receipt["exit_code"] != 0:
            return "execution-failed"
        finished = stamp(receipt["finished_at"])
        if finished < stamp(receipt["started_at"]) or finished > now:
            return "receipt-unavailable"
        age = (now - finished).total_seconds()
        return (
            "healthy"
            if age <= config["scheduler_timeout_seconds"]
            else "scheduler-overdue"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "receipt-unavailable"


def deadline(
    last: datetime,
    interval: timedelta,
    skip_weekdays: list[int],
    grace: int,
    scheduler_minute: int = 17,
) -> datetime:
    due = last + interval
    dispatch = due.replace(minute=scheduler_minute, second=0, microsecond=0)
    if dispatch < due:
        dispatch += timedelta(hours=1)
    due = dispatch
    # Round to an actual hourly dispatch before excluding skipped weekdays.
    for _ in range(7):
        if due.weekday() not in skip_weekdays:
            return due + timedelta(seconds=grace)
        due = (due + timedelta(days=1)).replace(
            hour=0, minute=scheduler_minute, second=0, microsecond=0
        )
    raise ValueError("schedule excludes every weekday")


def task_health(config: dict, now: datetime, cache: dict) -> dict[str, str]:
    schedule = load_config(Path(config["project_path"]) / "nightowl")
    states: dict = {}
    for attempt in range(3):
        try:
            states = get_all_task_states(config["project_path"])
            break
        except json.JSONDecodeError:
            if attempt == 2:
                raise
            time.sleep(0.1)
    history = read_runs(config["project_path"])
    tasks = {task.id: task for task in schedule.tasks}
    result = {}
    for task_id in config["task_urls"]:
        if task_id not in tasks:
            result[task_id] = "task-missing"
            continue
        state = states.get(task_id, {})
        if state.get("last_run") and stamp(state["last_run"]) > now:
            result[task_id] = "state-unavailable"
            continue
        evidence = list(history)
        if state.get("last_run"):
            evidence.append(
                {
                    "task_id": task_id,
                    "ended_at": state["last_run"],
                    "result": state.get("result"),
                }
            )
        previous = cache.get(task_id)
        if previous:
            evidence.append({"task_id": task_id, **previous})
        terminal = [
            item
            for item in evidence
            if item.get("task_id") == task_id
            and item.get("result") in ("success", "failure")
            and item.get("ended_at")
        ]
        if terminal:
            latest = max(terminal, key=lambda item: stamp(item["ended_at"]))
            if stamp(latest["ended_at"]) > now:
                result[task_id] = "state-unavailable"
                continue
            cache[task_id] = {
                "ended_at": latest["ended_at"],
                "result": latest["result"],
            }
        if cache.get(task_id, {}).get("result") == "failure":
            result[task_id] = "task-failed"
        elif state.get("result") == "started":
            age = (now - stamp(state["last_run"])).total_seconds()
            result[task_id] = (
                "running"
                if 0 <= age <= config["task_runtime_seconds"]
                else "task-timeout"
            )
            if result[task_id] == "task-timeout":
                cache[task_id] = {"ended_at": state["last_run"], "result": "failure"}
        elif state.get("result") == "disappeared":
            result[task_id] = "task-missing"
        else:
            last = stamp(
                state.get("last_run")
                or cache.get(task_id, {}).get("ended_at")
                or config["initialized_at"]
            )
            if last > now:
                result[task_id] = "state-unavailable"
                continue
            due = deadline(
                last,
                tasks[task_id].interval,
                schedule.skip_weekdays,
                config["dispatch_grace_seconds"],
                config.get("scheduler_minute", 17),
            )
            result[task_id] = "task-overdue" if now > due else "healthy"
    # New task files require an explicit check; alert via scheduler until provisioned.
    if set(tasks) - set(config["task_urls"]):
        result["scheduler"] = "unmonitored-task"
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ping(url: str, healthy: bool, status: str = "") -> bool:
    try:
        request = urllib.request.Request(
            url.rstrip("/") + ("" if healthy else "/fail"),
            data=status.encode("utf-8"),
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=10
        ) as response:
            return response.status == 200 and response.read(64) == b"OK"
    except Exception:
        # Exception strings can contain credential-bearing URLs or response bodies.
        return False


def report(config: dict, dry_run: bool = False) -> int:
    now = datetime.now().astimezone()
    cache_name = config.get(
        "health_cache_path", config["receipt_path"] + ".health.json"
    )
    if not isinstance(cache_name, str):
        raise ValueError("invalid health cache path")
    cache_path = Path(cache_name)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    statuses = {"scheduler": scheduler_health(config, now)}
    try:
        statuses.update(task_health(config, now, cache))
        if not dry_run:
            write_json(cache_path, cache)
    except Exception:
        statuses.update(dict.fromkeys(config["task_urls"], "state-unavailable"))
        statuses["scheduler"] = "state-unavailable"
    delivered = True
    for name, url in {
        "scheduler": config["scheduler_url"],
        **config["task_urls"],
    }.items():
        status = statuses[name]
        print(f"{name}: {status}")
        if not dry_run and not ping(url, status in ("healthy", "running"), status):
            print(f"{name}: delivery-failed", file=sys.stderr)
            delivered = False
    return 0 if delivered else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("run", "report"):
        child = sub.add_parser(action)
        child.add_argument("--config", type=Path, required=True)
        if action == "run":
            child.add_argument("command", nargs=argparse.REMAINDER)
        else:
            child.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        return (
            run(config, args.command)
            if args.action == "run"
            else report(config, args.dry_run)
        )
    except Exception:
        print(
            "Healthchecks: operation failed; inspect configuration and local state",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
