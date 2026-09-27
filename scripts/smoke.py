"""Exercise health, auth rejection and a real Streamable HTTP MCP handshake."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

AUTH_FAILURES = {401, 403}

# 0.5 s, 8 kHz mono sine; the image's ffprobe must accept it as an MP3 upload.
TINY_MP3 = base64.b64decode(
    "/+MYxAAK8AbVuUEAAv9HcNtgBg+D4PvUCAIHIPg+fxOD4PoggcRB8H34IOu/wxwG/SGOXfznT7ulJoc4WcQ3/GVEJRyhC3+D/+MYxA8QmUKQAZRQ"
    "AG8H6ge5ABlxEnAPiYDmR1gd6mHvgGgCQCorCKC6//IR6KpEPh8ab//5CgNCUJA1/4lOg0r//////////+MYxAcOaKY8Ad4AAP/////40iEYA4AS"
    "SQXAKMCsF4wfwajCHFKMQulwxvhNjDbBaMD0D4FAXiEAMDAKoSW07dP//vgSpVAg/+MYxAgNqKYsAFa8YCmJFGfQGycnl9GIANucd+h5p+CpmGqC"
    "8YJQC5QA8gRBoACAVL6x////3+U///9a/1//fGMDIYEAAdcF/+MYxAwO6KYsAKewgIIBpyIHmpGIyKqc5FMpq8iHmHkCsYMoFpgYARG6RjOX8Urx"
    "///Z/v9P////////Rf//+TsVQOAwSYQN/+MYxAsNaKYwAAb+RJgKECJ0AhGBgSyEkYCKDdEgESDADgwAMAVCAAsiAEECa4b/////+j///1L///4x"
    "gBgAA30DACAcDAsB/+MYxBANOKZAAVYAACAQC8DEaMsDH2bUDq7FsDekP4DBsCADAMAkAIF4CQCiPhmBCiEgEj/HMIIwyn+MwVBZggpIuB2k4jeP"
    "/+MYxBYSiS6UKZRoAIA5NITgLgBtwDgYSK//gnYK+PMd4mZT//8zL5usvhc//y4nPh8u//6AfAZomNf//QTcauQqO1ct2AA//+MYxAYM8LbRucMA"
    "A/2Zl/4zMpcYMzNxmZtVAm/VVAQp/QUGCvBQUKf4KDHf0FChvgoMFP8FChv+SCpMQU1FMy4xMDCqqqqq"
)


async def smoke(url: str, token: str) -> None:
    base_url = url.rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(45):
            try:
                health = await client.get(f"{base_url}/healthz")
            except httpx.RequestError:
                await asyncio.sleep(1)
                continue
            if health.status_code != 200:
                raise RuntimeError(f"/healthz returned HTTP {health.status_code}")
            break
        else:
            raise RuntimeError("/healthz did not become ready within 45 seconds")

        unauthenticated = await client.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        if unauthenticated.status_code not in AUTH_FAILURES:
            raise RuntimeError(
                f"/mcp without a bearer token returned HTTP {unauthenticated.status_code}, "
                "expected 401 or 403"
            )

        invalid_token = await client.post(
            f"{base_url}/mcp",
            headers={"Authorization": "Bearer invalid-ci-token"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        if invalid_token.status_code not in AUTH_FAILURES:
            raise RuntimeError(
                f"/mcp with an invalid bearer token returned HTTP {invalid_token.status_code}, "
                "expected 401 or 403"
            )

    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=15.0) as transport_client,
        streamable_http_client(f"{base_url}/mcp", http_client=transport_client) as (
            read_stream,
            write_stream,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        if not tools.tools:
            raise RuntimeError("authenticated MCP tools/list returned no tools")
        version = initialized.protocol_version
        tool_count = len(tools.tools)
        if "create_mp3_upload" not in {tool.name for tool in tools.tools}:
            raise RuntimeError("HTTP deployment lacks create_mp3_upload (is YOTO_UPLOAD_ROOT set?)")
        link = await session.call_tool("create_mp3_upload", {"filename": "smoke.mp3"})
        if link.is_error:
            raise RuntimeError("create_mp3_upload returned an error")
        upload_url = link.structured_content["upload_url"]

    async with httpx.AsyncClient(timeout=30.0) as anonymous:
        uploaded = await anonymous.put(upload_url, content=TINY_MP3)
        if uploaded.status_code != 201 or not uploaded.json().get("file_path", "").endswith("/smoke.mp3"):
            raise RuntimeError(f"MP3 upload link returned HTTP {uploaded.status_code}")
        replay = await anonymous.put(upload_url, content=TINY_MP3)
        if replay.status_code != 404:
            raise RuntimeError(f"Reused upload link returned HTTP {replay.status_code}, expected 404")

    print(
        "Smoke passed: unauthenticated healthz, missing/wrong bearer rejection, "
        f"authenticated MCP initialize + tools/list ({tool_count} tools, protocol {version}), "
        "single-use MP3 upload link."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("YOTO_SMOKE_URL", "http://127.0.0.1:18000"),
        help="server base URL (default: %(default)s)",
    )
    args = parser.parse_args()
    token = os.environ.get("YOTO_HTTP_TOKEN", "")
    if len(token) < 32:
        parser.error("YOTO_HTTP_TOKEN must be set to a 32-character-or-longer smoke token")
    try:
        asyncio.run(smoke(args.url, token))
    except (httpx.RequestError, httpx2.RequestError, RuntimeError) as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
