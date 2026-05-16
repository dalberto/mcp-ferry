"""LaunchAgent lifecycle: render plist, install/uninstall, query status."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

LABEL: str = "dev.ascention.mcp-ferry"
PLIST_PATH: Path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR: Path = Path.home() / "Library" / "Logs" / "mcp-ferry"


@dataclass(frozen=True, slots=True)
class LaunchAgentStatus:
    loaded: bool
    pid: int | None
    last_exit_code: int | None
    label: str = LABEL


def render_plist(ferry_executable: Path, log_dir: Path, path_env: str) -> str:
    """Render the LaunchAgent plist as a UTF-8 XML string."""
    payload: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [str(ferry_executable), "run"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "NetworkState": True},
        "StandardOutPath": str(log_dir / "ferry.out.log"),
        "StandardErrorPath": str(log_dir / "ferry.err.log"),
        "EnvironmentVariables": {"PATH": path_env},
        "WorkingDirectory": str(Path.home()),
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def install() -> Path:
    """Render plist, write it, ensure log dir exists, and bootstrap into launchd."""
    ferry_path = shutil.which("ferry")
    if ferry_path is None:
        raise RuntimeError("`ferry` console script not found on PATH; install the package first")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    rendered = render_plist(Path(ferry_path), LOG_DIR, os.environ.get("PATH", ""))
    PLIST_PATH.write_text(rendered, encoding="utf-8")

    # `bootstrap` is the modern replacement for `load` (10.10+) and works with domain targets.
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(PLIST_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )
    return PLIST_PATH


def uninstall() -> None:
    """Unload the agent (best-effort) and delete the plist."""
    target = f"gui/{os.getuid()}/{LABEL}"
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False,
        capture_output=True,
        text=True,
    )
    PLIST_PATH.unlink(missing_ok=True)


_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", re.MULTILINE)


def status() -> LaunchAgentStatus:
    """Query launchd for the agent's loaded/pid/exit-code state."""
    target = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run(
        ["launchctl", "print", target],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return LaunchAgentStatus(loaded=False, pid=None, last_exit_code=None)

    output = result.stdout
    pid_match = _PID_RE.search(output)
    exit_match = _EXIT_RE.search(output)
    return LaunchAgentStatus(
        loaded=True,
        pid=int(pid_match.group(1)) if pid_match else None,
        last_exit_code=int(exit_match.group(1)) if exit_match else None,
    )
