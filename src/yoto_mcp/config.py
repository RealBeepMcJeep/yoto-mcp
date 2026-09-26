"""Local-only configuration. Never embed credentials in process arguments or reprs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    client_id: str = ""
    session_file: Path = Path.home() / ".local/state/yoto-mcp/session.json"
    upload_root: Path | None = None
    job_root: Path | None = None
    allow_writes: bool = False
    http_token: str = field(default="", repr=False)
    allowed_hosts: tuple[str, ...] = (
        "yoto-mcp", "yoto-mcp:*", "localhost", "localhost:*",
        "127.0.0.1", "127.0.0.1:*",
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        state = values.get("XDG_STATE_HOME")
        default_file = Path(state) / "yoto-mcp/session.json" if state else cls.session_file
        return cls(
            client_id=values.get("YOTO_CLIENT_ID", ""),
            session_file=Path(values.get("YOTO_SESSION_FILE") or default_file),
            upload_root=Path(values["YOTO_UPLOAD_ROOT"]) if values.get("YOTO_UPLOAD_ROOT") else None,
            job_root=Path(values["YOTO_JOB_ROOT"]) if values.get("YOTO_JOB_ROOT") else None,
            allow_writes=values.get("YOTO_ALLOW_WRITES") == "1",
            http_token=values.get("YOTO_HTTP_TOKEN", ""),
            allowed_hosts=(
                tuple(host.strip() for host in values["YOTO_ALLOWED_HOSTS"].split(",") if host.strip())
                if "YOTO_ALLOWED_HOSTS" in values
                else cls.allowed_hosts
            ),
        )
