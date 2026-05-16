from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mcp_ferry import launchd
from mcp_ferry.launchd import (
    LABEL,
    LaunchAgentStatus,
    install,
    render_plist,
    status,
    uninstall,
)


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect $HOME and re-point module-level paths so install/uninstall hit tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log_dir = tmp_path / "Library" / "Logs" / "mcp-ferry"
    monkeypatch.setattr(launchd, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd, "LOG_DIR", log_dir)
    yield tmp_path


def test_render_plist_is_valid_xml_and_matches_snapshot() -> None:
    rendered = render_plist(
        ferry_executable=Path("/opt/homebrew/bin/ferry"),
        log_dir=Path("/Users/test/Library/Logs/mcp-ferry"),
        path_env="/usr/bin:/bin",
    )
    expected = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>EnvironmentVariables</key>\n"
        "\t<dict>\n"
        "\t\t<key>PATH</key>\n"
        "\t\t<string>/usr/bin:/bin</string>\n"
        "\t</dict>\n"
        "\t<key>KeepAlive</key>\n"
        "\t<dict>\n"
        "\t\t<key>NetworkState</key>\n"
        "\t\t<true/>\n"
        "\t\t<key>SuccessfulExit</key>\n"
        "\t\t<false/>\n"
        "\t</dict>\n"
        "\t<key>Label</key>\n"
        f"\t<string>{LABEL}</string>\n"
        "\t<key>ProgramArguments</key>\n"
        "\t<array>\n"
        "\t\t<string>/opt/homebrew/bin/ferry</string>\n"
        "\t\t<string>run</string>\n"
        "\t</array>\n"
        "\t<key>RunAtLoad</key>\n"
        "\t<true/>\n"
        "\t<key>StandardErrorPath</key>\n"
        "\t<string>/Users/test/Library/Logs/mcp-ferry/ferry.err.log</string>\n"
        "\t<key>StandardOutPath</key>\n"
        "\t<string>/Users/test/Library/Logs/mcp-ferry/ferry.out.log</string>\n"
        "\t<key>WorkingDirectory</key>\n"
        f"\t<string>{Path.home()}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )
    assert rendered == expected

    parsed: dict[str, Any] = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["Label"] == LABEL
    assert parsed["ProgramArguments"] == ["/opt/homebrew/bin/ferry", "run"]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False, "NetworkState": True}
    assert parsed["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    assert parsed["StandardOutPath"].endswith("ferry.out.log")
    assert parsed["StandardErrorPath"].endswith("ferry.err.log")


def test_install_writes_plist_and_bootstraps(tmp_home: Path) -> None:
    fake_ferry = "/opt/homebrew/bin/ferry"
    with (
        patch("mcp_ferry.launchd.shutil.which", return_value=fake_ferry),
        patch("mcp_ferry.launchd.subprocess.run") as run_mock,
    ):
        run_mock.return_value = _completed(0)
        plist_path = install()

    assert plist_path == launchd.PLIST_PATH
    assert plist_path.exists()
    assert launchd.LOG_DIR.is_dir()

    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == LABEL
    assert parsed["ProgramArguments"] == [fake_ferry, "run"]

    run_mock.assert_called_once()
    args = run_mock.call_args
    cmd = args.args[0]
    assert cmd[0] == "launchctl"
    assert cmd[1] == "bootstrap"
    assert cmd[2] == f"gui/{os.getuid()}"
    assert cmd[3] == str(plist_path)
    assert args.kwargs.get("check") is True


def test_install_raises_when_ferry_missing(tmp_home: Path) -> None:
    with (
        patch("mcp_ferry.launchd.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="ferry"),
    ):
        install()


def test_uninstall_calls_bootout_and_removes_plist(tmp_home: Path) -> None:
    launchd.PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    launchd.PLIST_PATH.write_text("<plist/>", encoding="utf-8")

    with patch("mcp_ferry.launchd.subprocess.run") as run_mock:
        run_mock.return_value = _completed(0)
        uninstall()

    assert not launchd.PLIST_PATH.exists()
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd == ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]
    assert run_mock.call_args.kwargs.get("check") is False


def test_uninstall_tolerates_missing_plist(tmp_home: Path) -> None:
    with patch("mcp_ferry.launchd.subprocess.run") as run_mock:
        run_mock.return_value = _completed(1, stdout="not loaded")
        uninstall()
    assert not launchd.PLIST_PATH.exists()


def test_status_not_loaded() -> None:
    with patch("mcp_ferry.launchd.subprocess.run", return_value=_completed(1, "")) as run_mock:
        result = status()
    assert result == LaunchAgentStatus(loaded=False, pid=None, last_exit_code=None)
    cmd = run_mock.call_args.args[0]
    assert cmd == ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]


def test_status_running_with_pid() -> None:
    sample_output = f"""gui/501/{LABEL} = {{
\ttype = LaunchAgent
\tstate = running
\tpath = /Users/x/Library/LaunchAgents/{LABEL}.plist
\truns = 1
\tpid = 4242
\tlast exit code = (never exited)
}}
"""
    with patch("mcp_ferry.launchd.subprocess.run", return_value=_completed(0, sample_output)):
        result = status()
    assert result.loaded is True
    assert result.pid == 4242
    assert result.last_exit_code is None
    assert result.label == LABEL


def test_status_loaded_but_not_running_with_exit_code() -> None:
    sample_output = f"""gui/501/{LABEL} = {{
\ttype = LaunchAgent
\tstate = not running
\truns = 3
\tlast exit code = -9
}}
"""
    with patch("mcp_ferry.launchd.subprocess.run", return_value=_completed(0, sample_output)):
        result = status()
    assert result.loaded is True
    assert result.pid is None
    assert result.last_exit_code == -9


def test_status_subprocess_invocation_uses_capture_output() -> None:
    mock_run = MagicMock(return_value=_completed(1))
    with patch("mcp_ferry.launchd.subprocess.run", mock_run):
        status()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is False
