from __future__ import annotations

import json
import subprocess
from datetime import datetime
from logging import Logger
from pathlib import Path

from nightowl.config import Task


def _run(cmd: list[str], cwd: Path, logger: Logger) -> subprocess.CompletedProcess:
    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        logger.debug(result.stdout.rstrip())
    if result.stderr:
        logger.debug(result.stderr.rstrip())
    return result


def run_task(task: Task, project_dir: Path, logger: Logger) -> dict:
    """Execute a single task. Returns a dict with result info."""
    date_str = datetime.now().strftime("%Y%m%d")
    branch = f"nightowl/{task.id}-{date_str}"

    logger.info(f"--- Task: {task.name} ({task.id}) ---")
    logger.info(f"Branch: {branch}")

    try:
        # Fetch origin
        result = _run(["git", "fetch", "origin"], cwd=project_dir, logger=logger)
        if result.returncode != 0:
            raise RuntimeError(f"git fetch failed: {result.stderr}")

        # Delete existing branch if present
        _run(["git", "branch", "-D", branch], cwd=project_dir, logger=logger)

        # Create branch from origin/main
        result = _run(
            ["git", "checkout", "-b", branch, "origin/main"],
            cwd=project_dir,
            logger=logger,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed: {result.stderr}")

        # Run claude
        logger.info("Running claude...")
        claude_cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--project-dir", str(project_dir),
            "--prompt", task.prompt,
            "--output-format", "json",
            "--max-turns", "50",
        ]
        claude_result = _run(claude_cmd, cwd=project_dir, logger=logger)
        logger.info(f"Claude exit code: {claude_result.returncode}")

        if claude_result.returncode != 0:
            raise RuntimeError(f"claude exited with code {claude_result.returncode}")

        # Check for changes
        status_result = _run(
            ["git", "status", "--porcelain"], cwd=project_dir, logger=logger
        )
        has_changes = bool(status_result.stdout.strip())

        pr_url = None

        if has_changes and task.output in ("pr", "commit"):
            # Commit
            _run(["git", "add", "-A"], cwd=project_dir, logger=logger)
            result = _run(
                ["git", "commit", "-m", task.name],
                cwd=project_dir,
                logger=logger,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git commit failed: {result.stderr}")

            # Push
            result = _run(
                ["git", "push", "-u", "origin", branch],
                cwd=project_dir,
                logger=logger,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git push failed: {result.stderr}")

            # Create PR if output is pr
            if task.output == "pr":
                result = _run(
                    [
                        "gh", "pr", "create",
                        "--title", task.name,
                        "--body", "Automated by nightowl",
                    ],
                    cwd=project_dir,
                    logger=logger,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"gh pr create failed: {result.stderr}")
                pr_url = result.stdout.strip()
                logger.info(f"PR created: {pr_url}")
        elif not has_changes:
            logger.info("No changes made by claude.")

        info = {"result": "success"}
        if pr_url:
            info["pr_url"] = pr_url
        return info

    except Exception as e:
        logger.error(f"Task {task.id} failed: {e}")
        return {"result": "failure", "error": str(e)}

    finally:
        # Clean up: go back to main
        _run(["git", "checkout", "main"], cwd=project_dir, logger=logger)
