"""
ClassPilot AI — Streamable HTTP MCP Entry Point

Runs the same FastMCP server as server.py (stdio) but over
Streamable HTTP so remote clients such as ChatGPT custom connectors
can reach it over a network.

The same `mcp` instance and all 7 tools are reused verbatim —
no logic is duplicated here. This file only starts uvicorn with the
correct transport and binding.

Transport: streamable-http  (MCP spec 2024-11-05, supported by FastMCP ≥ 3.0)
Default endpoint: http://127.0.0.1:8000/mcp

Start:
    python -m classpilot.http_server
    or: classpilot-ai-http

Environment variables (all optional, shown with defaults):
    MCP_HTTP_HOST=127.0.0.1     bind address
    MCP_HTTP_PORT=8000          bind port
    MCP_HTTP_PATH=/mcp          URL path for the MCP endpoint

IMPORTANT — security:
    By default the server binds to 127.0.0.1 (localhost only).
    To expose it on your LAN or internet change MCP_HTTP_HOST=0.0.0.0
    and put it behind a reverse proxy with TLS. Never expose it
    directly to the internet without authentication.
"""

import logging

from .config import get_config
from .logging_setup import configure_logging
from .server import mcp          # reuse the existing FastMCP instance — no duplication

logger = logging.getLogger(__name__)


def main() -> None:
    """
    Start the ClassPilot AI MCP server using Streamable HTTP transport.

    Reads MCP_HTTP_HOST / MCP_HTTP_PORT / MCP_HTTP_PATH from the environment
    (or .env file) and passes them directly to FastMCP's run() so there is
    no implicit dependency on FASTMCP_* env vars.
    """
    config = get_config()
    configure_logging(config.log_level)

    host = config.mcp_http_host
    port = config.mcp_http_port
    path = config.mcp_http_path

    logger.info(
        "Starting ClassPilot AI MCP server (streamable-http) on http://%s:%d%s",
        host, port, path,
    )

    # FastMCP 3.x: transport="streamable-http" → runs via uvicorn (Starlette/ASGI).
    # host/port/path are forwarded directly to run_http_async().
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )


if __name__ == "__main__":
    main()
