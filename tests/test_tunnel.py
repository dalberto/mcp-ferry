"""Tests for tunnel.py — no real cloudflared subprocess is invoked."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_ferry import tunnel as tunnel_mod
from mcp_ferry.config import BridgeConfig, CloudflareConfig, FerryConfig, MCPConfig
from mcp_ferry.tunnel import (
    TunnelManager,
    discover_credentials_file,
    render_config_yaml,
)


def _sample_config(credentials_file: Path | None = None) -> FerryConfig:
    return FerryConfig(
        bridge=BridgeConfig(hostname="bridge.example.com", local_port=8765),
        cloudflare=CloudflareConfig(
            tunnel_name="mcp-bridge",
            credentials_file=credentials_file,
        ),
        mcps=[MCPConfig(name="bear", path="/bear", command="bearcli mcp-server")],
    )


def test_render_config_yaml_snapshot() -> None:
    config = _sample_config()
    creds = Path("/Users/x/.cloudflared/abc-123.json")

    yaml_text = render_config_yaml(config, creds)

    expected = (
        "tunnel: mcp-bridge\n"
        "credentials-file: /Users/x/.cloudflared/abc-123.json\n"
        "ingress:\n"
        "  - hostname: bridge.example.com\n"
        "    service: http://localhost:8765\n"
        "  - service: http_status:404\n"
    )
    assert yaml_text == expected


def test_render_config_yaml_uses_configured_port() -> None:
    config = FerryConfig(
        bridge=BridgeConfig(hostname="h.example", local_port=9000),
        cloudflare=CloudflareConfig(tunnel_name="t"),
        mcps=[MCPConfig(name="m", path="/m", command="m")],
    )
    out = render_config_yaml(config, Path("/c.json"))
    assert "service: http://localhost:9000" in out
    assert "hostname: h.example" in out
    assert "tunnel: t" in out


def test_discover_credentials_file_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tunnel_id = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", tmp_path)
    creds = tmp_path / f"{tunnel_id}.json"
    creds.write_text("{}")

    canned = json.dumps(
        [
            {"id": "other", "name": "other-tunnel"},
            {"id": tunnel_id, "name": "mcp-bridge"},
        ]
    )

    def fake_run(*_a: object, **_kw: object) -> Any:
        result = MagicMock()
        result.returncode = 0
        result.stdout = canned
        result.stderr = ""
        return result

    monkeypatch.setattr(tunnel_mod.subprocess, "run", fake_run)

    found = discover_credentials_file("mcp-bridge")
    assert found == creds


def test_discover_credentials_file_missing_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_a: object, **_kw: object) -> Any:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "[]"
        result.stderr = ""
        return result

    monkeypatch.setattr(tunnel_mod.subprocess, "run", fake_run)

    with pytest.raises(FileNotFoundError, match="tunnel 'mcp-bridge' not found"):
        discover_credentials_file("mcp-bridge")


def test_discover_credentials_file_missing_creds_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tunnel_id = "abc"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", tmp_path)
    canned = json.dumps([{"id": tunnel_id, "name": "mcp-bridge"}])

    def fake_run(*_a: object, **_kw: object) -> Any:
        result = MagicMock()
        result.returncode = 0
        result.stdout = canned
        result.stderr = ""
        return result

    monkeypatch.setattr(tunnel_mod.subprocess, "run", fake_run)

    with pytest.raises(FileNotFoundError, match="credentials file"):
        discover_credentials_file("mcp-bridge")


def test_discover_credentials_file_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_a: object, **_kw: object) -> Any:
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "cloudflared: not logged in"
        return result

    monkeypatch.setattr(tunnel_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cloudflared tunnel list"):
        discover_credentials_file("mcp-bridge")


class _FakeStderr:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, stderr_lines: list[bytes]) -> None:
        self.stderr = _FakeStderr(stderr_lines)
        self.pid = 4242
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self._wait_event = asyncio.Event()

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        # Simulate graceful termination on SIGTERM.
        if sig == signal.SIGTERM:
            self.returncode = 0
            self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        assert self.returncode is not None
        return self.returncode

    def exit_with(self, code: int) -> None:
        self.returncode = code
        self._wait_event.set()


async def test_tunnel_manager_start_ready_and_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "fake.json"
    creds.write_text("{}")
    config_dir = tmp_path / ".cloudflared"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", config_dir)
    monkeypatch.setattr(tunnel_mod, "CONFIG_YAML", config_dir / "config.yml")
    monkeypatch.setattr(tunnel_mod, "cloudflared_binary", lambda: "/fake/cloudflared")

    fake_proc = _FakeProc(
        [
            b"INF Starting tunnel\n",
            b"INF Registered tunnel connection connIndex=0\n",
            b"",
        ]
    )

    captured: dict[str, object] = {}

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProc:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    config = _sample_config(credentials_file=creds)
    mgr = TunnelManager(config)
    await mgr.start()

    # Config rendered to disk.
    rendered = (config_dir / "config.yml").read_text()
    assert "tunnel: mcp-bridge" in rendered
    assert f"credentials-file: {creds}" in rendered

    # Argv looks right.
    assert captured["args"] == (
        "/fake/cloudflared",
        "tunnel",
        "run",
        "mcp-bridge",
    )

    # The stderr pump should pick up the registration line and set ready.
    await asyncio.wait_for(mgr.ready(), timeout=1.0)
    assert mgr.health is True
    assert mgr.pid() == 4242

    await mgr.stop()
    assert signal.SIGTERM in fake_proc.signals
    assert fake_proc.killed is False
    assert mgr.health is False


async def test_tunnel_manager_stop_falls_back_to_sigkill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "fake.json"
    creds.write_text("{}")
    config_dir = tmp_path / ".cloudflared"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", config_dir)
    monkeypatch.setattr(tunnel_mod, "CONFIG_YAML", config_dir / "config.yml")
    monkeypatch.setattr(tunnel_mod, "cloudflared_binary", lambda: "/fake/cloudflared")
    monkeypatch.setattr(tunnel_mod, "STOP_GRACE_SECONDS", 0.05)

    class StubbornProc(_FakeProc):
        def send_signal(self, sig: int) -> None:  # noqa: ARG002
            # Refuse to die on SIGTERM.
            self.signals.append(sig)

    stubborn = StubbornProc([b""])

    async def fake_exec(*_a: str, **_kw: object) -> StubbornProc:
        return stubborn

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mgr = TunnelManager(_sample_config(credentials_file=creds))
    await mgr.start()
    await mgr.stop()

    assert signal.SIGTERM in stubborn.signals
    assert stubborn.killed is True


async def test_tunnel_manager_wait_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "fake.json"
    creds.write_text("{}")
    config_dir = tmp_path / ".cloudflared"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", config_dir)
    monkeypatch.setattr(tunnel_mod, "CONFIG_YAML", config_dir / "config.yml")
    monkeypatch.setattr(tunnel_mod, "cloudflared_binary", lambda: "/fake/cloudflared")

    fake_proc = _FakeProc([b""])

    async def fake_exec(*_a: str, **_kw: object) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mgr = TunnelManager(_sample_config(credentials_file=creds))
    await mgr.start()

    # Simulate the subprocess exiting on its own.
    fake_proc.exit_with(7)

    rc = await mgr.wait()
    assert rc == 7


async def test_tunnel_manager_wait_without_start_raises() -> None:
    mgr = TunnelManager(_sample_config(credentials_file=Path("/tmp/x")))
    with pytest.raises(RuntimeError, match="not started"):
        await mgr.wait()


async def test_health_tracks_per_connection_up_and_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A connection dropping without the process exiting must show in health.

    Regression: `ready_event` latched True on first connect and never cleared,
    so /healthz reported "ok" while every edge connection was actually down.
    """
    creds = tmp_path / "fake.json"
    creds.write_text("{}")
    config_dir = tmp_path / ".cloudflared"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", config_dir)
    monkeypatch.setattr(tunnel_mod, "CONFIG_YAML", config_dir / "config.yml")
    monkeypatch.setattr(tunnel_mod, "cloudflared_binary", lambda: "/fake/cloudflared")

    fake_proc = _FakeProc(
        [
            b"INF Registered tunnel connection connIndex=0\n",
            b"INF Registered tunnel connection connIndex=1\n",
            b'ERR Connection terminated error="boom" connIndex=0\n',
            b"",
        ]
    )

    async def fake_exec(*_a: str, **_kw: object) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mgr = TunnelManager(_sample_config(credentials_file=creds))
    await mgr.start()
    assert mgr._stderr_task is not None
    await mgr._stderr_task  # drain the scripted stderr

    # conn 0 terminated, conn 1 still live → still reachable, still healthy.
    assert mgr._live_conns == {1}
    assert mgr.health is True


async def test_health_false_when_all_connections_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "fake.json"
    creds.write_text("{}")
    config_dir = tmp_path / ".cloudflared"
    monkeypatch.setattr(tunnel_mod, "CLOUDFLARED_DIR", config_dir)
    monkeypatch.setattr(tunnel_mod, "CONFIG_YAML", config_dir / "config.yml")
    monkeypatch.setattr(tunnel_mod, "cloudflared_binary", lambda: "/fake/cloudflared")

    fake_proc = _FakeProc(
        [
            b"INF Registered tunnel connection connIndex=0\n",
            b"ERR Connection terminated connIndex=0\n",
            b"",
        ]
    )

    async def fake_exec(*_a: str, **_kw: object) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    mgr = TunnelManager(_sample_config(credentials_file=creds))
    await mgr.start()
    assert mgr._stderr_task is not None
    await mgr._stderr_task

    # Process still alive, but zero live connections → unhealthy (the zombie).
    assert mgr._live_conns == set()
    assert mgr.health is False
    # ready_event stayed set (connected once) — proving it can't be the signal.
    assert mgr.ready_event.is_set()
