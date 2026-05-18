"""Typer entry point: init / run / install / uninstall / status / logs."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import cloudflare_setup, launchd, supervisor
from .config import DEFAULT_CONFIG_PATH, FerryConfig

if TYPE_CHECKING:
    from cloudflare import Cloudflare

app = typer.Typer(no_args_is_help=True, add_completion=False, help="mcp-ferry CLI")
console = Console()

STARTER_CONFIG = """\
# mcp-ferry config: https://github.com/dalberto/mcp-ferry

[bridge]
hostname = "bridge.example.com"   # public hostname, fronted by Cloudflare Access
local_port = 8765

[cloudflare]
tunnel_name = "mcp-ferry"
# credentials_file = "/Users/you/.cloudflared/<tunnel-id>.json"  # auto-discovered if omitted
# Set these to skip SDK discovery — then the API token needs neither
# Account Settings:Read nor zone enumeration (find both in the Cloudflare
# dashboard URL / the zone's Overview page):
# account_id = "<cloudflare-account-id>"
# zone_id = "<zone-id-for-your-hostname>"
# How long a client (e.g. claude.ai) stays authenticated before re-auth.
# Default "168h" (1 week). Use "24h" for tighter security or "730h" (~1
# month) for fewer prompts. Takes effect on the next `ferry setup`.
# session_duration = "168h"

[[mcps]]
name = "bear"
path = "/bear"
command = "/Applications/Bear.app/Contents/MacOS/bearcli mcp-server"

