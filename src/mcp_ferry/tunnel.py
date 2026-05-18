"""Cloudflare tunnel subprocess lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pycloudflared.util import download, get_info  # pyright: ignore[reportMissingTypeStubs]

if TYPE_CHECKING:
    from mcp_ferry.config import FerryConfig

logger = logging.getLogger(__name__)

CLOUDFLARED_DIR = Path.home() / ".cloudflared"
CONFIG_YAML = CLOUDFLARED_DIR / "config.yml"
READY_PATTERN = re.compile(r"Registered tunnel connection", re.IGNORECASE)
# cloudflared runs 4 edge connections (connIndex 0-3). These mark one going
# down without the process exiting — the "zombie tunnel" case that made
# /healthz lie. We track live connIndexes so health reflects real reachability.
CONN_DOWN_PATTERN = re.compile(
    r"Unregistered tunnel connection|Connection terminated|failed to serve tunnel connection",
    re.IGNORECASE,
)
CONN_INDEX_PATTERN = re.compile(r"connIndex=(\d+)")
STOP_GRACE_SECONDS = 5.0


def cloudflared_binary() -> str:
    """Path to the cloudflared executable; download if pycloudflared hasn't yet."""
    info = get_info()
    if not Path(info.executable).exists():
        return download(info)
    return info.executable


def render_config_yaml(config: FerryConfig, credentials_file: Path) -> str:
    return (
        f"tunnel: {config.cloudflare.tunnel_name}\n"
        f"credentials-file: {credentials_file}\n"
        f"ingress:\n"
        f"  - hostname: {config.bridge.hostname}\n"
        f"    service: http://localhost:{config.bridge.local_port}\n"
        f"  - service: http_status:404\n"
    )


def _list_tunnels(binary: str = "cloudflared") -> list[dict[str, object]]:
    result = subprocess.run(  # noqa: S603
        [binary, "tunnel", "list", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`cloudflared tunnel list` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse `cloudflared tunnel list` output: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError("`cloudflared tunnel list` did not return a JSON array")
    return data  # pyright: ignore[reportUnknownVariableType]


def discover_credentials_file(tunnel_name: str, binary: str = "cloudflared") -> Path:
    tunnels = _list_tunnels(binary)
    tunnel_id: str | None = None
    for entry in tunnels:
        if entry.get("name") == tunnel_name:
            raw_id = entry.get("id")
            if isinstance(raw_id, str):
                tunnel_id = raw_id
                break
    if tunnel_id is None:
        raise FileNotFoundError(
            f"tunnel {tunnel_name!r} not found via `cloudflared tunnel list`. "
            f"Run: cloudflared tunnel login && cloudflared tunnel create {tunnel_name}"
        )
    candidate = CLOUDFLARED_DIR / f"{tunnel_id}.json"
    if not candidate.exists():
        raise FileNotFoundError(
            f"credentials file {candidate} missing for tunnel {tunnel_name!r}. "
            f"Run: cloudflared tunnel login && cloudflared tunnel create {tunnel_name}"
        )
    return candidate


class TunnelManager:
    """Owns the cloudflared subprocess; exposes ready/health/wait/stop."""

    def __init__(self, config: FerryConfig) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._live_conns: set[int] = set()
        self.ready_event: asyncio.Event = asyncio.Event()

    @property
    def health(self) -> bool:
        """Healthy only with the process up AND >=1 live edge connection.

        `ready_event` (set on first registration, never cleared) only proves
        the tunnel connected once — it stayed True through every overnight
        drop. Live-connection tracking is what makes /healthz tell the truth.
        """
        return (
            self._proc is not None
            and self._proc.returncode is None
            and len(self._live_conns) > 0
        )

    async def start(self) -> None:
        creds = self._config.cloudflare.credentials_file or discover_credentials_file(
            self._config.cloudflare.tunnel_name
        )
        # Fresh incarnation: drop connIndexes from any prior (crashed) process.
        self._live_conns.clear()
        CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_YAML.write_text(render_config_yaml(self._config, creds))

        binary = cloudflared_binary()
        logger.info(
            "starting cloudflared (%s) for tunnel %s",
            binary,
            self._config.cloudflare.tunnel_name,
        )
        self._proc = await asyncio.create_subprocess_exec(
            binary,
            "tunnel",
            "run",
            self._config.cloudflare.tunnel_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._pump_stderr())

    async def _pump_stderr(self) -> None:
        assert self._proc is not None
        stderr = self._proc.stderr
        if stderr is None:
            return
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            logger.info("[cloudflared] %s", text)
            idx_match = CONN_INDEX_PATTERN.search(text)
            conn = int(idx_match.group(1)) if idx_match else None
            if READY_PATTERN.search(text):
                # -1 is a sentinel for the rare format without connIndex: never
                # let a parse miss read as "down" — /healthz is observational,
                # so a false-healthy is safer than a false-dead flap.
                self._live_conns.add(conn if conn is not None else -1)
                self.ready_event.set()
            elif conn is not None and CONN_DOWN_PATTERN.search(text):
                self._live_conns.discard(conn)

    async def ready(self) -> None:
        await self.ready_event.wait()

    async def wait(self) -> int:
        if self._proc is None:
            raise RuntimeError("tunnel not started")
        rc = await self._proc.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        return rc

    async def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_SECONDS)
        except TimeoutError:
            logger.warning("cloudflared did not exit after SIGTERM; sending SIGKILL")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if self._stderr_task is not None:
            with contextlib.suppress(Exception):
                await self._stderr_task

    def pid(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.pid


__all__ = [
    "CONFIG_YAML",
    "TunnelManager",
    "cloudflared_binary",
    "discover_credentials_file",
    "render_config_yaml",
]
