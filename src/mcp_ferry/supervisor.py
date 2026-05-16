"""Wires HTTP server + tunnel + N stdio MCPs and supervises their lifetimes.

Per-MCP crashes restart only that MCP with exponential backoff. Tunnel death
is fatal — the whole supervisor unwinds because the public hostname is gone.
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
    app = build_app(config, transports)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

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

    async def watch_stop() -> None:
        await stop.wait()
        server.should_exit = True

    main: list[asyncio.Task[Any]] = [
        asyncio.create_task(server.serve(), name="server"),
        asyncio.create_task(tunnel.wait(), name="tunnel"),
        asyncio.create_task(stop.wait(), name="signal"),
    ]
    aux: list[asyncio.Task[Any]] = [
        asyncio.create_task(watch_stop(), name="watch-stop"),
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
            elif name == "tunnel":
                logger.error("cloudflared exited rc=%s; shutting down", task.result())
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

    return 0
