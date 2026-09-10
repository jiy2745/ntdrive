"""ntdrive-mcp: stateless MCP server that forwards every tool call to ntdrived.

Tools, descriptions and input schemas come from the registry, so the MCP surface is identical
to the CLI and the SDK. Errors are returned as JSON with `error.code` and `error.hint` rather
than raised, so the agent can read the hint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ntdrive.core.registry import ToolRegistry, load_builtin_tools
from ntdrive.daemon.client import DaemonClient, connect
from ntdrive.errors import NtDriveError

log = logging.getLogger("ntdrive-mcp")


def build_server(registry: ToolRegistry, client: DaemonClient) -> Server:
    """Create the MCP server object."""
    server: Server = Server("ntdrive")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema(),
            )
            for spec in registry
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        try:
            result: dict[str, Any] = await client.acall(name, arguments or {})
        except NtDriveError as exc:
            result = exc.to_dict()
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def serve(client: DaemonClient) -> None:
    """Run over stdio until the client disconnects."""
    registry = load_builtin_tools()
    server = build_server(registry, client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Entry point for `ntdrive-mcp`."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        client = connect(caller="mcp")
    except NtDriveError as exc:
        log.error("%s", exc)
        sys.exit(1)
    asyncio.run(serve(client))


if __name__ == "__main__":
    main()
