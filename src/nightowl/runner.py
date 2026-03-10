from __future__ import annotations

import json
import subprocess
from datetime import datetime
from logging import Logger
from pathlib import Path

from nightowl.config import Task


def _run(
    cmd: list[str],
    cwd: Path,
    logger: Logger,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise
    if result.stdout:
        logger.debug(result.stdout.rstrip())
    if result.stderr:
        logger.debug(result.stderr.rstrip())
    return result


def _generate_pr_metadata(
    task: Task, project_dir: Path, logger: Logger,
) -> tuple[str, str]:
    """Use Claude to generate an informative PR title and body from the diff."""
    diff_result = _run(
        ["git", "diff", "origin/main", "--stat"],
        cwd=project_dir, logger=logger,
    )
    full_diff = _run(
        ["git", "diff", "origin/main"],
        cwd=project_dir, logger=logger,
    )
    # Truncate diff to avoid blowing up context
    diff_text = full_diff.stdout[:8000] if full_diff.stdout else "(no diff)"
    stat_text = diff_result.stdout or "(no stat)"

    prompt = (
        "You are writing a GitHub PR title and description.\n"
        f"Task name: {task.name}\n"
        f"Task prompt: {task.prompt}\n\n"
        f"Diff stat:\n{stat_text}\n\n"
        f"Diff (may be truncated):\n{diff_text}\n\n"
        "Respond with EXACTLY this format, no other text:\n"
        "TITLE: <a concise, informative PR title under 72 chars>\n"
        "BODY: <a markdown description of what changed and why, 2-5 sentences>\n"
    )

    logger.info("Generating PR title and body with Claude...")
    result = _run(
        ["claude", "-p", "--output-format", "text", "--", prompt],
        cwd=project_dir, logger=logger, timeout=120,
    )

    title = task.name
    body = "Automated by nightowl"

    if result.returncode == 0 and result.stdout:
        lines = result.stdout.strip().splitlines()
        for line in lines:
            if line.startswith("TITLE:"):
                title = line[len("TITLE:"):].strip()
            elif line.startswith("BODY:"):
                body = line[len("BODY:"):].strip()
                # Grab remaining lines as part of body
                idx = lines.index(line)
                if idx + 1 < len(lines):
                    body = body + "\n" + "\n".join(lines[idx + 1:])
                break
        body += "\n\n---\n*Automated by nightowl*"
    else:
        logger.warning("Failed to generate PR metadata, using defaults")

    return title, body


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

        # Snapshot untracked files before claude runs
        pre_untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir, logger=logger,
        )
        pre_untracked_set = set(pre_untracked.stdout.strip().splitlines())

        # Run claude (30 min timeout per task)
        logger.info("Running claude...")
        claude_cmd = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--", task.prompt,
        ]
        claude_result = _run(
            claude_cmd, cwd=project_dir, logger=logger, timeout=1800,
        )
        logger.info(f"Claude exit code: {claude_result.returncode}")

        if claude_result.returncode != 0:
            raise RuntimeError(f"claude exited with code {claude_result.returncode}")

        # Find tracked files changed relative to origin/main
        diff_result = _run(
            ["git", "diff", "--name-only", "origin/main"],
            cwd=project_dir, logger=logger,
        )
        changed_files = [
            f for f in diff_result.stdout.strip().splitlines() if f
        ]
        # Find new files claude created (untracked now but not before)
        post_untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir, logger=logger,
        )
        new_files = [
            f for f in post_untracked.stdout.strip().splitlines()
            if f and f not in pre_untracked_set
        ]
        changed_files.extend(new_files)

        has_changes = bool(changed_files)

        pr_url = None

        if has_changes and task.output in ("pr", "commit"):
            # Stage only files claude changed or created
            _run(["git", "add", "--"] + changed_files, cwd=project_dir, logger=logger)
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
                pr_title, pr_body = _generate_pr_metadata(
                    task, project_dir, logger,
                )
                result = _run(
                    [
                        "gh", "pr", "create",
                        "--title", pr_title,
                        "--body", pr_body,
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
        # Clean up: discard any uncommitted changes then go back to main
        _run(["git", "checkout", "."], cwd=project_dir, logger=logger)
        _run(["git", "clean", "-fd"], cwd=project_dir, logger=logger)
        _run(["git", "checkout", "main"], cwd=project_dir, logger=logger)
