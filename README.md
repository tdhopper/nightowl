# nightowl

A CLI tool that runs automated overnight tasks on a codebase using [Claude Code](https://claude.ai/code). A launchd plist fires the tool on schedule; it runs tasks and exits. No daemon, no database, no stale state.

## How it works

nightowl reads a `nightowl.yaml` config from your project root on every invocation. For each task whose interval has elapsed, it:

1. Checks out a fresh branch from `origin/main`
2. Runs Claude Code with the task's prompt
3. Commits and pushes changes, optionally opening a PR via `gh`
4. Records success or failure in a local state file

macOS scheduling is handled by a launchd plist that runs `nightowl run` at a random time within a configured window.

## Installation

```bash
pip install nightowl
```

Requires Python 3.12+, a working `claude` CLI, and `gh` (GitHub CLI) for PR creation.

## Getting started

Create `nightowl.yaml` in your project root:

```yaml
schedule:
  window_start: "22:00"
  window_end: "06:00"

tasks:
  - id: fix-typos
    name: "Fix Typos"
    interval: 24h
    output: pr
    prompt: |
      Find and fix typos in the documentation.
      Pick one file per run.
```

Run your tasks:

```bash
nightowl run
```

Install the schedule so tasks run overnight automatically:

```bash
nightowl install
```

## How-to guides

### Run a specific task

Skip the interval check and run one task by id:

```bash
nightowl run --task fix-typos
```

### Preview what would run

See which tasks are eligible without executing anything:

```bash
nightowl run --dry-run
```

### Check task status

View the last run time, result, and next eligible run for each task:

```bash
nightowl status
```

### Schedule overnight runs

Install a launchd plist that triggers `nightowl run` daily at a random time within the configured window:

```bash
nightowl install
```

The plist is written to `~/Library/LaunchAgents/com.nightowl.<project-slug>.plist`.

### Remove the schedule

```bash
nightowl uninstall
```

## Reference

### Config file (`nightowl.yaml`)

| Field | Required | Description |
|---|---|---|
| `schedule.window_start` | yes | Start of the run window (e.g. `"22:00"`) |
| `schedule.window_end` | yes | End of the run window (e.g. `"06:00"`) |
| `tasks` | yes | List of task definitions |

### Task fields

| Field | Required | Default | Description |
|---|---|---|---|
| `id` | yes | | Unique identifier, used for branch names and state tracking |
| `name` | yes | | Human-readable name for logs and PR titles |
| `interval` | yes | | Minimum time between runs (`12h`, `24h`, `7d`) |
| `output` | no | `pr` | What to do with changes: `pr`, `commit`, or `none` |
| `prompt` | yes | | The prompt sent to Claude Code |

### Output modes

- **`pr`**: Commit, push, and open a pull request via `gh pr create`
- **`commit`**: Commit and push to the branch (no PR)
- **`none`**: No git operations after Claude runs

### State file

Located at `~/.config/nightowl/state.json`. Tracks the last run time and result for each project/task combination. nightowl uses this to determine whether a task's interval has elapsed.

### Logs

Written to `~/.local/share/nightowl/logs/nightowl-<YYYY-MM-DD>.log`. Logs older than 30 days are cleaned up automatically.

### Git workflow

For each task, nightowl creates a branch named `nightowl/<task-id>-<YYYYMMDD>`. If the branch already exists from a failed previous run, it is deleted and recreated from `origin/main`.

### Claude invocation

```
claude --dangerously-skip-permissions \
  --project-dir <project-root> \
  --prompt "<task prompt>" \
  --output-format json \
  --max-turns 50
```

nightowl does not manage Claude config, permissions, or API keys. It relies on your existing `claude` CLI setup.

## Explanation

### Why no daemon?

Daemons accumulate stale state, fail silently, and add operational complexity. nightowl delegates scheduling to launchd, which macOS already runs and monitors. The tool starts, does its work, and exits. Every invocation reads config fresh from disk.

### Why one config file per project?

A single `nightowl.yaml` in the project root keeps task definitions next to the code they operate on. There is no global config to fall out of sync. If you need different tasks for different projects, each project gets its own file.

### What nightowl does not do

- No budget tracking or token counting
- No built-in task catalog or prioritization
- No multi-project orchestration
- No database
