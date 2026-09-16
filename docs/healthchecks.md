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
