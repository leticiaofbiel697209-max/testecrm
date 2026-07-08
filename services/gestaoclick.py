"""Cliente mínimo para API GestãoClick."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class GestaoClickClient:
    def __init__(self, access_token: str, secret_token: str, base_url: str = "https://api.gestaoclick.com"):
        self.access_token = access_token
        self.secret_token = secret_token
        self.base_url = base_url.rstrip("/")

    def request(self, path: str, method: str = "GET", payload=None, query: dict | None = None):
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        url = f"{self.base_url}/{path.lstrip('/')}{qs}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "access-token": self.access_token,
                "secret-access-token": self.secret_token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
