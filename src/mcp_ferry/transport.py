"""Owns one stdio MCP subprocess and routes JSON-RPC requests/responses by id.

Hardened against the failure that wedged real deployments: if the subprocess's
stdout closes while the process lingers (partial crash), the reader task exits
but the process stays alive — so a naive `health = returncode is None` check
stays True and every later call blocks forever on a future nothing resolves.
Here, reader death force-kills the process so the supervisor restarts it, health
reflects the reader, and every call is bounded by a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import MCPConfig

logger = logging.getLogger(__name__)

# StreamReader's default buffer is 64 KiB; MCP tool results (large Bear notes,
# search dumps) routinely exceed that and would make readline() raise. Give it
# room so a big-but-legitimate response doesn't look like a crash.
MAX_LINE_BYTES = 16 * 1024 * 1024
STOP_GRACE_SECONDS = 5.0


class StdioMCP:
    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str | int, asyncio.Future[dict[str, Any]]] = {}
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._stopping = False

    @property
    def health(self) -> bool:
        """Alive only if the process AND its stdout reader are both up.

        The reader clause is load-bearing: a process whose reader has died can
        accept stdin writes but will never produce a response.
        """
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._stdout_task is not None
            and not self._stdout_task.done()
        )

    async def wait(self) -> int:
        if self._proc is None:
            raise RuntimeError(f"MCP {self.config.name} not started")
        return await self._proc.wait()

    async def start(self) -> None:
        if self.health:
            return  # already running; idempotent for supervisor restarts
        # Clear any residue from a previous incarnation.
        self._fail_pending(ConnectionError(f"MCP {self.config.name} restarting"))
        self._stopping = False

        env = {**os.environ, **self.config.env}
        cwd = str(self.config.cwd) if self.config.cwd is not None else None
        self._proc = await asyncio.create_subprocess_exec(
            *self.config.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            limit=MAX_LINE_BYTES,
        )
        name = self.config.name
        self._stdout_task = asyncio.create_task(self._read_stdout(), name=f"{name}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"{name}-stderr")
        logger.info("started MCP %s (pid=%s)", name, self._proc.pid)

    async def stop(self) -> None:
        if self._proc is None or self._stopping:
            return
        self._stopping = True
        proc = self._proc

        # Closing stdin sends EOF — well-behaved MCP servers exit cleanly on it.
        if proc.stdin is not None and not proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(ConnectionError(f"MCP {self.config.name} stopped"))
        logger.info("stopped MCP %s", self.config.name)

    async def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self.health or self._proc is None or self._proc.stdin is None:
            raise ConnectionError(f"MCP {self.config.name} not running")

        msg_id = message.get("id")
        line = (json.dumps(message) + "\n").encode("utf-8")

        if msg_id is None:
            await self._write(line)
            return None

        if msg_id in self._pending:
            # Reusing an in-flight id would orphan the first caller's future.
            raise ValueError(f"duplicate in-flight JSON-RPC id {msg_id!r}")

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._write(line)
            return await asyncio.wait_for(future, timeout=self.config.request_timeout)
        except TimeoutError as e:
            raise TimeoutError(
                f"MCP {self.config.name} did not respond to id {msg_id!r} "
                f"within {self.config.request_timeout}s"
            ) from e
        finally:
            self._pending.pop(msg_id, None)

    async def _write(self, line: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            async with self._write_lock:
                self._proc.stdin.write(line)
                await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as e:
            # Subprocess vanished between the health check and the write.
            self._force_down()
            raise ConnectionError(f"MCP {self.config.name} stdin write failed: {e}") from e

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # A single line exceeded MAX_LINE_BYTES. Unrecoverable for
                    # this stream position; treat as fatal so we restart clean.
                    logger.error(
                        "MCP %s: response line exceeded %d bytes; restarting",
                        self.config.name,
                        MAX_LINE_BYTES,
                    )
                    break
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("MCP %s: bad JSON on stdout: %r", self.config.name, text)
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    logger.debug("MCP %s: unsolicited message %r", self.config.name, msg)
                    continue
                fut = self._pending.get(msg_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                else:
                    logger.debug("MCP %s: response for unknown id %r", self.config.name, msg_id)
        except Exception:
            logger.exception("MCP %s: stdout reader crashed", self.config.name)
        finally:
            self._fail_pending(ConnectionError(f"MCP {self.config.name} stdout closed"))
            if not self._stopping:
                # Process may still be alive (partial crash). Force it down so
                # `wait()` returns and the supervisor restarts a clean instance.
                logger.warning(
                    "MCP %s: stdout reader exited while not stopping; killing process",
                    self.config.name,
                )
                self._force_down()

    async def _read_stderr(self) -> None:
        # stderr is not part of the JSON-RPC stream; forward to logging only.
        if self._proc is None or self._proc.stderr is None:
            return
        stream = self._proc.stderr
        try:
            while True:
                try:
                    raw = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue  # noisy oversized stderr line; never fatal
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info("MCP %s [stderr]: %s", self.config.name, text)
        except Exception:
            logger.exception("MCP %s: stderr reader crashed", self.config.name)

    def _force_down(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, AttributeError):
            proc.kill()

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
