"""Run the Yoto MCP server over stdio or opt-in Streamable HTTP."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .config import Settings
from .server import create_server, run_http_server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="yoto-mcp")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="run an MCP transport")
    serve.add_argument("--transport", choices=("streamable-http",), default="streamable-http")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.command == "serve":
        server = create_server(settings, interactive_auth=False)
    else:
        server = create_server(settings)
    if args.command == "serve":
        run_http_server(server, host=args.host, port=args.port, settings=settings)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