# Add more MCPs by appending [[mcps]] blocks. No new tunnel or Access app needed.
# [[mcps]]
# name = "things"
# path = "/things"
# command = "uvx things-mcp"
"""

ConfigOpt = Annotated[Path, typer.Option("--config", help="Path to config.toml")]
ForceOpt = Annotated[bool, typer.Option("--force", help="Overwrite an existing config")]
FollowOpt = Annotated[bool, typer.Option("-f", "--follow", help="Tail and follow")]
StreamOpt = Annotated[
    str,
    typer.Option(
        "--stream",
        help="'main' (rotated ferry.log, default), or 'out'/'err' for the "
        "LaunchAgent boot files (pre-init tracebacks only)",
    ),
]
TokenOpt = Annotated[
    str | None,
    typer.Option(
        "--token",
        help="Cloudflare API token (or set CLOUDFLARE_API_TOKEN)",
        envvar="CLOUDFLARE_API_TOKEN",
    ),
]
EmailOpt = Annotated[
    list[str] | None,
    typer.Option("--email", help="Email allowed by the Access policy (repeatable)"),
]
RedirectUriOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--allowed-redirect-uri",
        help="Hosted-client OAuth callback URL to allow (repeatable). Overrides the "
        "built-in default list. CLI/editor clients use loopback and need no entry.",
    ),
]
GoogleClientIdOpt = Annotated[
    str | None,
    typer.Option(
        "--google-client-id",
        help="Google OAuth client ID (or set GOOGLE_CLIENT_ID). Pass it to skip the "
        "prompt — avoids paste/getpass truncation.",
        envvar="GOOGLE_CLIENT_ID",
    ),
]
GoogleClientSecretOpt = Annotated[
    str | None,
    typer.Option(
        "--google-client-secret",
        help="Google OAuth client secret (or set GOOGLE_CLIENT_SECRET). Pass it to "
        "skip the hidden prompt — avoids paste/getpass truncation.",
        envvar="GOOGLE_CLIENT_SECRET",
    ),
]
AccountIdOpt = Annotated[
    str | None,
    typer.Option(
        "--account-id",
        help="Cloudflare account id (or set CLOUDFLARE_ACCOUNT_ID, or cloudflare.account_id "
        "in config). Supplying it lets the token skip the Account Settings:Read permission.",
        envvar="CLOUDFLARE_ACCOUNT_ID",
    ),
]
ZoneIdOpt = Annotated[
    str | None,
    typer.Option(
        "--zone-id",
        help="Cloudflare zone id for the hostname (or set CLOUDFLARE_ZONE_ID, or "
        "cloudflare.zone_id in config). Supplying it skips the zones.list() lookup.",
        envvar="CLOUDFLARE_ZONE_ID",
    ),
]


def _normalize_emails(raw: list[str]) -> tuple[str, ...]:
    """Flatten repeated/comma-separated emails, trim, dedupe (order-preserving)."""
    seen: dict[str, None] = {}
    for chunk in raw:
        for part in chunk.split(","):
            addr = part.strip()
            if addr:
                seen.setdefault(addr, None)
    return tuple(seen)


def _load_config(config_path: Path) -> FerryConfig:
    """Load config or exit cleanly — a missing config is user error, not a crash."""
    try:
        return FerryConfig.load(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
LOG_BACKUP_COUNT = 5  # ~60 MiB ceiling total; cloudflared is chatty at INFO


def _setup_logging() -> None:
    """Rotate the primary log in-process; launchd can't bound its own files.

    launchd's StandardErrorPath grows forever (cloudflared logs every reconnect
    overnight). So the structured stream goes to a size-capped RotatingFileHandler
    and reaches stderr only on a TTY — under launchd there's no TTY, so the
    LaunchAgent .err.log stays near-empty (just pre-init tracebacks).
    """
    launchd.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    handlers: list[logging.Handler] = []
    file_handler = RotatingFileHandler(
        launchd.LOG_DIR / "ferry.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    handlers.append(file_handler)
    if sys.stderr is not None and sys.stderr.isatty():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        handlers.append(stream_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


@app.command()
def init(config_path: ConfigOpt = DEFAULT_CONFIG_PATH, force: ForceOpt = False) -> None:
    """Write a starter config.toml. Edit it before `ferry run`."""
    if config_path.exists() and not force:
        console.print(
            f"[yellow]config already exists at {config_path}[/]; pass --force to overwrite"
        )
        raise typer.Exit(1)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(STARTER_CONFIG)
    console.print(f"[green]wrote {config_path}[/]")
    console.print(
        "Next: `cloudflared tunnel login && cloudflared tunnel create <name>`, then `ferry run`."
    )


@app.command()
def setup(
    config_path: ConfigOpt = DEFAULT_CONFIG_PATH,
    token: TokenOpt = None,
    email: EmailOpt = None,
    account_id: AccountIdOpt = None,
    zone_id: ZoneIdOpt = None,
    google_client_id: GoogleClientIdOpt = None,
    google_client_secret: GoogleClientSecretOpt = None,
    redirect_uri: RedirectUriOpt = None,
) -> None:
    """Provision Cloudflare tunnel + DNS + Access app + policy for this config."""
    config = _load_config(config_path)
    api_token = token or typer.prompt("Cloudflare API token", hide_input=True)
    raw_emails = email or [typer.prompt("Allowed email(s), comma-separated")]
    allowed_emails = _normalize_emails(raw_emails)
    if not allowed_emails:
        console.print("[red]at least one allowed email is required[/]")
        raise typer.Exit(1)

    # Precedence: flag/env (typer-resolved) → config.toml → SDK discovery.
    # Supplying both ids means the token never calls accounts.list()/zones.list(),
    # so it doesn't need Account Settings:Read or zone enumeration.
    account_id = account_id or config.cloudflare.account_id
    zone_id = zone_id or config.cloudflare.zone_id

    from cloudflare import Cloudflare

    client = Cloudflare(api_token=api_token)
    if account_id is None:
        account_id = _pick_account(client)
    if zone_id is None:
        zone_id = _pick_zone(client, config.bridge.hostname)

    gcid = google_client_id or typer.prompt("Google OAuth client ID")
    gsecret = google_client_secret or typer.prompt(
        "Google OAuth client secret", hide_input=True
    )

    # Precedence: flag → config.toml → built-in hosted-client defaults.
    if redirect_uri:
        redirect_uris = tuple(redirect_uri)
    elif config.cloudflare.allowed_redirect_uris:
        redirect_uris = tuple(config.cloudflare.allowed_redirect_uris)
    else:
        redirect_uris = cloudflare_setup.DEFAULT_ALLOWED_REDIRECT_URIS

    inputs = cloudflare_setup.SetupInputs(
        api_token=api_token,
        account_id=account_id,
        zone_id=zone_id,
        hostname=config.bridge.hostname,
        tunnel_name=config.cloudflare.tunnel_name,
        google_client_id=gcid,
        google_client_secret=gsecret,
        allowed_emails=allowed_emails,
        allowed_redirect_uris=redirect_uris,
        session_duration=config.cloudflare.session_duration,
    )

    console.print("[cyan]provisioning Cloudflare resources…[/]")
    result = cloudflare_setup.run_setup(inputs)

    _write_credentials_path_into_config(config_path, result.tunnel_credentials_path)

    table = Table(title="Cloudflare setup complete", show_header=False)
    table.add_row("tunnel id", result.tunnel_id)
    table.add_row("credentials", str(result.tunnel_credentials_path))
    table.add_row("dns record", result.dns_record_id)
    table.add_row("idp", result.idp_id)
    table.add_row("application", result.application_id)
    table.add_row("policy", result.policy_id)
    console.print(table)
    console.print(
        f"[green]Done[/] — add [bold]https://{config.bridge.hostname}[/] as a "
        "remote MCP server in your client."
    )


def _pick_account(client: Cloudflare) -> str:
    """Return the only account id, or prompt to choose."""
    accounts = list(client.accounts.list())
    if not accounts:
        console.print("[red]no Cloudflare accounts visible to this token[/]")
        raise typer.Exit(1)
    if len(accounts) == 1:
        return accounts[0].id
    table = Table(title="accounts")
    table.add_column("#")
    table.add_column("id")
    table.add_column("name")
    for i, acct in enumerate(accounts):
        table.add_row(str(i), acct.id, acct.name or "")
    console.print(table)
    idx = int(typer.prompt("pick an account #"))
    return accounts[idx].id


def _pick_zone(client: Cloudflare, hostname: str) -> str:
    """Find the zone whose name is the longest suffix of `hostname`."""
    zones = list(client.zones.list())
    best: tuple[int, str] | None = None
    for zone in zones:
        name = zone.name
        if hostname == name or hostname.endswith(f".{name}"):
            score = len(name)
            if best is None or score > best[0]:
                best = (score, zone.id)
    if best is None:
        console.print(
            f"[red]no zone in this account matches hostname {hostname!r}[/]; "
            "is the apex domain on a different Cloudflare account?"
        )
        raise typer.Exit(1)
    return best[1]


def _write_credentials_path_into_config(config_path: Path, credentials_path: Path) -> None:
    """Patch `cloudflare.credentials_file` in the user's config.toml.

    Edit by line so we don't depend on a TOML round-tripping library.
    """
    if not config_path.exists():
        return
    lines = config_path.read_text().splitlines()
    new_line = f'credentials_file = "{credentials_path}"'
    in_cloudflare_section = False
    out: list[str] = []
    replaced = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_cloudflare_section and not replaced and not inserted:
                out.append(new_line)
                inserted = True
            in_cloudflare_section = stripped == "[cloudflare]"
        if in_cloudflare_section and (
            stripped.startswith("credentials_file")
            or stripped.startswith("# credentials_file")
        ):
            out.append(new_line)
            replaced = True
            continue
        out.append(line)
    if in_cloudflare_section and not replaced and not inserted:
        out.append(new_line)
    config_path.write_text("\n".join(out) + ("\n" if lines and lines[-1] == "" else "\n"))


@app.command()
def run(config_path: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Run the bridge in the foreground."""
    _setup_logging()
    config = _load_config(config_path)
    raise typer.Exit(asyncio.run(supervisor.run(config)))


