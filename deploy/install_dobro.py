"""Install on dobro after runner tests and private check provisioning pass."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

home = Path.home()
label = "com.nightowl.python-developer-tooling-handbook"
domain = "gui/" + str(os.getuid())
source = home / "Library/LaunchAgents" / f"{label}.plist"
backup = home / ".local/state/nightowl-healthchecks-backup-20260916"
backup.mkdir(mode=0o700, exist_ok=True)
state = subprocess.run(
    ["launchctl", "print", domain + "/" + label], capture_output=True, text=True
)
if any(line.strip().startswith("pid =") for line in state.stdout.splitlines()):
    raise SystemExit("Nightowl is running; wait for it to finish before installing.")
if (backup / source.name).exists():
    raise SystemExit("Backup exists; refusing to overwrite a previous deployment.")
shutil.copy2(source, backup / source.name)
python = str(home / ".local/share/uv/tools/nightowl/bin/python")
config = str(home / ".config/nightowl/healthchecks-handbook.json")
plist = plistlib.loads(source.read_bytes())
plist["ProgramArguments"] = [
    python,
    "-m",
    "nightowl.healthchecks",
    "run",
    "--config",
    config,
    "--",
    *plist["ProgramArguments"],
]
plist["EnvironmentVariables"]["NIGHTOWL_MONITORING_BACKEND"] = "healthchecks"
subprocess.run(["launchctl", "bootout", domain + "/" + label], check=True)
with source.open("wb") as out:
    plistlib.dump(plist, out)
source.chmod(0o600)
subprocess.run(["launchctl", "bootstrap", domain, str(source)], check=True)
subprocess.run(["launchctl", "kickstart", domain + "/" + label], check=True)

monitor_label = "com.nightowl.healthchecks-handbook"
monitor_path = home / "Library/LaunchAgents" / f"{monitor_label}.plist"
if monitor_path.exists():
    raise SystemExit("Monitor plist already exists; refusing duplicate install.")
logs = home / ".local/share/nightowl/logs"
logs.mkdir(parents=True, exist_ok=True)
monitor = {
    "Label": monitor_label,
    "ProgramArguments": [
        python,
        "-m",
        "nightowl.healthchecks",
        "report",
        "--config",
        config,
    ],
    "WorkingDirectory": str(home / "repos/python-developer-tooling-handbook"),
    "EnvironmentVariables": {
        "HOME": str(home),
        "PATH": str(home / ".local/bin")
        + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "StartInterval": 300,
    "StandardOutPath": str(logs / "healthchecks-handbook.out.log"),
    "StandardErrorPath": str(logs / "healthchecks-handbook.err.log"),
}
with monitor_path.open("wb") as out:
    plistlib.dump(monitor, out)
monitor_path.chmod(0o600)
print("Scheduler wrapped and started. Monitor plist prepared but NOT loaded.")
print(
    "Validate a dry report and notification channel routing before loading the monitor."
)
