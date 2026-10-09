from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from logging import Logger
from pathlib import Path

from nightowl.config import SkipIfOpenCheck, Task
from nightowl.state import record_task_started

WORKTREE_ROOT = Path.home() / ".cache" / "nightowl" / "worktrees"
TASK_STATE_ROOT = Path.home() / ".local" / "state" / "nightowl"


def _task_state_dir(task_id: str) -> Path:
    """Per-task state directory that survives worktree teardown.

    The worktree is ``rmtree``-d at the end of every run, so anything a task
    writes inside it is gone. Tasks that need to persist state across runs
    (e.g. ``reddit-scout`` dedupe history) should read/write under
    ``$NIGHTOWL_STATE_DIR`` instead, which points here.
    """
    return TASK_STATE_ROOT / task_id


def _worktree_path(project_dir: Path, task_id: str) -> Path:
    """Path where the worktree for a given (project, task) is checked out."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", project_dir.name).strip("-").lower()
    return WORKTREE_ROOT / slug / task_id


def _run(
    cmd: list[str],
    cwd: Path,
    logger: Logger,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    stdin=None,
    log_output: bool = True,
) -> subprocess.CompletedProcess:
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=stdin,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise
    if log_output and result.stdout:
        logger.debug(result.stdout.rstrip())
    if log_output and result.stderr:
        logger.debug(result.stderr.rstrip())
    return result


def _generate_pr_metadata(
    task: Task,
    project_dir: Path,
    logger: Logger,
) -> tuple[str, str]:
    """Pick a PR title and body for the task's branch.

    If the task wrote its own title/body to its state dir
    (``$NIGHTOWL_STATE_DIR/pr_title.txt`` and ``$NIGHTOWL_STATE_DIR/pr_body.md``),
    use those. Tasks that produce structured PR content (preview links,
    verified numbers, social copy, etc.) can compose the full body and
    bypass the generic generator. The override files are consumed after
    reading so a subsequent run starts clean.

    Otherwise, fall back to asking Claude to summarize the diff in 2-5
    sentences.
    """
    state_dir = _task_state_dir(task.id)
    title_override = state_dir / "pr_title.txt"
    body_override = state_dir / "pr_body.md"

    if task.agent == "codex" and (
        not title_override.is_file() or not body_override.is_file()
    ):
        raise RuntimeError("Codex task did not provide PR title and body")

    if body_override.is_file():
        logger.info(f"Using task-provided PR body: {body_override}")
        body = body_override.read_text().strip()
        if title_override.is_file():
            title_text = title_override.read_text().strip()
            title = title_text.splitlines()[0] if title_text else ""
            title_override.unlink()
        else:
            title = task.name
        if not title or not body:
            raise RuntimeError("Task provided an empty PR title or body")
        body_override.unlink()
        return title, body + "\n\n---\n*Automated by nightowl*"

    diff_result = _run(
        ["git", "diff", "origin/main", "--stat"],
        cwd=project_dir,
        logger=logger,
    )
    full_diff = _run(
        ["git", "diff", "origin/main"],
        cwd=project_dir,
        logger=logger,
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
        [
            "claude",
            "-p",
            "--model",
            task.model,
            "--output-format",
            "text",
            "--",
            prompt,
        ],
        cwd=project_dir,
        logger=logger,
        timeout=120,
    )

    title = task.name
    body = "Automated by nightowl"

    if result.returncode == 0 and result.stdout:
        lines = result.stdout.strip().splitlines()
        for line in lines:
            if line.startswith("TITLE:"):
                title = line[len("TITLE:") :].strip()
            elif line.startswith("BODY:"):
                body = line[len("BODY:") :].strip()
                # Grab remaining lines as part of body
                idx = lines.index(line)
                if idx + 1 < len(lines):
                    body = body + "\n" + "\n".join(lines[idx + 1 :])
                break
        body += "\n\n---\n*Automated by nightowl*"
    else:
        logger.warning("Failed to generate PR metadata, using defaults")

    return title, body


def _get_working_diff(project_dir: Path, logger: Logger) -> str:
    """Get the current working diff against origin/main plus any new untracked files."""
    diff_result = _run(
        ["git", "diff", "origin/main"],
        cwd=project_dir,
        logger=logger,
    )
    diff_text = diff_result.stdout or ""

    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=project_dir,
        logger=logger,
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
    diff_text: str,
    project_dir: Path,
    logger: Logger,
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
        with tempfile.TemporaryDirectory(prefix="nightowl-factcheck-") as directory:
            output_path = Path(directory) / "verdict.txt"
            result = _run(
                [
                    "codex",
                    "exec",
                    "--ephemeral",
                    "--disable",
                    "hooks",
                    "-s",
                    "workspace-write",
                    "-c",
                    "sandbox_workspace_write.network_access=true",
                    *_codex_writable_args(dict(os.environ)),
                    "-o",
                    str(output_path),
                    "-C",
                    str(project_dir),
                    prompt,
                ],
                cwd=project_dir,
                logger=logger,
                timeout=300,
                stdin=subprocess.DEVNULL,
                log_output=False,
            )
            if result.returncode != 0:
                raise RuntimeError("Codex fact-check process failed")
            output = output_path.read_text().strip()
            if output == "VERDICT: PASS":
                return (True, "")
            if (
                output.startswith("VERDICT: ISSUES FOUND\n")
                and output.split("\n", 1)[1].strip()
            ):
                return (False, output)
            raise RuntimeError("Codex fact-check did not return a valid verdict")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Codex fact-check could not complete") from error


def _run_fact_check_loop(
    task: Task,
    project_dir: Path,
    logger: Logger,
) -> None:
    """Verify up to three times, allowing two repairs between verdicts."""
    max_iterations = 3
    state_dir = _task_state_dir(task.id)
    state_dir.mkdir(parents=True, exist_ok=True)
    claude_env = {**os.environ, "NIGHTOWL_STATE_DIR": str(state_dir)}

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

        if i == max_iterations:
            break

        logger.info(f"Fact-check found issues on iteration {i}, re-invoking Claude")

        fix_prompt = (
            f"Original task: {task.prompt}\n\n"
            f"A fact-checker found the following issues with your changes:\n\n"
            f"{feedback}\n\n"
            f"Please fix these factual issues in the code/documentation."
        )

        _run(
            [
                "claude",
                "-p",
                "--model",
                task.model,
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--",
                fix_prompt,
            ],
            cwd=project_dir,
            logger=logger,
            timeout=1800,
            env=claude_env,
        )

    raise RuntimeError(f"Fact-check did not pass after {max_iterations} iterations")


def _check_skip_if_open(
    task: Task,
    project_dir: Path,
    logger: Logger,
) -> str | None:
    """If any configured artifact check matches, return a human-readable reason.

    Returns ``None`` if no check matches (task should run). The runner uses
    this to skip a task when a previous run's PR or issue is still open and
    Tim hasn't reviewed it — piling on another artifact just creates noise.

    Each check shells out to ``gh`` and counts matching open artifacts. If
    the count is >= ``threshold``, the task is skipped. Any ``gh`` failure
    is logged and treated as "no match" so a transient API outage doesn't
    permanently wedge a task.
    """
    for check in task.skip_if_open:
        count, ref = _run_skip_check(check, task.id, project_dir, logger)
        if count is None:
            continue
        if count >= check.threshold:
            return (
                f"{check.type} matched {count} open artifact(s) "
                f"(threshold {check.threshold}): {ref}"
            )
    return None


def _run_skip_check(
    check: SkipIfOpenCheck,
    task_id: str,
    project_dir: Path,
    logger: Logger,
) -> tuple[int | None, str]:
    """Run one ``gh`` query for a skip check. Returns (count, ref_description).

    ``count`` is ``None`` if the query failed (treated as "no match").
    """
    if check.type == "pr-branch-prefix":
        value = check.value or task_id
        # Match either `nightowl/<task-id>` or `nightowl/<date>-<task-id>`
        # (the runner uses the date-prefixed form, but legacy branches and
        # custom slugs like `nightowl/trim-bloat-<page>` also count).
        cmd = [
            "gh",
            "pr",
            "list",
            "--author",
            "@me",
            "--state",
            "open",
            "--json",
            "headRefName,url",
        ]
        result = _run(cmd, cwd=project_dir, logger=logger, timeout=60)
        if result.returncode != 0:
            logger.warning(
                f"skip_if_open pr-branch-prefix check failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
            return None, ""
        try:
            prs = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning(f"skip_if_open pr-branch-prefix parse failed: {e}")
            return None, ""
        matches = [
            p
            for p in prs
            if isinstance(p, dict)
            and p.get("headRefName", "").startswith("nightowl/")
            and value in p.get("headRefName", "")
        ]
        if matches:
            first = matches[0]
            ref = str(first.get("url") or first.get("headRefName") or "")
        else:
            ref = ""
        return len(matches), ref

    if check.type == "issue-label":
        value = check.value or f"source:{task_id}"
        cmd = [
            "gh",
            "issue",
            "list",
            "--label",
            value,
            "--state",
            "open",
            "--json",
            "number,url",
        ]
        result = _run(cmd, cwd=project_dir, logger=logger, timeout=60)
        if result.returncode != 0:
            logger.warning(
                f"skip_if_open issue-label check failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
            return None, ""
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning(f"skip_if_open issue-label parse failed: {e}")
            return None, ""
        ref = issues[0].get("url", "") if issues else ""
        return len(issues), ref

    if check.type == "issue-title":
        value = check.value or task_id
        cmd = [
            "gh",
            "issue",
            "list",
            "--author",
            "@me",
            "--state",
            "open",
            "--search",
            f"{value} in:title",
            "--json",
            "number,url",
        ]
        result = _run(cmd, cwd=project_dir, logger=logger, timeout=60)
        if result.returncode != 0:
            logger.warning(
                f"skip_if_open issue-title check failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
            return None, ""
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning(f"skip_if_open issue-title parse failed: {e}")
            return None, ""
        ref = issues[0].get("url", "") if issues else ""
        return len(issues), ref

    # Unreachable — SkipIfOpenCheck validates type at construction.
    return None, ""


def _cleanup_worktree(
    project_dir: Path,
    worktree_path: Path,
    branch: str,
    logger: Logger,
) -> None:
    """Remove the worktree directory and branch, tolerating partial state."""
    _run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=project_dir,
        logger=logger,
    )
    _run(["git", "worktree", "prune"], cwd=project_dir, logger=logger)
    if worktree_path.exists():
        # Worktree dir survived registration removal (e.g. it was orphaned).
        try:
            shutil.rmtree(worktree_path)
        except OSError as e:
            logger.warning(f"Could not remove worktree dir {worktree_path}: {e}")
    _run(["git", "branch", "-D", branch], cwd=project_dir, logger=logger)


def _codex_writable_args(env: dict[str, str]) -> list[str]:
    """Grant only explicitly configured existing directories to Codex."""
    paths = []
    if env.get("NIGHTOWL_STATE_DIR"):
        paths.append(env["NIGHTOWL_STATE_DIR"])
    if "NIGHTOWL_WRITABLE_DIRS" in env:
        extra = env["NIGHTOWL_WRITABLE_DIRS"].split(os.pathsep)
        if any(not path.strip() for path in extra):
            raise ValueError("NIGHTOWL_WRITABLE_DIRS contains an empty entry")
        paths.extend(extra)
    args = []
    for value in dict.fromkeys(paths):
        path = Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Codex writable directories must exist and be absolute")
        args.extend(["--add-dir", str(path)])
    return args


def _save_codex_outcome(env: dict[str, str], payload: dict) -> None:
    """Retain only the structured result privately, replacing stale diagnostics."""
    if not env.get("NIGHTOWL_STATE_DIR"):
        return
    destination = Path(env["NIGHTOWL_STATE_DIR"]) / "last-codex-outcome.json"
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".codex-outcome-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _push_branch(project_dir: Path, branch: str, logger: Logger) -> None:
    """Publish, or recognize an identical tree already published by the agent."""
    pushed = _run(
        ["git", "push", "-u", "origin", branch], cwd=project_dir, logger=logger
    )
    if pushed.returncode == 0:
        return
    # Agents sometimes publish through the GitHub API because their Git metadata
    # is read-only. Independent commits may then contain exactly the same files.
    fetched = _run(
        ["git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}"],
        cwd=project_dir,
        logger=logger,
    )
    if fetched.returncode == 0:
        local = _run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=project_dir, logger=logger
        )
        remote = _run(
            ["git", "rev-parse", "FETCH_HEAD^{tree}"], cwd=project_dir, logger=logger
        )
        if (
            local.returncode == remote.returncode == 0
            and local.stdout.strip()
            and local.stdout.strip() == remote.stdout.strip()
        ):
            logger.info("Remote branch already contains the exact validated file tree")
            return
    raise RuntimeError(f"git push failed: {pushed.stderr}")


def _open_pr(project_dir: Path, branch: str, logger: Logger) -> dict | None:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url,isDraft,labels",
        ],
        cwd=project_dir,
        logger=logger,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr}")
    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise RuntimeError("gh pr list returned invalid JSON") from error
    if not isinstance(prs, list) or len(prs) > 1:
        raise RuntimeError("gh pr list returned an unexpected PR set")
    return prs[0] if prs else None


def _check_pr(pr: dict, labels: list[str]) -> str:
    if not isinstance(pr, dict) or not pr.get("url") or pr.get("isDraft") is not True:
        raise RuntimeError("Task PR is missing or is not a draft")
    actual_labels = {label.get("name") for label in pr.get("labels", [])}
    if not set(labels).issubset(actual_labels):
        raise RuntimeError("Task PR is missing required labels")
    return str(pr["url"])


def _verify_direct_pr(
    task: Task, project_dir: Path, branch: str, logger: Logger
) -> str:
    """Require a clean, pushed task branch and an open draft PR."""
    status = _run(["git", "status", "--porcelain"], cwd=project_dir, logger=logger)
    current = _run(["git", "branch", "--show-current"], cwd=project_dir, logger=logger)
    head = _run(["git", "rev-parse", "HEAD"], cwd=project_dir, logger=logger)
    remote = _run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=project_dir,
        logger=logger,
        timeout=60,
    )
    if any(result.returncode != 0 for result in (status, current, head, remote)):
        raise RuntimeError("Could not verify task branch publication")
    if status.stdout.strip() or current.stdout.strip() != branch:
        raise RuntimeError("Task left uncommitted changes or switched branches")
    remote_sha = remote.stdout.split()[0] if remote.stdout.strip() else None
    if not remote_sha or remote_sha != head.stdout.strip():
        raise RuntimeError("Task branch was not pushed at the reviewed commit")
    pr = _open_pr(project_dir, branch, logger)
    if pr is None:
        raise RuntimeError("Task did not open a PR")
    return _check_pr(pr, task.pr_labels)


def _run_agent_task(
    task: Task,
    project_dir: Path,
    logger: Logger,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run the configured task agent in a non-interactive launchd-safe mode."""
    prompt = task.prompt
    if task.agent == "claude":
        logger.info("Running claude...")
        return _run(
            [
                "claude",
                "-p",
                "--model",
                task.model,
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--",
                prompt,
            ],
            cwd=project_dir,
            logger=logger,
            timeout=1800,
            env=env,
        )

    if task.agent == "codex":
        logger.info("Running codex...")
        if task.output in ("pr", "commit"):
            prompt += (
                "\n\nNightowl publication handoff (overrides task and repository "
                "instructions to commit, push, or create a PR): Git metadata is "
                "intentionally read-only inside this sandbox. The Nightowl runner "
                "owns staging, committing, pushing, and PR creation after you return. "
                "Do not attempt those operations, create another checkout, or publish "
                "through the GitHub API as a workaround. Complete the requested "
                "file changes and all validation, then leave the validated edits in "
                "this worktree. Do not restore them just because Git writes are "
                "unavailable. For PR output, write the full required PR description "
                "to $NIGHTOWL_STATE_DIR/pr_body.md and its title to "
                "$NIGHTOWL_STATE_DIR/pr_title.txt. Include required preview links "
                "and issue references. Report completed when the validated files "
                "and publication handoff are ready; the runner separately verifies "
                "publication before recording task success. Read-only Git/GitHub "
                "inspection remains available. Real validation or prerequisite "
                "failures must still report blocked or failed."
            )
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--disable",
            "hooks",
            "-s",
            "workspace-write",
            "-c",
            f'model_reasoning_effort="{task.codex_reasoning_effort}"',
            "-c",
            "sandbox_workspace_write.network_access=true",
        ]
        cmd.extend(_codex_writable_args(env))
        if task.git_write:
            git_dir = _run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=project_dir,
                logger=logger,
            )
            if git_dir.returncode != 0:
                raise RuntimeError("Could not locate Git metadata for Codex task")
            path = Path(git_dir.stdout.strip())
            if not path.is_dir():
                raise RuntimeError("Codex Git metadata directory does not exist")
            cmd.extend(["--add-dir", str(path)])
        if task.codex_model is not None:
            cmd.extend(["--model", task.codex_model])
        prompt += (
            "\n\nReturn the required structured outcome. Use completed only when "
            "the requested work and required checks are complete; a verified finding "
            "that no work is needed also counts as completed. Use blocked when a "
            "required prerequisite, permission, credential, network service, or tool "
            "is unavailable. Use failed when attempted work fails. Never report "
            "completed merely because you explained a blocker. Summarize the result "
            "briefly without credentials, tokens, or secret URLs."
        )
        schema = {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["completed", "blocked", "failed"],
                },
                "summary": {"type": "string"},
            },
            "required": ["outcome", "summary"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory(prefix="nightowl-outcome-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "outcome.json"
            schema_path.write_text(json.dumps(schema))
            cmd.extend(
                [
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--",
                    prompt,
                ]
            )
            _save_codex_outcome(
                env,
                {
                    "outcome": "failed",
                    "summary": "No validated outcome received for the current invocation.",
                },
            )
            result = _run(
                cmd,
                cwd=project_dir,
                logger=logger,
                timeout=1800,
                env=env,
                stdin=subprocess.DEVNULL,
                log_output=False,
            )
            if result.returncode != 0:
                return result
            payload: dict = {}
            try:
                payload = json.loads(output_path.read_text())
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"outcome", "summary"}
                    or payload["outcome"] not in ("completed", "blocked", "failed")
                    or not isinstance(payload["summary"], str)
                    or not payload["summary"].strip()
                ):
                    raise ValueError("invalid outcome")
                outcome = payload["outcome"]
            except (OSError, ValueError, TypeError):
                outcome = "invalid-output"
            # Do not put free-form agent output in operational logs: it may contain
            # credentials despite the prompt. Fixed summaries make failures safe.
            summaries = {
                "completed": "Codex confirmed the requested work is complete.",
                "blocked": "Codex reported an unavailable prerequisite.",
                "failed": "Codex reported that the requested work failed.",
                "invalid-output": "Codex did not return a valid structured outcome.",
            }
            summary = summaries[outcome]
            _save_codex_outcome(
                env,
                {"outcome": "failed", "summary": summary}
                if outcome == "invalid-output"
                else payload,
            )
            logger.info("Codex outcome %s: %s", outcome, summary)
            return subprocess.CompletedProcess(
                result.args,
                0 if outcome == "completed" else 1,
                stdout=summary,
                stderr="" if outcome == "completed" else summary,
            )

    raise ValueError(f"Unsupported task agent: {task.agent!r}")


