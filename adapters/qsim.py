#!/usr/bin/env python3
"""Agent-side access to the original task's MCP service; no proxy or scorer."""

import argparse
import asyncio
import json
import os


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "call"))
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    servers = json.loads(os.environ["QIQC_MCP_SERVERS"])
    if len(servers) != 1 or servers[0]["transport"] != "streamable-http":
        parser.error("Expected one task MCP service from the runner")
    if args.action == "call" and not args.tool:
        parser.error("call requires a tool name")
    parameters = json.loads(args.arguments)
    if not isinstance(parameters, dict):
        parser.error("tool arguments must be a JSON object")
    async with streamable_http_client(servers[0]["url"]) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = (await session.list_tools() if args.action == "list"
                      else await session.call_tool(args.tool, parameters))
            print(json.dumps(result.model_dump(mode="json")))
            if getattr(result, "isError", False):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
