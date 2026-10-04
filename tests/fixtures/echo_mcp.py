"""Tiny stdio MCP echo for tests. Reads JSON lines, echoes JSON-RPC responses.

I/O is done on the binary buffers (sys.stdin.buffer / sys.stdout.buffer):
text-mode `sys.stdin` block-buffers and can withhold a lone line indefinitely,
which is exactly the stall this fixture exists to test against.

Cues (by ``method``):
  - ``sleep`` ``{"params":{"seconds":N}}``  — wait N seconds, then echo. Blocks
                                               the read loop (a wedged upstream).
  - ``slow`` ``{"params":{"seconds":N}}``   — like ``sleep`` but on a thread, so
                                               other requests (ping) still answer.
  - ``crash``                                — exit immediately (process dies).
  - ``close_stdout``                         — close stdout but keep the process
                                               alive (partial crash: reader sees
                                               EOF, process lingers).
  - ``big`` ``{"params":{"size":N}}``        — reply with an N-byte blob (tests
                                               the raised stdout buffer limit).
  - anything else                            — echo ``params`` as ``result``.
  - notifications (no id)                    — logged to stderr; no response.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

_write_lock = threading.Lock()


def _reply(msg_id: object, result: object) -> None:
    payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
    with _write_lock:
        sys.stdout.buffer.write(payload.encode() + b"\n")
        sys.stdout.buffer.flush()


def _slow(msg: dict[str, Any]) -> None:
    params: dict[str, Any] = msg.get("params", {})
    time.sleep(float(params.get("seconds", 0)))
    _reply(msg["id"], params)


def main() -> None:
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if raw == b"":
            break  # stdin EOF
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "crash":
            sys.exit(1)
        if method == "close_stdout":
            # os.close(1) drops the pipe fd directly — closing the Python
            # wrapper alone doesn't reliably propagate EOF to the parent. This
            # mimics a process whose stdout vanished while it keeps running.
            os.close(1)
            time.sleep(30)  # stay alive with stdout gone
            continue
        if msg.get("id") is None:
            sys.stderr.write(f"notification: {method}\n")
            sys.stderr.flush()
            continue
        if method == "slow":
            threading.Thread(target=_slow, args=(msg,), daemon=True).start()
            continue
        if method == "sleep":
            time.sleep(float(msg.get("params", {}).get("seconds", 0)))
        if method == "big":
            size = int(msg.get("params", {}).get("size", 0))
            result: dict[str, object] = {"blob": "x" * size}
        else:
            result = msg.get("params", {})
        _reply(msg["id"], result)


if __name__ == "__main__":
    main()
