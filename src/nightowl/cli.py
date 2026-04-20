from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import click

from nightowl.config import WEEKDAY_DISPLAY, load_config
from nightowl.logging import setup_logging
from nightowl.runner import run_task
from nightowl.state import (
    get_all_task_states,
    is_task_eligible,
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
    for task in tasks_to_run:
        result = run_task(task, Path.cwd(), logger)
        record_task_result(
            project_path,
            task.id,
            result["result"],
            pr_url=result.get("pr_url"),
            error=result.get("error"),
        )
        if result["result"] == "failure":
            any_failed = True

    if any_failed:
        sys.exit(1)


@main.command()
def status():
    """Show task status for the current project."""
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    project_path = str(Path.cwd())
    states = get_all_task_states(project_path)

    for task in config.tasks:
        click.echo(f"{task.id} ({task.name}):")
        task_state = states.get(task.id)
        if task_state is None:
            click.echo("  Last run: never")
            click.echo("  Eligible: yes")
        else:
            click.echo(f"  Last run: {task_state['last_run']}")
            click.echo(f"  Result: {task_state['result']}")
            if task_state.get("pr_url"):
                click.echo(f"  PR: {task_state['pr_url']}")
            if task_state.get("error"):
                click.echo(f"  Error: {task_state['error']}")
            eligible = is_task_eligible(project_path, task.id, task.interval)
            click.echo(f"  Eligible: {'yes' if eligible else 'no'}")
            if not eligible:
                last_run = datetime.fromisoformat(task_state["last_run"])
                next_run = last_run + task.interval
                click.echo(f"  Next eligible: {next_run.isoformat(timespec='seconds')}")
        click.echo()


@main.command()
def install():
    """Install launchd plist for scheduled runs."""
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    plist_path = scheduler_install(Path.cwd(), config.window_start, config.window_end)
    click.echo(f"Installed: {plist_path}")


@main.command()
def uninstall():
    """Remove launchd plist."""
    scheduler_uninstall(Path.cwd())
    click.echo("Uninstalled.")
