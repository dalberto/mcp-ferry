"""mcp-ferry: bridge local stdio MCP servers to public HTTPS."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-ferry")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
