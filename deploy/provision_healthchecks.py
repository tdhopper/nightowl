"""Run in Healthchecks' manage.py shell after copying the task spec to /tmp.

Creates only the tagged Nightowl checks, with notifications initially detached.
The output is a private monitor configuration, never printed to the console.
Attach the existing verified email integration after real probe verification.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils.timezone import now
from hc.accounts.models import Project
from hc.api.models import Check

spec = json.loads(Path("/tmp/nightowl-task-spec.json").read_text())
project = Project.objects.get(owner__email="t@ehop.me", name="hopperhosted monitoring")
output = Path("/tmp/nightowl-healthchecks.json")
if output.exists():
    raise RuntimeError(
        "Configuration already exists; preserve it before reprovisioning"
    )

config = {
    "project_path": "/Users/thopper/repos/python-developer-tooling-handbook",
    "receipt_path": "/Users/thopper/.local/state/nightowl/handbook-scheduler.json",
    "initialized_at": now().isoformat(),
    "scheduler_timeout_seconds": 5400,
    "max_run_seconds": 43200,
    "task_runtime_seconds": 10800,
    "dispatch_grace_seconds": 43200,
    "task_urls": {},
}
with transaction.atomic():
    for item in [{"id": "scheduler", "name": "Scheduler"}, *spec]:
        task_id = item["id"]
        slug = "nightowl-handbook-" + task_id
        if Check.objects.filter(project=project, slug=slug).exists():
            raise RuntimeError("Nightowl check already exists; refusing duplicate")
        check = Check.objects.create(
            project=project,
            name="[P2] Nightowl: " + item["name"],
            slug=slug,
            tags="nightowl handbook dobro",
            desc=(
                "Current health evaluated on dobro every five minutes. "
                "Pings represent observed health, not new task executions. "
                "Failures remain down until a later successful task result; "
                "intentional skips do not clear failures. "
                "No reports for 15 minutes also alerts. "
                "See Nightowl docs/healthchecks.md for deadlines and rollback."
            ),
            methods="POST",
            timeout=timedelta(minutes=10),
            grace=timedelta(minutes=5),
        )
        url = settings.PING_ENDPOINT + str(check.code)
        if task_id == "scheduler":
            config["scheduler_url"] = url
        else:
            config["task_urls"][task_id] = url
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle, indent=2)
print("Created", len(spec) + 1, "Nightowl checks without notification channels.")
print("Private monitor configuration written to /tmp/nightowl-healthchecks.json.")
