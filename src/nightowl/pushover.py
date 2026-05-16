"""Failure-only Pushover notifications.

After every ``nightowl run`` that produced at least one failed task,
the runner sends a single Pushover push so Tim's phone wakes up only
when something actually broke. Successful runs are silent — the PR
list in GitHub is signal enough.

Credentials live in two mode-600 files because launchd does not load
direnv when the plist fires, so ``$PUSHOVER_API_TOKEN`` /
``$PUSHOVER_USER_KEY`` are not in the environment unless the plist
explicitly bakes them in. The on-disk fallback mirrors the
``~/.config/resend/key`` pattern that the previous email path used.

Push is a nicety, not a barrier. Any failure here is logged and
swallowed so a Pushover outage doesn't fail the whole ``nightowl run``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from logging import Logger
from pathlib import Path

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_TOKEN_PATH = Path.home() / ".config" / "pushover" / "token"
PUSHOVER_USER_PATH = Path.home() / ".config" / "pushover" / "user"


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _read_credentials() -> tuple[str | None, str | None]:
    token = None
    user = None
    if PUSHOVER_TOKEN_PATH.exists():
        token = PUSHOVER_TOKEN_PATH.read_text().strip() or None
    if PUSHOVER_USER_PATH.exists():
        user = PUSHOVER_USER_PATH.read_text().strip() or None
    return token, user


def build_failure_message(
    failed_runs: list[dict],
    *,
    project_dir: str | None = None,
) -> tuple[str, str]:
    """Build ``(title, message)`` for a Pushover failure push.

    ``failed_runs`` is the subset of per-task records whose ``result``
    was ``failure``. The title is a short summary; the message body
    lists each failed task with the first line of its error.
    """
    n = len(failed_runs)
    plural = "task" if n == 1 else "tasks"
    if project_dir:
        project_name = Path(project_dir).name
        title = f"nightowl: {n} {plural} failed in {project_name}"
    else:
        title = f"nightowl: {n} {plural} failed"

    lines: list[str] = []
    for r in failed_runs:
        task_id = str(r.get("task_id", "?"))
        err = _first_line(r.get("error")) or "(no error message)"
        lines.append(f"{task_id}: {err}")
    message = "\n".join(lines)
    return title, message


def send_failure_notification(
    runs: list[dict],
    logger: Logger,
    *,
    project_dir: str | None = None,
) -> bool:
    """Send a Pushover push if any task in this run failed. Returns True on send.

    Never raises: missing credentials, Pushover being unreachable, or a
    non-2xx response are all logged and swallowed. Notifications are
    observability, not a barrier to the run completing.
    """
    failed = [r for r in runs if r.get("result") == "failure"]
    if not failed:
        logger.debug("No failed tasks this invocation; skipping Pushover push.")
        return False

    token, user = _read_credentials()
    if not token or not user:
        logger.warning(
            f"Pushover credentials not found at {PUSHOVER_TOKEN_PATH} / "
            f"{PUSHOVER_USER_PATH}; skipping push."
        )
        return False

    title, message = build_failure_message(failed, project_dir=project_dir)

    payload = urllib.parse.urlencode({
        "token": token,
        "user": user,
        "title": title,
        "message": message,
    }).encode("utf-8")
    req = urllib.request.Request(PUSHOVER_API_URL, data=payload, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 300:
                logger.warning(
                    f"Pushover returned HTTP {resp.status}: {body.strip()}"
                )
                return False
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}
            if parsed.get("status") != 1:
                logger.warning(f"Pushover rejected push: {body.strip()}")
                return False
            logger.info(f"Sent Pushover failure push: {title!r}")
            return True
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.warning(f"Pushover HTTP {e.code}: {err_body.strip() or e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.warning(f"Could not reach Pushover: {e.reason}")
        return False
    except Exception as e:
        logger.warning(f"Could not send Pushover push: {e}")
        return False
