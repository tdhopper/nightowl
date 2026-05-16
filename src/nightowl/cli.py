from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

from nightowl.config import WEEKDAY_DISPLAY, load_config
from nightowl.logging import setup_logging
from nightowl.pushover import send_failure_notification
from nightowl.runner import run_task
from nightowl.runs import append_run
from nightowl.state import (
    get_all_task_states,
    is_task_eligible,
    record_task_disappeared,
    record_task_result,
)
from nightowl.scheduler import install as scheduler_install, uninstall as scheduler_uninstall


@click.group()
def main():
    """nightowl - automated overnight tasks using Claude Code."""
    pass


@main.command()
@click.option("--task", "task_id", default=None, help="Run a single task by id, skipping interval check.")
@click.option("--dry-run", is_flag=True, help="Show eligible tasks without executing.")
def run(task_id: str | None, dry_run: bool):
    """Run eligible tasks."""
    logger = setup_logging()
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    project_path = str(Path.cwd())

    # Detect tasks that ran before but are no longer in the loaded config.
    # A markdown file that was renamed or deleted would silently stop running
    # without this; record_task_disappeared surfaces it in `nightowl status`.
    loaded_ids = {t.id for t in config.tasks}
    for known_id, known_state in get_all_task_states(project_path).items():
        if known_id in loaded_ids:
            continue
        if known_state.get("result") == "disappeared":
            continue
        logger.warning(
            f"Task {known_id!r} is in state but no longer in loaded config; "
            "marking as disappeared."
        )
        record_task_disappeared(project_path, known_id)

    if task_id:
        task = config.get_task(task_id)
        if not task:
            click.echo(f"Error: task '{task_id}' not found in config.", err=True)
            sys.exit(1)
        tasks_to_run = [task]
    else:
        today = datetime.now().weekday()
        if today in config.skip_weekdays:
            logger.info(
                f"Today is {WEEKDAY_DISPLAY[today]}, which is in skip_weekdays. "
                f"Skipping this run."
            )
            return
        tasks_to_run = [
            t for t in config.tasks
            if is_task_eligible(project_path, t.id, t.interval)
        ]

    if dry_run:
        if not tasks_to_run:
            click.echo("No tasks are currently eligible to run.")
        else:
            click.echo("Eligible tasks:")
            for t in tasks_to_run:
                click.echo(f"  - {t.id}: {t.name} (interval: {t.interval})")
        return

    if not tasks_to_run:
        logger.info("No tasks are currently eligible to run.")
        return

    any_failed = False
    run_records: list[dict] = []
    try:
        for task in tasks_to_run:
            result = run_task(task, Path.cwd(), logger)
            record_task_result(
                project_path,
                task.id,
                result["result"],
                pr_url=result.get("pr_url"),
                error=result.get("error"),
                skip_reason=result.get("skip_reason"),
            )
            append_run(project_path, result)
            run_records.append(result)
            # `skipped_open_artifact` is not a failure — the previous run's
            # artifact is still open and the task deliberately didn't run.
            if result["result"] == "failure":
                any_failed = True
    finally:
        # Push to Pushover only if at least one task failed. Successful
        # runs are silent — the PR list in GitHub is signal enough.
        # Wrapped in the finally so a crash mid-loop that left a partial
        # ``run_records`` with a failure still alerts.
        send_failure_notification(run_records, logger, project_dir=project_path)

    if any_failed:
        sys.exit(1)


@main.command()
@click.option(
    "--stale",
    is_flag=True,
    help=(
        "Print only tasks where now - last_run > 2 * interval, or that have "
        "disappeared. Exits non-zero if any are stale (good for launchd / shell)."
    ),
)
def status(stale: bool):
    """Show task status for the current project."""
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    project_path = str(Path.cwd())
    states = get_all_task_states(project_path)

    if stale:
        _status_stale(config, project_path, states)
        return

    for task in config.tasks:
        click.echo(f"{task.id} ({task.name}):")
        task_state = states.get(task.id)
        if task_state is None:
            click.echo("  Last run: never")
            click.echo("  Eligible: yes")
        else:
            if "last_run" in task_state:
                click.echo(f"  Last run: {task_state['last_run']}")
            click.echo(f"  Result: {task_state['result']}")
            if task_state.get("pr_url"):
                click.echo(f"  PR: {task_state['pr_url']}")
            if task_state.get("error"):
                click.echo(f"  Error: {task_state['error']}")
            if task_state.get("skip_reason"):
                click.echo(f"  Skip reason: {task_state['skip_reason']}")
            eligible = is_task_eligible(project_path, task.id, task.interval)
            click.echo(f"  Eligible: {'yes' if eligible else 'no'}")
            if not eligible and "last_run" in task_state:
                last_run = datetime.fromisoformat(task_state["last_run"])
                next_run = last_run + task.interval
                click.echo(f"  Next eligible: {next_run.isoformat(timespec='seconds')}")
        click.echo()

    # Disappeared tasks: in state, but not in loaded config.
    loaded_ids = {t.id for t in config.tasks}
    for tid, ts in states.items():
        if tid in loaded_ids:
            continue
        if ts.get("result") != "disappeared":
            continue
        click.echo(f"{tid} (disappeared):")
        if "last_run" in ts:
            click.echo(f"  Last run: {ts['last_run']}")
        if "noticed_at" in ts:
            click.echo(f"  Noticed at: {ts['noticed_at']}")
        click.echo("  Result: disappeared")
        click.echo()


def _status_stale(config, project_path: str, states: dict) -> None:
    """Print only stale or disappeared tasks; exit non-zero if any exist."""
    now = datetime.now()
    stale_lines: list[str] = []

    for task in config.tasks:
        ts = states.get(task.id)
        if ts is None:
            continue
        if ts.get("result") == "disappeared":
            stale_lines.append(f"{task.id}: disappeared")
            continue
        last_run_str = ts.get("last_run")
        if not last_run_str:
            continue
        last_run = datetime.fromisoformat(last_run_str)
        threshold = task.interval * 2
        if now - last_run > threshold:
            age = now - last_run
            stale_lines.append(
                f"{task.id}: last_run {last_run_str} "
                f"({_format_duration(age)} ago, interval {task.interval})"
            )

    # Also surface disappeared tasks that aren't in the loaded config.
    loaded_ids = {t.id for t in config.tasks}
    for tid, ts in states.items():
        if tid in loaded_ids:
            continue
        if ts.get("result") == "disappeared":
            noticed = ts.get("noticed_at", "?")
            stale_lines.append(f"{tid}: disappeared (noticed {noticed})")

    if not stale_lines:
        return

    for line in stale_lines:
        click.echo(line)
    sys.exit(1)


def _format_duration(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


@main.command()
def install():
    """Install launchd plist for scheduled runs."""
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    plist_path = scheduler_install(
        Path.cwd(),
        config.window_start,
        config.window_end,
        cadence=config.cadence,
    )
    click.echo(f"Installed: {plist_path} (cadence: {config.cadence})")


@main.command()
def uninstall():
    """Remove launchd plist."""
    scheduler_uninstall(Path.cwd())
    click.echo("Uninstalled.")
