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
import os
import signal
from typing import TYPE_CHECKING, Any

import uvicorn

from . import __version__
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
    # One identifying banner per incarnation: pid + version make each launchd
    # (re)start greppable, so "why is it a fresh process at 06:40?" is answerable
    # from the log alone.
    logger.info(
        "mcp-ferry %s starting (pid=%d): %d mcp(s) [%s] on 127.0.0.1:%d, tunnel %r",
        __version__,
        os.getpid(),
        len(config.mcps),
        ", ".join(m.name for m in config.mcps),
        config.bridge.local_port,
        config.cloudflare.tunnel_name,
    )

    transports: dict[str, StdioMCP] = {m.name: StdioMCP(m) for m in config.mcps}
    tunnel = TunnelManager(config)
    app = build_app(config, transports, tunnel=tunnel)

    stop = asyncio.Event()
    got_signal = False
    signal_name: str | None = None
    loop = asyncio.get_running_loop()

    def _on_signal(signum: int) -> None:
        nonlocal got_signal, signal_name
        got_signal = True
        signal_name = signal.Signals(signum).name
        # WARNING so it stands out against cloudflared's INFO chatter, and names
        # the exact signal — SIGTERM (launchd/OS/operator) vs SIGINT (Ctrl-C).
        # The sender pid is not available here: asyncio's signal handler doesn't
        # expose siginfo, so "who sent it" needs OS-level forensics (system log).
        logger.warning("received %s — initiating clean shutdown", signal_name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

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
    if got_signal:
        logger.info(
            "exiting 0 after %s (clean shutdown). launchd KeepAlive(SuccessfulExit"
            "=false) will NOT relaunch — the bridge stays down until the next "
            "login/reboot or `launchctl kickstart`. If you did not stop it, an OS- "
            "or operator-sent %s is why it went dark.",
            signal_name,
            signal_name,
        )
        return 0
    logger.error(
        "exiting 1 (HTTP server died without a signal). launchd KeepAlive will "
        "relaunch the bridge."
    )
    return 1
