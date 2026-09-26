"""Verify a GHCR Yoto image tag can be read without GitHub credentials."""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any

IMAGE = "realbeepmcjeep/yoto-mcp"
TOKEN_URL = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{IMAGE}:pull"
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def verify_public_manifest(
    tag: str, *, open_url: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """Use an anonymous registry token (never Docker/GitHub login) and HEAD the tag."""
    if tag != "latest" and not re.fullmatch(r"sha-[0-9a-f]{7,40}", tag):
        raise ValueError("Expected latest or sha-<hex commit> tag")
    with open_url(urllib.request.Request(TOKEN_URL), timeout=15) as response:
        token = json.load(response).get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("GHCR did not issue an anonymous pull token")
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{IMAGE}/manifests/{tag}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
    )
    with open_url(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Anonymous GHCR manifest check returned {response.status}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="latest or sha-<commit>")
    args = parser.parse_args()
    verify_public_manifest(args.tag)
    print(f"Anonymous GHCR pull verified for {IMAGE}:{args.tag}")


if __name__ == "__main__":
    main()
