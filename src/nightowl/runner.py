from __future__ import annotations

import json
import subprocess
import tempfile
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


def _get_working_diff(project_dir: Path, logger: Logger) -> str:
    """Get the current working diff against origin/main plus any new untracked files."""
    diff_result = _run(
        ["git", "diff", "origin/main"],
        cwd=project_dir, logger=logger,
    )
    diff_text = diff_result.stdout or ""

    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=project_dir, logger=logger,
    )
    if untracked.stdout and untracked.stdout.strip():
        diff_text += "\n\n--- New untracked files ---\n"
        for fpath in untracked.stdout.strip().splitlines():
            diff_text += f"\n+++ {fpath}\n"
            try:
                content = (project_dir / fpath).read_text()
                diff_text += content[:2000]
            except Exception:
                diff_text += "(could not read file)\n"

    return diff_text[:12000]


def _run_codex_fact_check(
    diff_text: str, project_dir: Path, logger: Logger,
) -> tuple[bool, str]:
    """Run Codex to fact-check the given diff. Returns (passed, feedback)."""
    prompt = (
        "Review the following code diff for factual accuracy. "
        "Check any claims, comments, documentation, URLs, or data for correctness. "
        "Respond with exactly one of:\n"
        "VERDICT: PASS\n"
        "or\n"
        "VERDICT: ISSUES FOUND\n"
        "followed by a numbered list of specific factual issues.\n\n"
        f"Diff:\n{diff_text}"
    )

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False,
        ) as tmp:
            tmp_path = tmp.name

        result = _run(
            [
                "codex", "exec", "--ephemeral",
                "-o", tmp_path,
                "-C", str(project_dir),
                prompt,
            ],
            cwd=project_dir, logger=logger, timeout=300,
        )

        if result.returncode != 0:
            logger.warning(f"Codex exited with code {result.returncode}, treating as pass")
            return (True, "")

        output = Path(tmp_path).read_text().strip()
        logger.debug(f"Codex output: {output}")

        if "VERDICT: PASS" in output:
            return (True, "")
        elif "VERDICT: ISSUES FOUND" in output:
            return (False, output)
        else:
            logger.warning("Codex output did not contain a recognized verdict, treating as pass")
            return (True, "")

    except (subprocess.TimeoutExpired, Exception) as e:
        logger.warning(f"Codex fact-check failed ({e}), treating as pass")
        return (True, "")


def _run_fact_check_loop(
    task: Task, project_dir: Path, logger: Logger,
) -> None:
    """Run up to 3 rounds of Codex fact-checking, re-invoking Claude to fix issues."""
    max_iterations = 3

    for i in range(1, max_iterations + 1):
        logger.info(f"Fact-check iteration {i}/{max_iterations}")

        diff_text = _get_working_diff(project_dir, logger)
        if not diff_text.strip():
            logger.info("No diff to fact-check, skipping")
            return

        passed, feedback = _run_codex_fact_check(diff_text, project_dir, logger)

        if passed:
            logger.info(f"Fact-check passed on iteration {i}")
            return

        logger.info(f"Fact-check found issues on iteration {i}, re-invoking Claude")

        fix_prompt = (
            f"Original task: {task.prompt}\n\n"
            f"A fact-checker found the following issues with your changes:\n\n"
            f"{feedback}\n\n"
            f"Please fix these factual issues in the code/documentation."
        )

        _run(
            [
                "claude", "-p", "--dangerously-skip-permissions",
                "--output-format", "json",
                "--", fix_prompt,
            ],
            cwd=project_dir, logger=logger, timeout=1800,
        )

    logger.warning(f"Fact-check did not pass after {max_iterations} iterations, proceeding anyway")


def run_task(task: Task, project_dir: Path, logger: Logger) -> dict:
    """Execute a single task. Returns a dict with result info."""
    date_str = datetime.now().strftime("%Y%m%d")
    # Date before task.id so that hosts that truncate branch slugs (e.g.
    # Cloudflare Pages preview aliases cut at 28 chars) keep the date in
    # the slug and don't collide across daily runs of the same task.
    branch = f"nightowl/{date_str}-{task.id}"

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

        if task.fact_check:
            _run_fact_check_loop(task, project_dir, logger)

        # Check if Claude already committed (branch ahead of origin/main)
        rev_list = _run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            cwd=project_dir, logger=logger,
        )
        commits_ahead = int(rev_list.stdout.strip() or "0")

        # Find uncommitted working-tree changes (unstaged tracked files)
        wt_diff = _run(
            ["git", "diff", "--name-only"],
            cwd=project_dir, logger=logger,
        )
        uncommitted_files = [
            f for f in wt_diff.stdout.strip().splitlines() if f
        ]
        # Find new untracked files claude created
        post_untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir, logger=logger,
        )
        new_files = [
            f for f in post_untracked.stdout.strip().splitlines()
            if f and f not in pre_untracked_set
        ]
        uncommitted_files.extend(new_files)

        has_changes = commits_ahead > 0 or bool(uncommitted_files)

        pr_url = None

        if has_changes and task.output in ("pr", "commit"):
            # Only add/commit if there are uncommitted changes
            if uncommitted_files:
                _run(["git", "add", "--"] + uncommitted_files, cwd=project_dir, logger=logger)
                result = _run(
                    ["git", "commit", "-m", task.name],
                    cwd=project_dir,
                    logger=logger,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git commit failed: {result.stderr}")
            else:
                logger.info("Claude already committed all changes, skipping add/commit")

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
