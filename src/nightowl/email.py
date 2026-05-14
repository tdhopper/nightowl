"""Per-run summary email.

After every ``nightowl run`` that actually executes at least one task,
the runner sends a single HTML summary email via Resend. Closes the
observability gap where a task silently disappearing from the schedule
(or claude crashing before emitting JSON) goes unnoticed because the
only signal Tim sees is "PRs in GitHub."

Resend is invoked via the existing ``resend emails send`` CLI — the
same convention used by nightowl's own task prompts. The API key is
read from ``~/.config/resend/key`` (mode 600) because launchd does not
load direnv and so ``$RESEND_API_KEY`` is not in the environment when
the plist fires.

Email is a nicety, not a barrier. Any failure here is logged and
swallowed so a Resend outage doesn't fail the whole ``nightowl run``.
"""

from __future__ import annotations

import html
import subprocess
import tempfile
from datetime import datetime
from logging import Logger
from pathlib import Path

RESEND_KEY_PATH = Path.home() / ".config" / "resend" / "key"

_RESULT_EMOJI = {
    "success": "✅",   # ✅
    "failure": "❌",   # ❌
    "skipped": "⏭️",  # ⏭️
}


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def build_summary(
    runs: list[dict],
    *,
    now: datetime | None = None,
    project_dir: str | None = None,
) -> tuple[str, str]:
    """Build ``(subject, html_body)`` for a run summary email.

    ``runs`` is the list of per-task records produced this run, in
    execution order (the same dicts that get appended to ``runs.jsonl``).
    """
    now = now or datetime.now()
    ran = len(runs)
    failed = sum(1 for r in runs if r.get("result") == "failure")
    subject = (
        f"[nightowl] run {now.strftime('%Y-%m-%d %H:%M')} "
        f"-- {ran} ran, {failed} failed"
    )

    rows: list[str] = []
    total_cost = 0.0
    have_any_cost = False
    total_duration = 0
    for r in runs:
        emoji = _RESULT_EMOJI.get(r.get("result", ""), "")
        task_id = html.escape(str(r.get("task_id", "")))
        duration = _fmt_duration(r.get("duration_s"))
        pr_url = r.get("pr_url") or ""
        pr_cell = (
            f'<a href="{html.escape(pr_url)}">{html.escape(pr_url)}</a>'
            if pr_url else ""
        )
        err = _first_line(r.get("error")) if r.get("result") == "failure" else ""
        err_cell = html.escape(err)

        cost = r.get("claude_cost_usd")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            have_any_cost = True
        dur = r.get("duration_s")
        if isinstance(dur, (int, float)):
            total_duration += int(dur)

        rows.append(
            "<tr>"
            f"<td>{emoji}</td>"
            f"<td><code>{task_id}</code></td>"
            f"<td>{duration}</td>"
            f"<td>{pr_cell}</td>"
            f"<td>{err_cell}</td>"
            "</tr>"
        )

    table = (
        '<table border="1" cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;font-family:system-ui,sans-serif;">'
        "<thead><tr>"
        "<th></th><th>task</th><th>duration</th><th>PR</th><th>error</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )

    footer_parts = [f"total duration: {_fmt_duration(total_duration)}"]
    if have_any_cost:
        footer_parts.append(f"total claude cost: ${total_cost:.2f}")
    if project_dir:
        footer_parts.append(f"project: <code>{html.escape(project_dir)}</code>")
    footer = (
        '<p style="font-family:system-ui,sans-serif;color:#555;">'
        + " &middot; ".join(footer_parts)
        + "</p>"
    )

    body = (
        '<div style="font-family:system-ui,sans-serif;">'
        f"<h2>{html.escape(subject)}</h2>"
        f"{table}"
        f"{footer}"
        "</div>"
    )
    return subject, body


def _read_api_key() -> str | None:
    if not RESEND_KEY_PATH.exists():
        return None
    key = RESEND_KEY_PATH.read_text().strip()
    return key or None


def send_summary_email(
    runs: list[dict],
    logger: Logger,
    *,
    project_dir: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Send the per-run summary email. Returns True on success.

    Never raises: Resend being unreachable, the API key missing, or the
    CLI not being installed are all logged and swallowed. The summary is
    observability, not a barrier to the run completing.
    """
    if not runs:
        # "No tasks eligible" runs do not email — that's daily spam.
        logger.debug("No task runs this invocation; skipping summary email.")
        return False

    api_key = _read_api_key()
    if api_key is None:
        logger.warning(
            f"Resend API key not found at {RESEND_KEY_PATH}; "
            "skipping summary email."
        )
        return False

    subject, html_body = build_summary(runs, now=now, project_dir=project_dir)

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False,
        ) as tmp:
            tmp.write(html_body)
            tmp_path = tmp.name
    except OSError as e:
        logger.warning(f"Could not write summary email body: {e}")
        return False

    cmd = [
        "resend", "emails", "send",
        "--from", "Nightowl <claude@ehop.me>",
        "--to", "t@ehop.me",
        "--subject", subject,
        "--html-file", tmp_path,
    ]
    env_override = {"RESEND_API_KEY": api_key}
    try:
        import os
        env = {**os.environ, **env_override}
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            logger.warning(
                f"resend emails send exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )
            return False
        logger.info(f"Sent run summary email: {subject!r}")
        return True
    except FileNotFoundError:
        logger.warning("resend CLI not installed; skipping summary email.")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("resend emails send timed out; skipping summary email.")
        return False
    except Exception as e:
        logger.warning(f"Could not send summary email: {e}")
        return False
