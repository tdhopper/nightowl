# Monitor Nightowl with Healthchecks

The runner wrapper writes an atomic local execution receipt. A separate reporter
runs every five minutes and sends current health to Healthchecks. Configure each
remote check with a ten-minute timeout and five-minute grace. A stopped reporter
or unreachable machine therefore alerts without relying on Nightowl itself.

Store private JSON configuration with mode 600 containing `project_path`,
`receipt_path`, `scheduler_url`, `task_urls` (task ID to ping URL mapping), and
`initialized_at` (ISO timestamp). URLs must use the HTTPS `/ping/` endpoint on
`healthchecks.hopperhosted.com`. Do not commit ping credentials.

```bash
python -m nightowl.healthchecks run --config /private/config.json -- /path/to/handbook-launcher
python -m nightowl.healthchecks report --config /private/config.json
python -m nightowl.healthchecks report --config /private/config.json --dry-run
```

Wrap the launcher, including its `origin/main` fetch, so synchronization failures
are recorded. The scheduler fails on a missing receipt, unsuccessful exit,
completed receipt older than 90 minutes, or a running invocation older than
12 hours. SIGTERM, SIGINT, and SIGHUP are forwarded to the child process group.

Have the monitored launcher invoke `nightowl run --scheduler-health`. This makes
a completed dispatch exit successfully even when a task fails; per-task checks
report those failures. Configuration errors, interrupted dispatches, and failed
state or history writes still fail the scheduler. Without this flag, `nightowl run`
exits nonzero when any task fails. Dobro's `run-handbook-nightowl` launcher forwards
arguments after the project path to `nightowl run`.

Task failures remain unhealthy through skips and idle scheduler invocations until
that task succeeds. History and a private terminal-result cache preserve failures.
A running task gets three hours before a timeout. Overdue tasks get their configured
interval, rounded to the next hourly dispatch, plus 12 hours for dispatch.
Dispatches on excluded weekdays move to the first dispatch on the next allowed day. New tasks age from `initialized_at`.
Removed task definitions remain unhealthy until explicitly removed from configuration;
new task files without checks make the scheduler unhealthy.

Optional positive integer overrides: `scheduler_timeout_seconds`, `max_run_seconds`,
`task_runtime_seconds`, and `dispatch_grace_seconds`. Set `scheduler_minute` to the
hourly launch minute (0–59; default 17). The health cache defaults to
`receipt_path` plus `.health.json`; `health_cache_path` overrides it.

Set `NIGHTOWL_MONITORING_BACKEND=healthchecks` in the runner environment to disable
built-in Pushover and append an agent instruction overriding ad hoc operational
email/Pushover instructions. Agents retain operational findings in their output
or PR. Content deliverables remain unchanged. `NIGHTOWL_DISABLE_PUSHOVER=1` also
independently disables built-in Pushover.

Reports transmit only fixed status labels. They never transmit task errors,
credential URLs, or response bodies to logs. HTTP redirects are refused; only
HTTP 200 with exact body `OK` counts as delivery. Delivery failures exit nonzero.
A dry report prints current statuses without pinging or modifying the cache.

## Recognize blocked agent work

Codex tasks return a structured `completed`, `blocked`, or `failed` outcome.
A zero process exit code alone does not mean success. Missing or invalid outcomes
fail the task, while a verified finding that no change is needed may succeed.
Fact-check process failures and invalid verdicts also fail the task.

Codex runs in `workspace-write` with network access enabled for GitHub, package
registries, and source verification. Each task can write its `NIGHTOWL_STATE_DIR`.
Set `NIGHTOWL_WRITABLE_DIRS` to an OS-path-separator-delimited list of additional
absolute, existing directories when tasks need persistent caches. On dobro,
the handbook scheduler grants `/Users/thopper/.cache/uv` and
`/Users/thopper/.local/state/pydevtools-handbook`.

Verify deployments with dependency access from inside the deployed sandbox and
an affected task retry. A model response alone does not verify operational access.

## Hand validated files to the publisher

Codex leaves validated edits in the task worktree. Nightowl owns staging, commits,
pushes, and PR creation because sandbox Git metadata is read-only. Tasks prepare
`pr_title.txt` and `pr_body.md` in `NIGHTOWL_STATE_DIR` for required PR content.
A completed agent handoff still fails if Nightowl cannot publish it.

If an older task already published an independent commit through GitHub, a rejected
push is accepted only after fetching that exact branch and matching its complete
file tree to the validated local commit. Different content remains a failure;
Nightowl never force-pushes to resolve this conflict.
