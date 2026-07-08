"""Serviço central de persistência no Supabase."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.url = str(url or "").rstrip("/")
        self.key = str(key or "").strip()
        if not self.url or not self.key:
            raise ValueError("Configure url e key do Supabase.")

    def request(self, table: str, method: str = "GET", query: str = "", body=None, prefer: str | None = None):
        endpoint = f"{self.url}/rest/v1/{urllib.parse.quote(table)}{query or ''}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else []

    def select(self, table: str, query: str = ""):
        return self.request(table, "GET", query=query)

    def insert(self, table: str, payload: dict):
        return self.request(table, "POST", body=payload, prefer="return=representation")

    def update(self, table: str, query: str, payload: dict):
        return self.request(table, "PATCH", query=query, body=payload, prefer="return=representation")


def configured(secrets: dict) -> bool:
    cfg = secrets.get("supabase", {}) if secrets else {}
    key = cfg.get("key") or cfg.get("anon_key") or cfg.get("service_role_key")
    return bool(str(cfg.get("url", "")).strip() and str(key or "").strip())