def _parse_claude_envelope(stdout: str | None) -> dict:
    """Pull cost and token fields out of claude's ``--output-format json`` envelope.

    Returns a dict with ``claude_cost_usd``, ``claude_input_tokens``,
    ``claude_output_tokens``, and ``claude_cache_read_tokens``. Any field
    the envelope doesn't carry (or that we can't parse because claude
    crashed before emitting JSON) comes back as ``None`` — the run record
    captures the best-effort fields and the runner moves on.
    """
    keys = (
        "claude_cost_usd",
        "claude_input_tokens",
        "claude_output_tokens",
        "claude_cache_read_tokens",
    )
    empty = dict.fromkeys(keys)
    if not stdout:
        return empty
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(envelope, dict):
        return empty
    usage = envelope.get("usage") or {}
    return {
        "claude_cost_usd": envelope.get("total_cost_usd"),
        "claude_input_tokens": usage.get("input_tokens"),
        "claude_output_tokens": usage.get("output_tokens"),
        "claude_cache_read_tokens": usage.get("cache_read_input_tokens"),
    }


def run_task(task: Task, project_dir: Path, logger: Logger) -> dict:
    """Execute a single task. Returns a dict with result info.

    Each run happens in a throwaway git worktree under WORKTREE_ROOT. The main
    checkout in ``project_dir`` is never modified — so a nightowl run can't
    clobber uncommitted edits a developer left in the project working tree.

    The returned dict always carries ``started_at``, ``ended_at``,
    ``duration_s``, and the four ``claude_*`` fields (which may be
    ``None`` if the envelope couldn't be parsed) in addition to the
    ``result``, ``pr_url``, and ``error`` fields the caller already
    relied on.
    """
    started_at = datetime.now()
    date_str = started_at.strftime("%Y%m%d")
    # Date before task.id so that hosts that truncate branch slugs (e.g.
    # Cloudflare Pages preview aliases cut at 28 chars) keep the date in
    # the slug and don't collide across daily runs of the same task.
    branch = f"nightowl/{date_str}-{task.id}"
    worktree_path = _worktree_path(project_dir, task.id)

    logger.info(f"--- Task: {task.name} ({task.id}) ---")
    logger.info(f"Branch: {branch}")
    logger.info(f"Worktree: {worktree_path}")

    # Check skip_if_open BEFORE marking the task as started. A skip
    # shouldn't consume the interval — the task simply didn't run, so
    # the next eligible window should still fire normally.
    skip_reason = _check_skip_if_open(task, project_dir, logger)
    if skip_reason is not None:
        logger.info(f"Task {task.id} skipping: {skip_reason}")
        return {"result": "skipped_open_artifact", "skip_reason": skip_reason}

    record_task_started(str(project_dir), task.id)

    claude_envelope_stdout: str | None = None
    outcome: dict = {"result": "failure", "error": "task did not complete"}

    try:
        # Fetch origin
        result = _run(["git", "fetch", "origin"], cwd=project_dir, logger=logger)
        if result.returncode != 0:
            raise RuntimeError(f"git fetch failed: {result.stderr}")

        # Clean up any leftover state from a prior crashed run
        _cleanup_worktree(project_dir, worktree_path, branch, logger)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create the worktree on a fresh branch from origin/main
        result = _run(
            [
                "git",
                "worktree",
                "add",
                str(worktree_path),
                "-b",
                branch,
                "origin/main",
            ],
            cwd=project_dir,
            logger=logger,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")

        # Snapshot untracked files before the agent runs
        pre_untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree_path,
            logger=logger,
        )
        pre_untracked_set = set(pre_untracked.stdout.strip().splitlines())

        # Per-task state dir that survives worktree teardown.
        state_dir = _task_state_dir(task.id)
        state_dir.mkdir(parents=True, exist_ok=True)
        if task.agent == "codex" and task.output == "pr":
            for filename in ("pr_title.txt", "pr_body.md"):
                (state_dir / filename).unlink(missing_ok=True)
        agent_env = {**os.environ, "NIGHTOWL_STATE_DIR": str(state_dir)}

        # Run the configured agent (30 min timeout per task).
        agent_result = _run_agent_task(
            task,
            worktree_path,
            logger,
            env=agent_env,
        )
        logger.info(f"{task.agent} exit code: {agent_result.returncode}")
        if task.agent == "claude":
            claude_envelope_stdout = agent_result.stdout

        if agent_result.returncode != 0:
            safe_failures = {
                "Codex reported an unavailable prerequisite.",
                "Codex reported that the requested work failed.",
                "Codex did not return a valid structured outcome.",
            }
            if task.agent == "codex" and agent_result.stderr in safe_failures:
                raise RuntimeError(agent_result.stderr)
            raise RuntimeError(
                f"{task.agent} exited with code {agent_result.returncode}"
            )

        if task.fact_check:
            _run_fact_check_loop(task, worktree_path, logger)

        # Check if Claude already committed (branch ahead of origin/main)
        rev_list = _run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            cwd=worktree_path,
            logger=logger,
        )
        commits_ahead = int(rev_list.stdout.strip() or "0")

        # Include staged and unstaged tracked changes.
        wt_diff = _run(
            ["git", "diff", "HEAD", "--name-only"],
            cwd=worktree_path,
            logger=logger,
        )
        uncommitted_files = [f for f in wt_diff.stdout.strip().splitlines() if f]
        # Find new untracked files the agent created
        post_untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree_path,
            logger=logger,
        )
        new_files = [
            f
            for f in post_untracked.stdout.strip().splitlines()
            if f and f not in pre_untracked_set
        ]
        uncommitted_files.extend(new_files)

        has_changes = commits_ahead > 0 or bool(uncommitted_files)

        pr_url = None

        if has_changes and task.output in ("pr", "commit"):
            # Require the Codex handoff before publishing a branch. A missing
            # description must not leave an orphaned remote branch.
            codex_pr_metadata = (
                _generate_pr_metadata(task, worktree_path, logger)
                if task.agent == "codex" and task.output == "pr"
                else None
            )
            # Only add/commit if there are uncommitted changes
            if uncommitted_files:
                staged = _run(
                    ["git", "add", "--"] + uncommitted_files,
                    cwd=worktree_path,
                    logger=logger,
                )
                if staged.returncode != 0:
                    raise RuntimeError(f"git add failed: {staged.stderr}")
                result = _run(
                    ["git", "commit", "-m", task.name],
                    cwd=worktree_path,
                    logger=logger,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git commit failed: {result.stderr}")
            else:
                logger.info("Claude already committed all changes, skipping add/commit")

            # Push
            _push_branch(worktree_path, branch, logger)

            # Create PR if output is pr
            if task.output == "pr":
                # If the agent already opened a PR for this branch (some task
                # prompts call `gh pr create` themselves), adopt it instead of
                # firing a duplicate `gh pr create` that would fail with
                # "a pull request for branch ... already exists".
                existing = _open_pr(worktree_path, branch, logger)
                if existing is not None:
                    pr_url = (
                        _check_pr(existing, task.pr_labels)
                        if task.agent == "codex"
                        else str(existing["url"])
                    )
                    logger.info(f"PR already opened by task: {pr_url}")
                else:
                    pr_title, pr_body = codex_pr_metadata or _generate_pr_metadata(
                        task, worktree_path, logger
                    )
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".md"
                    ) as body_file:
                        body_file.write(pr_body)
                        body_file.flush()
                        command = [
                            "gh",
                            "pr",
                            "create",
                            "--title",
                            pr_title,
                            "--body-file",
                            body_file.name,
                        ]
                        if task.agent == "codex":
                            command.append("--draft")
                        for label in task.pr_labels:
                            command.extend(["--label", label])
                        result = _run(command, cwd=worktree_path, logger=logger)
                    if result.returncode != 0:
                        raise RuntimeError(f"gh pr create failed: {result.stderr}")
                    pr_url = result.stdout.strip()
                    logger.info(f"PR created: {pr_url}")
        elif task.require_pr and has_changes:
            pr_url = _verify_direct_pr(task, worktree_path, branch, logger)
        elif not has_changes:
            logger.info(f"No changes made by {task.agent}.")

        outcome = {"result": "success"}
        if pr_url:
            outcome["pr_url"] = pr_url

    except Exception as e:
        logger.error(f"Task {task.id} failed: {e}")
        outcome = {"result": "failure", "error": str(e)}

    finally:
        _cleanup_worktree(project_dir, worktree_path, branch, logger)

    ended_at = datetime.now()
    outcome["task_id"] = task.id
    outcome["started_at"] = started_at.isoformat(timespec="seconds")
    outcome["ended_at"] = ended_at.isoformat(timespec="seconds")
    outcome["duration_s"] = int((ended_at - started_at).total_seconds())
    outcome.setdefault("pr_url", None)
    outcome.setdefault("error", None)
    outcome.update(_parse_claude_envelope(claude_envelope_stdout))
    return outcome
