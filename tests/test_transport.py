from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_ferry.config import MCPConfig
from mcp_ferry.transport import StdioMCP

ECHO = Path(__file__).parent / "fixtures" / "echo_mcp.py"
STRICT = ECHO.with_name("strict_mcp.py")


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


async def test_reused_client_id_across_concurrent_sends(mcp: StdioMCP) -> None:
    """Two concurrent calls reusing the same client id must both complete.

    The bridge multiplexes independent client sessions onto one subprocess, so
    a client-chosen id (unique only per-session) is not unique here. Internal
    id remapping lets both in-flight 'id=dup' calls resolve, each seeing its own
    id and result back. Regression: this used to raise 'duplicate in-flight id',
    which surfaced as recurring 500s in production.
    """
    slow = asyncio.create_task(
        mcp.send(
            {"jsonrpc": "2.0", "id": "dup", "method": "sleep",
             "params": {"seconds": 0.3, "marker": "slow"}}
        )
    )
    await asyncio.sleep(0.05)
    fast = await mcp.send(
        {"jsonrpc": "2.0", "id": "dup", "method": "ping", "params": {"marker": "fast"}}
    )
    assert fast is not None
    assert fast["id"] == "dup"
    assert fast["result"]["marker"] == "fast"  # type: ignore[index]

    slow_resp = await slow
    assert slow_resp is not None
    assert slow_resp["id"] == "dup"
    assert slow_resp["result"]["marker"] == "slow"  # type: ignore[index]


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


@pytest.fixture
async def strict_mcp():
    m = StdioMCP(_config().model_copy(update={"command": f"{sys.executable} {STRICT}"}))
    await m.start()
    try:
        yield m
    finally:
        await m.stop()


def _initialize(client_id: int, **params: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0", "id": client_id, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}, **params},
    }


async def test_concurrent_clients_share_one_handshake(strict_mcp: StdioMCP) -> None:
    responses = await asyncio.gather(*(strict_mcp.send(_initialize(i)) for i in range(10)))
    for i, response in enumerate(responses):
        assert response is not None
        assert response["id"] == i
        assert response["result"]["serverInfo"]["name"] == "strict"
        await strict_mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    status = await strict_mcp.send({"jsonrpc": "2.0", "id": 11, "method": "status"})
    assert status is not None
    assert status["result"]["acknowledgements"] == 1


async def test_initialize_error_is_not_cached(strict_mcp: StdioMCP) -> None:
    failed = await strict_mcp.send(_initialize(1, reject=True))
    assert failed is not None and "error" in failed
    good = await strict_mcp.send(_initialize(2))
    assert good is not None and "result" in good


async def test_restart_reinitializes_for_existing_clients(strict_mcp: StdioMCP) -> None:
    await strict_mcp.send(_initialize(1))
    before = await strict_mcp.send({"jsonrpc": "2.0", "id": 2, "method": "status"})
    await strict_mcp.stop()
    await strict_mcp.start()
    after = await strict_mcp.send({"jsonrpc": "2.0", "id": 3, "method": "status"})
    assert before is not None and after is not None
    assert after["result"]["pid"] != before["result"]["pid"]
    assert after["result"]["acknowledgements"] == 1
    new_client = await strict_mcp.send(_initialize(4))
    assert new_client is not None and "result" in new_client


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_initialize_replaces_connection(
    strict_mcp: StdioMCP, cancel: bool
) -> None:
    strict_mcp.config.request_timeout = 0.15
    task = asyncio.create_task(strict_mcp.send(_initialize(1, delay=10)))
    if cancel:
        await asyncio.sleep(0.05)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    await asyncio.wait_for(strict_mcp.wait(), timeout=2)
    assert not strict_mcp.health
    await strict_mcp.start()
    recovered = await strict_mcp.send(_initialize(2))
    assert recovered is not None and "result" in recovered
