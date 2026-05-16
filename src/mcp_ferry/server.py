"""Starlette app: one POST/GET/DELETE route per MCP path implementing Streamable HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from .config import FerryConfig
    from .transport import StdioMCP

logger = logging.getLogger(__name__)

# A JSON-RPC message is a request if it has both "method" and "id".
# Notifications have "method" but no "id"; responses have "id" but no "method".


def _is_request(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" in msg


def _new_session_id() -> str:
    return secrets.token_urlsafe(24)


def _make_post_handler(mcp: StdioMCP, sessions: set[str]) -> Any:
    async def handler(request: Request) -> Response:
        if not mcp.health:
            return JSONResponse({"error": "mcp not running"}, status_code=503)

        try:
            body: Any = await request.json()
        except json.JSONDecodeError:
            return _rpc_error(-32700, "Parse error")

        session_id = request.headers.get("mcp-session-id")

        messages: list[dict[str, Any]]
        if isinstance(body, list):
            raw_items = cast("list[Any]", body)
            messages = [m for m in raw_items if isinstance(m, dict)]
            if len(messages) != len(raw_items):
                return _rpc_error(-32600, "Invalid Request")
        elif isinstance(body, dict):
            messages = [cast("dict[str, Any]", body)]
        else:
            return _rpc_error(-32600, "Invalid Request")

        # An initialize request mints a new session id.
        is_initialize = any(
            m.get("method") == "initialize" and "id" in m for m in messages
        )
        if is_initialize:
            session_id = _new_session_id()
            sessions.add(session_id)
        elif session_id is not None and session_id not in sessions:
            # Unknown session — spec says respond 404 so the client re-initializes.
            return JSONResponse({"error": "unknown session"}, status_code=404)

        requests_in = [m for m in messages if _is_request(m)]

        if not requests_in:
            # Only notifications/responses: forward fire-and-forget, return 202.
            for m in messages:
                await mcp.send(m)
            return Response(status_code=202)

        accept = request.headers.get("accept", "")
        wants_sse = "text/event-stream" in accept and "application/json" not in accept.split(",")[0]

        if wants_sse:
            return _sse_response(mcp, messages, session_id, is_initialize)

        # Plain JSON path: await all responses concurrently.
        results: list[dict[str, Any]] = []
        for m in messages:
            if _is_request(m):
                resp = await mcp.send(m)
                if resp is not None:
                    results.append(resp)
            else:
                await mcp.send(m)

        payload: Any = results[0] if isinstance(body, dict) and results else results
        headers = {"Mcp-Session-Id": session_id} if is_initialize and session_id else {}
        return JSONResponse(payload, headers=headers)

    return handler


def _sse_response(
    mcp: StdioMCP,
    messages: list[dict[str, Any]],
    session_id: str | None,
    is_initialize: bool,
) -> EventSourceResponse:
    async def gen() -> AsyncGenerator[dict[str, str]]:
        for m in messages:
            if _is_request(m):
                resp = await mcp.send(m)
                if resp is not None:
                    yield {"data": json.dumps(resp)}
            else:
                await mcp.send(m)

    headers = {"Mcp-Session-Id": session_id} if is_initialize and session_id else {}
    return EventSourceResponse(gen(), headers=headers)


def _make_get_handler() -> Any:
    # Spec allows GET to open a long-lived SSE stream for server-initiated messages.
    # We don't surface those (StdioMCP drops unsolicited messages); return 405.
    async def handler(_request: Request) -> Response:
        return Response(status_code=405)

    return handler


def _make_delete_handler(sessions: set[str]) -> Any:
    async def handler(request: Request) -> Response:
        session_id = request.headers.get("mcp-session-id")
        if session_id is None:
            return Response(status_code=400)
        sessions.discard(session_id)
        return Response(status_code=204)

    return handler


def _rpc_error(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": None},
        status_code=400,
    )


def build_app(
    config: FerryConfig,
    transports: dict[str, StdioMCP],
    manage_lifecycle: bool = False,
) -> Starlette:
    routes: list[Route] = []

    async def healthz(_request: Request) -> Response:
        all_ok = all(t.health for t in transports.values())
        status = {
            name: ("ok" if t.health else "down") for name, t in transports.items()
        }
        return JSONResponse(
            {"status": "ok" if all_ok else "degraded", "mcps": status},
            status_code=200 if all_ok else 503,
        )

    routes.append(Route("/healthz", healthz, methods=["GET"]))

    for mcp_config in config.mcps:
        mcp = transports[mcp_config.name]
        sessions: set[str] = set()
        routes.append(
            Route(mcp_config.path, _make_post_handler(mcp, sessions), methods=["POST"])
        )
        routes.append(
            Route(mcp_config.path, _make_get_handler(), methods=["GET"])
        )
        routes.append(
            Route(mcp_config.path, _make_delete_handler(sessions), methods=["DELETE"])
        )

    if manage_lifecycle:
        @contextlib.asynccontextmanager
        async def _lifespan(_app: Starlette) -> AsyncGenerator[None]:
            async with asyncio.TaskGroup() as tg:
                for t in transports.values():
                    tg.create_task(t.start())
            try:
                yield
            finally:
                async with asyncio.TaskGroup() as tg:
                    for t in transports.values():
                        tg.create_task(t.stop())

        return Starlette(routes=routes, lifespan=_lifespan)

    return Starlette(routes=routes)
