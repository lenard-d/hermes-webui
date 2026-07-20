"""Profile, HTTP, and filesystem adapters for knowledge implementations."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
import urllib.request


def active_hermes_home() -> Path:
    """Resolve the request-scoped Hermes home, including isolated-profile mode."""
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def active_config_snapshot() -> dict:
    """Read one configuration snapshot for the active profile home."""
    try:
        from api.config import get_config_for_profile_home

        config = get_config_for_profile_home(active_hermes_home())
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


def configured_server(config: Mapping | None, source: str) -> dict:
    servers = config.get("mcp_servers", {}) if isinstance(config, Mapping) else {}
    if not isinstance(servers, Mapping):
        return {}
    source_lower = str(source or "").strip().lower()
    for name, server_config in servers.items():
        if (
            str(name or "").strip().lower() == source_lower
            and isinstance(server_config, dict)
        ):
            return server_config
    return {}


def joplin_connection(
    config: Mapping | None,
    *,
    environ: Mapping[str, str],
) -> tuple[str, str]:
    server_config = configured_server(config, "joplin")
    server_env = server_config.get("env", {}) if isinstance(server_config, dict) else {}
    if not isinstance(server_env, Mapping):
        server_env = {}
    url = str(
        server_env.get("JOPLIN_URL")
        or environ.get("JOPLIN_URL")
        or "http://127.0.0.1:41184"
    ).rstrip("/")
    token = str(server_env.get("JOPLIN_TOKEN") or environ.get("JOPLIN_TOKEN") or "")
    return url, token


class JoplinHTTPAdapter:
    """Bounded adapter for the local Joplin Web Clipper HTTP interface."""

    timeout_seconds = 8
    response_limit_bytes = 2_000_000

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = str(base_url or "").rstrip("/")
        self._token = str(token or "")

    @classmethod
    def from_active_profile(cls, config: Mapping | None = None) -> JoplinHTTPAdapter:
        resolved_config = config if isinstance(config, Mapping) else active_config_snapshot()
        base_url, token = joplin_connection(resolved_config, environ=os.environ)
        return cls(base_url, token)

    def get(self, path: str, params: dict | None = None) -> dict:
        if not self._token:
            raise ValueError("Joplin token is not configured")
        safe_path = "/" + str(path or "").lstrip("/")
        query = dict(params or {})
        # Some Web Clipper builds reject header-only auth on /search. Preserve
        # the compatibility token only there and keep it out of all other URLs.
        if safe_path == "/search":
            query["token"] = self._token
        url = f"{self._base_url}{safe_path}?{urlencode(query)}"
        request = Request(url, headers={"Authorization": f"token {self._token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.response_limit_bytes).decode(
                    "utf-8", errors="replace"
                )
        except HTTPError as exc:
            raise ValueError(f"Joplin API returned HTTP {exc.code}") from None
        except (URLError, TimeoutError):
            raise ValueError("Joplin API is not reachable") from None
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError("Joplin API returned invalid JSON") from None
        return data if isinstance(data, dict) else {}


class KnowledgeFilesystemAdapter:
    """Filesystem adapter used by command parsing and identity-checked wiki reads."""

    def read_command_source(self, script_path: Path) -> str | None:
        if not script_path.exists() or not script_path.is_file():
            return None
        try:
            return script_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def read_identity_checked(
        self,
        path: Path,
        identity: tuple[int, int],
        *,
        max_bytes: int,
    ) -> bytes:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != identity:
                raise FileNotFoundError("knowledge file changed after allowlist snapshot")
            return os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)
