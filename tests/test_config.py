from __future__ import annotations

from pathlib import Path

import pytest

from mcp_ferry.config import FerryConfig


def test_tilde_paths_expand_against_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[bridge]
hostname = "bridge.example.com"

[cloudflare]
tunnel_name = "mcp-ferry"
credentials_file = "~/.cloudflared/TID.json"

[[mcps]]
name = "things"
path = "/things"
command = "~/.local/bin/things-mcp --db ~/things.db"
cwd = "~/work"
"""
    )

    config = FerryConfig.load(config_path)

    assert config.cloudflare.credentials_file == tmp_path / ".cloudflared" / "TID.json"
    mcp = config.mcps[0]
    assert mcp.cwd == tmp_path / "work"
    assert mcp.argv() == [
        str(tmp_path / ".local" / "bin" / "things-mcp"),
        "--db",
        str(tmp_path / "things.db"),
    ]


def test_absolute_paths_and_plain_words_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[bridge]
hostname = "bridge.example.com"

[cloudflare]
tunnel_name = "mcp-ferry"
credentials_file = "/etc/cloudflared/TID.json"

[[mcps]]
name = "bear"
path = "/bear"
command = "bearcli mcp-server"
"""
    )

    config = FerryConfig.load(config_path)

    assert config.cloudflare.credentials_file == Path("/etc/cloudflared/TID.json")
    assert config.mcps[0].cwd is None
    assert config.mcps[0].argv() == ["bearcli", "mcp-server"]
