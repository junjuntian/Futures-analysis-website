from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Credentials:
    base_url: str
    origin: str
    username: str
    password: str


def load_credentials() -> Credentials:
    path = Path(os.environ.get("COLLECTOR_CREDENTIALS_FILE", "/run/secrets/collector-credentials"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    base_url = str(payload.get("base_url", "")).rstrip("/")
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("collector credentials contain an invalid platform URL")
    if not username or not password:
        raise ValueError("collector credentials are incomplete")
    origin = str(payload.get("origin") or f"{parsed.scheme}://{parsed.netloc}").rstrip("/")
    _drop_container_privileges()
    return Credentials(base_url=base_url, origin=origin, username=username, password=password)


def _drop_container_privileges() -> None:
    """Read the root:0400 secret first, then run every network operation unprivileged."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
