"""Wires HTTP server + tunnel + N stdio MCPs and supervises their lifetimes.

Each crash restarts only that component with exponential backoff. The named
tunnel has a stable hostname, so a cloudflared bounce (overnight network drop,
laptop sleep/wake) is recovered in-process — it is NOT fatal. Only the HTTP
server dying, or a SIGINT/SIGTERM, unwinds the supervisor; a non-signal unwind
exits non-zero so the LaunchAgent (KeepAlive SuccessfulExit=false) restarts the
process. Exiting 0 on a non-signal death is what wedged it dead overnight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Any

import uvicorn

from .server import build_app
from .transport import StdioMCP
from .tunnel import TunnelManager

if TYPE_CHECKING:
    from .config import FerryConfig

logger = logging.getLogger(__name__)

RESTART_BACKOFF_INITIAL = 1.0
RESTART_BACKOFF_MAX = 30.0
# An MCP that stayed up at least this long is treated as a one-off failure on
# its next death — reset backoff so a long-lived process that finally hiccups
# restarts promptly, while a crash-looper still escalates.
HEALTHY_RESET_SECONDS = 30.0


async def run(config: FerryConfig) -> int:
    transports: dict[str, StdioMCP] = {m.name: StdioMCP(m) for m in config.mcps}
    tunnel = TunnelManager(config)
    app = build_app(config, transports, tunnel=tunnel)

    stop = asyncio.Event()
    got_signal = False
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        nonlocal got_signal
        got_signal = True
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    for t in transports.values():
        await t.start()
    await tunnel.start()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config.bridge.local_port,
            log_level="info",
            access_log=True,
        )
    )

    async def supervise(mcp: StdioMCP) -> None:
        backoff = RESTART_BACKOFF_INITIAL
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            rc = await mcp.wait()
            if stop.is_set():
                return
            uptime = loop.time() - started
            if uptime >= HEALTHY_RESET_SECONDS:
                backoff = RESTART_BACKOFF_INITIAL
            logger.warning(
                "MCP %s exited rc=%d after %.1fs; restarting in %.1fs",
                mcp.config.name,
                rc,
                uptime,
                backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            try:
                await mcp.start()
            except Exception:
                logger.exception("MCP %s restart failed", mcp.config.name)
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX)

    async def supervise_tunnel() -> None:
        # Same restart discipline as MCPs. The hostname is stable across a
        # cloudflared restart (named tunnel), so reconnecting needs no client
        # reconfiguration — the bridge stays reachable at the same URL.
        backoff = RESTART_BACKOFF_INITIAL
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            rc = await tunnel.wait()
            if stop.is_set():
                return
            uptime = loop.time() - started
            if uptime >= HEALTHY_RESET_SECONDS:
                backoff = RESTART_BACKOFF_INITIAL
            logger.warning(
                "cloudflared exited rc=%s after %.1fs; restarting in %.1fs",
                rc,
                uptime,
                backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            try:
                await tunnel.start()
            except Exception:
                logger.exception("cloudflared restart failed")
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX)

    async def watch_stop() -> None:
        await stop.wait()
        server.should_exit = True

    main: list[asyncio.Task[Any]] = [
        asyncio.create_task(server.serve(), name="server"),
        asyncio.create_task(stop.wait(), name="signal"),
    ]
    aux: list[asyncio.Task[Any]] = [
        asyncio.create_task(watch_stop(), name="watch-stop"),
        asyncio.create_task(supervise_tunnel(), name="tunnel"),
    ]
    for t in transports.values():
        aux.append(asyncio.create_task(supervise(t), name=f"mcp-{t.config.name}"))

    try:
        done, _ = await asyncio.wait(main, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = task.get_name()
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error("%s task raised: %r", name, exc)
            elif name == "signal":
                logger.info("shutdown signal received")
            elif name == "server":
                logger.info("HTTP server stopped")
        stop.set()
        server.should_exit = True
    finally:
        for task in main + aux:
            if not task.done():
                task.cancel()
        await asyncio.gather(*main, *aux, return_exceptions=True)
        with contextlib.suppress(Exception):
            await tunnel.stop()
        for t in transports.values():
            with contextlib.suppress(Exception):
                await t.stop()

    # 0 only on an explicit SIGINT/SIGTERM. Any other unwind (the HTTP server
    # died) is non-zero so launchd's KeepAlive(SuccessfulExit=false) restarts
    # the process instead of leaving it dead.
    return 0 if got_signal else 1
