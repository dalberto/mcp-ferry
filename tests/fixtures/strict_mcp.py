"""MCP fixture enforcing one handshake per process, like Bear."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def main() -> None:
    initialized = False
    ready = False
    acknowledgements = 0
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        method = message.get("method")
        params = message.get("params", {})
        if method == "notifications/initialized":
            acknowledgements += 1
            ready = initialized
            continue
        if "id" not in message:
            continue
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize":
            if initialized:
                response["error"] = {"code": -32600, "message": "initialize already received"}
            elif params.get("reject"):
                response["error"] = {"code": -32602, "message": "unsupported protocol version"}
            else:
                initialized = True
                time.sleep(params.get("delay", 0))
                response["result"] = {
                    "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "strict", "version": "1"},
                }
        elif not ready:
            response["error"] = {"code": -32000, "message": "not initialized"}
        elif method == "tools/list":
            response["result"] = {"tools": []}
        else:
            response["result"] = {"pid": os.getpid(), "acknowledgements": acknowledgements}
        sys.stdout.buffer.write(json.dumps(response).encode() + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
