from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_ferry.config import BridgeConfig, CloudflareConfig, FerryConfig, MCPConfig
from mcp_ferry.server import build_app
from mcp_ferry.transport import StdioMCP

ECHO = Path(__file__).parent / "fixtures" / "echo_mcp.py"


def _ferry_config() -> FerryConfig:
    return FerryConfig(
        bridge=BridgeConfig(hostname="bridge.test"),
        cloudflare=CloudflareConfig(tunnel_name="t"),
        mcps=[MCPConfig(name="echo", path="/echo", command=f"{sys.executable} {ECHO}")],
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    cfg = _ferry_config()
    transports = {m.name: StdioMCP(m) for m in cfg.mcps}
    app = build_app(cfg, transports, manage_lifecycle=True)
    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcps": {"echo": "ok"}}


def test_initialize_assigns_session_and_echoes(client: TestClient) -> None:
    r = client.post(
        "/echo",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"hi": True}},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id")
    body = r.json()
    assert body["id"] == 1
    assert body["result"] == {"hi": True}


def test_notification_returns_202(client: TestClient) -> None:
    r = client.post(
        "/echo",
        json={"jsonrpc": "2.0", "method": "notifications/something"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 202
    assert r.content == b""


def test_unknown_session_404(client: TestClient) -> None:
    r = client.post(
        "/echo",
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers={"Accept": "application/json", "Mcp-Session-Id": "nope"},
    )
    assert r.status_code == 404


def test_delete_session(client: TestClient) -> None:
    init = client.post(
        "/echo",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Accept": "application/json"},
    )
    sid = init.headers["mcp-session-id"]
    r = client.delete("/echo", headers={"Mcp-Session-Id": sid})
    assert r.status_code == 204
    r2 = client.post(
        "/echo",
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers={"Accept": "application/json", "Mcp-Session-Id": sid},
    )
    assert r2.status_code == 404
