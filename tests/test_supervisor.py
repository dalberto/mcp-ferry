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


async def test_tunnel_death_is_not_fatal_and_restarts(
    echo_config: FerryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinct port: the other test's uvicorn may still hold 18765 on teardown.
    echo_config = echo_config.model_copy(
        update={"bridge": echo_config.bridge.model_copy(update={"local_port": 18766})}
    )
    """cloudflared exiting must restart the tunnel in-process, not unwind.

    Regression: a tunnel exit used to tear the whole supervisor down and exit 0,
    which left the bridge dead overnight (launchd won't restart a clean exit).
    """
    from mcp_ferry import supervisor as supervisor_mod

    starts = 0

    class FlakyTunnel:
        def __init__(self, _config: FerryConfig) -> None:
            self._stop = asyncio.Event()
            self._exited_once = asyncio.Event()

        async def start(self) -> None:
            nonlocal starts
            starts += 1

        async def wait(self) -> int:
            if not self._exited_once.is_set():
                # First incarnation dies immediately (network blip).
                self._exited_once.set()
                return 1
            await self._stop.wait()  # restarted incarnation stays up
            return 0

        async def stop(self) -> None:
            self._stop.set()

    monkeypatch.setattr(supervisor_mod, "TunnelManager", FlakyTunnel)
    monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_INITIAL", 0.05)

    async def fire_signal_soon() -> None:
        await asyncio.sleep(1.0)
        import os

        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(fire_signal_soon())
    rc = await asyncio.wait_for(run(echo_config), timeout=10.0)
    # Signal-driven shutdown still exits 0; the tunnel was restarted, proving
    # its death did not unwind the supervisor before the signal arrived.
    assert rc == 0
    assert starts >= 2
