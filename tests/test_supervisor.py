"""Smoke test for the supervisor: signal triggers clean shutdown across all tasks."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from mcp_ferry.config import BridgeConfig, CloudflareConfig, FerryConfig, MCPConfig
from mcp_ferry.supervisor import run


@pytest.fixture
def echo_config(tmp_path: Path) -> FerryConfig:
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    return FerryConfig(
        bridge=BridgeConfig(hostname="bridge.test", local_port=18765),
        cloudflare=CloudflareConfig(tunnel_name="test", credentials_file=creds),
        mcps=[
            MCPConfig(
                name="echo",
                path="/echo",
                command=f"{sys.executable} {Path(__file__).parent / 'fixtures' / 'echo_mcp.py'}",
            )
        ],
    )


async def test_signal_triggers_clean_shutdown(
    echo_config: FerryConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGTERM should unwind all tasks within the grace window."""

    # Mock the tunnel: pretend cloudflared is up and stays up until stop().
    from mcp_ferry import supervisor as supervisor_mod

    class FakeTunnel:
        def __init__(self, _config: FerryConfig) -> None:
            self._stop = asyncio.Event()

        async def start(self) -> None:
            return None

        async def wait(self) -> int:
            await self._stop.wait()
            return 0

        async def stop(self) -> None:
            self._stop.set()

    monkeypatch.setattr(supervisor_mod, "TunnelManager", FakeTunnel)

    async def fire_signal_soon() -> None:
        await asyncio.sleep(0.5)
        import os

        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(fire_signal_soon())
    rc = await asyncio.wait_for(run(echo_config), timeout=10.0)
    assert rc == 0
