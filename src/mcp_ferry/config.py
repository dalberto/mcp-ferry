"""Config schema. The contract every other module codes against.

Loaded from `~/.config/mcp-ferry/config.toml` by default. Each MCP gets its own
path under one hostname; one tunnel fronts them all. Adding a new MCP is one
`[[mcps]]` block — no new tunnel, no new Access app required.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mcp-ferry" / "config.toml"


class BridgeConfig(BaseModel):
    """HTTP server settings — what cloudflared points its `service:` at."""

    hostname: str = Field(description="Public hostname, e.g. 'bridge.example.com'")
    local_port: int = Field(default=8765, ge=1024, le=65535)


class CloudflareConfig(BaseModel):
    """Named tunnel identity. Credentials file is auto-discovered under ~/.cloudflared/."""

    tunnel_name: str
    credentials_file: Path | None = Field(
        default=None,
        description="Override auto-discovery of <tunnel-id>.json under ~/.cloudflared/",
    )
    account_id: str | None = Field(
        default=None,
        description="Cloudflare account id; set to skip accounts.list() (needs fewer token perms)",
    )
    zone_id: str | None = Field(
        default=None,
        description="Cloudflare zone id for the hostname; set to skip zones.list()",
    )
    allowed_redirect_uris: list[str] | None = Field(
        default=None,
        description="Override the default hosted-client OAuth redirect URI allowlist",
    )


class MCPConfig(BaseModel):
    """One stdio MCP server. Forwarded to from <hostname><path>."""

    name: str = Field(description="Short identifier, used in logs and the LaunchAgent label")
    path: str = Field(description="URL path prefix, e.g. '/bear'. Must start with '/'")
    command: str = Field(description="Shell-style command, e.g. 'bearcli mcp-server'")
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Path | None = None
    request_timeout: float = Field(
        default=300.0,
        gt=0,
        description="Seconds to await a JSON-RPC response before failing the call",
    )

    @field_validator("path")
    @classmethod
    def _path_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        return v.rstrip("/") or "/"

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("name must be alphanumeric (dashes/underscores ok)")
        return v

    def argv(self) -> list[str]:
        """Split command into argv for asyncio.create_subprocess_exec."""
        return shlex.split(self.command)


class FerryConfig(BaseModel):
    """Top-level config. Validated on load; failures abort startup."""

    bridge: BridgeConfig
    cloudflare: CloudflareConfig
    mcps: list[MCPConfig] = Field(min_length=1)

    @field_validator("mcps")
    @classmethod
    def _paths_unique(cls, v: list[MCPConfig]) -> list[MCPConfig]:
        paths = [m.path for m in v]
        if len(paths) != len(set(paths)):
            raise ValueError("each MCP must have a unique path")
        names = [m.name for m in v]
        if len(names) != len(set(names)):
            raise ValueError("each MCP must have a unique name")
        return v

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> FerryConfig:
        if not path.exists():
            raise FileNotFoundError(f"config not found at {path}; run `ferry init`")
        with path.open("rb") as f:
            return cls.model_validate(tomllib.load(f))