@app.command()
def install() -> None:
    """Install and load the LaunchAgent so the bridge auto-starts at login."""
    path = launchd.install()
    console.print(f"[green]installed[/] {path}")
    console.print("logs: ~/Library/Logs/mcp-ferry/")


@app.command()
def uninstall() -> None:
    """Unload and remove the LaunchAgent."""
    launchd.uninstall()
    console.print("[green]uninstalled[/]")


@app.command()
def status(config_path: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Show LaunchAgent state and per-MCP health."""
    agent = launchd.status()
    table = Table(title="mcp-ferry status", show_header=False)
    table.add_row("loaded", "[green]yes[/]" if agent.loaded else "[red]no[/]")
    table.add_row("pid", str(agent.pid) if agent.pid else "—")
    last_exit = str(agent.last_exit_code) if agent.last_exit_code is not None else "—"
    table.add_row("last exit", last_exit)
    console.print(table)

    if not config_path.exists():
        return
    config = FerryConfig.load(config_path)
    url = f"http://127.0.0.1:{config.bridge.local_port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as e:
        console.print(f"[yellow]/healthz unreachable[/]: {e}")
        return

    mcps_table = Table(title=f"MCPs ({payload.get('status', 'unknown')})", show_header=True)
    mcps_table.add_column("name")
    mcps_table.add_column("state")
    for name, state in payload.get("mcps", {}).items():
        color = "green" if state == "ok" else "red"
        mcps_table.add_row(name, f"[{color}]{state}[/]")
    console.print(mcps_table)


@app.command()
def logs(follow: FollowOpt = False, stream: StreamOpt = "main") -> None:
    """Tail the rotated ferry.log (or the LaunchAgent boot files via --stream)."""
    name = "ferry.log" if stream == "main" else f"ferry.{stream}.log"
    log_file = launchd.LOG_DIR / name
    if not log_file.exists():
        console.print(f"[yellow]no log file at {log_file} yet[/]")
        raise typer.Exit(1)
    argv = ["tail", "-f", str(log_file)] if follow else ["tail", "-n", "200", str(log_file)]
    raise typer.Exit(subprocess.run(argv, check=False).returncode)


if __name__ == "__main__":
    app()
