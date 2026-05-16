from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_ferry.config import MCPConfig
from mcp_ferry.transport import StdioMCP

ECHO = Path(__file__).parent / "fixtures" / "echo_mcp.py"


def _config(name: str = "echo", request_timeout: float = 300.0) -> MCPConfig:
    return MCPConfig(
        name=name,
        path="/echo",
        command=f"{sys.executable} {ECHO}",
        request_timeout=request_timeout,
    )


@pytest.fixture
async def mcp():
    m = StdioMCP(_config())
    await m.start()
    try:
        yield m
    finally:
        await m.stop()


async def test_request_response(mcp: StdioMCP) -> None:
    resp = await mcp.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": 1}})
    assert resp == {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}


async def test_notification_returns_none(mcp: StdioMCP) -> None:
    result = await mcp.send({"jsonrpc": "2.0", "method": "notify"})
    assert result is None
    # Still responsive afterward.
    resp = await mcp.send({"jsonrpc": "2.0", "id": "after-notif", "method": "ping"})
    assert resp is not None
    assert resp["id"] == "after-notif"


async def test_concurrent_requests_correlate_by_id(mcp: StdioMCP) -> None:
    # Fire many in parallel; result params should match request params exactly.
    async def one(i: int) -> dict[str, object]:
        delay = 0.05 if i % 2 == 0 else 0.02
        resp = await mcp.send(
            {
                "jsonrpc": "2.0",
                "id": f"req-{i}",
                "method": "sleep",
                "params": {"seconds": delay, "marker": i},
            }
        )
        assert resp is not None
        return resp

    responses = await asyncio.gather(*(one(i) for i in range(20)))
    for i, resp in enumerate(responses):
        assert resp["id"] == f"req-{i}"
        assert resp["result"]["marker"] == i  # type: ignore[index]


async def test_clean_shutdown() -> None:
    m = StdioMCP(_config())
    await m.start()
    assert m.health
    await m.stop()
    assert not m.health


async def test_crash_propagates_connection_error() -> None:
    m = StdioMCP(_config())
    await m.start()
    try:
        # Issue a slow request, then kill the subprocess out from under it.
        slow = asyncio.create_task(
            m.send({"jsonrpc": "2.0", "id": "slow", "method": "sleep", "params": {"seconds": 5}})
        )
        await asyncio.sleep(0.05)
        assert m._proc is not None  # noqa: SLF001
        m._proc.kill()  # noqa: SLF001
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(slow, timeout=2.0)
        await asyncio.sleep(0.1)
        assert not m.health
    finally:
        await m.stop()


async def test_send_when_not_started_raises() -> None:
    m = StdioMCP(_config())
    with pytest.raises(ConnectionError):
        await m.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


async def test_reader_death_with_live_process_is_not_a_zombie() -> None:
    """The regression that wedged prod: stdout closes, process lives on.

    Health must flip false, the process must be force-killed so `wait()`
    returns (supervisor restart hook), and further sends must fail fast — not
    hang forever on a future nothing will resolve.
    """
    m = StdioMCP(_config(request_timeout=2.0))
    await m.start()
    # Notification: echo closes stdout and sleeps 30s (process stays alive).
    await m.send({"jsonrpc": "2.0", "method": "close_stdout"})

    rc = await asyncio.wait_for(m.wait(), timeout=5.0)
    assert rc != 0 or rc is not None  # it exited because we killed it
    assert not m.health
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(
            m.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}), timeout=1.0
        )
    await m.stop()


async def test_send_times_out_fast_and_cleans_pending() -> None:
    """A wedged subprocess must surface as a bounded TimeoutError, not an
    infinite hang, and the timed-out id must not leak in _pending."""
    timeout = 0.3
    m = StdioMCP(_config(request_timeout=timeout))
    await m.start()
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(TimeoutError):
            await m.send(
                {"jsonrpc": "2.0", "id": "slow", "method": "sleep", "params": {"seconds": 5}}
            )
        elapsed = loop.time() - t0
        assert elapsed < timeout + 1.0  # bounded, not hung
        assert m._pending == {}  # noqa: SLF001 — no leaked future
        assert m.health  # process + reader still alive; only the call failed
    finally:
        await m.stop()


async def test_large_response_over_64k_round_trips(mcp: StdioMCP) -> None:
    size = 200_000  # well past StreamReader's default 64 KiB limit
    resp = await mcp.send(
        {"jsonrpc": "2.0", "id": "big", "method": "big", "params": {"size": size}}
    )
    assert resp is not None
    assert len(resp["result"]["blob"]) == size  # type: ignore[index]


async def test_duplicate_inflight_id_rejected(mcp: StdioMCP) -> None:
    inflight = asyncio.create_task(
        mcp.send({"jsonrpc": "2.0", "id": "dup", "method": "sleep", "params": {"seconds": 1}})
    )
    await asyncio.sleep(0.05)
    with pytest.raises(ValueError, match="duplicate in-flight"):
        await mcp.send({"jsonrpc": "2.0", "id": "dup", "method": "ping"})
    assert (await inflight) is not None


async def test_send_after_kill_fails_fast_not_hang() -> None:
    m = StdioMCP(_config(request_timeout=30.0))
    await m.start()
    try:
        assert m._proc is not None  # noqa: SLF001
        m._proc.kill()  # noqa: SLF001
        await asyncio.sleep(0.1)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(
                m.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}), timeout=1.0
            )
    finally:
        await m.stop()
