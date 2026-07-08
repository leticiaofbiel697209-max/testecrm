"""Integração waTidy/Wascript usando apenas o endpoint configurado."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error


class WatidyClient:
    def __init__(self, base_url: str, token: str, send_path: str = "/api/enviar-texto/{token}"):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "").strip()
        self.send_path = str(send_path or "").strip() or "/api/enviar-texto/{token}"
        if not self.base_url or not self.token:
            raise ValueError("Configure base_url e token do waTidy.")

    def url_envio(self) -> str:
        path = self.send_path.replace("{token}", urllib.parse.quote(self.token))
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def enviar_texto(self, telefone: str, mensagem: str) -> dict:
        payload = {
            "phone": telefone,
            "telefone": telefone,
            "numero": telefone,
            "message": mensagem,
            "mensagem": mensagem,
            "text": mensagem,
        }
        req = urllib.request.Request(
            self.url_envio(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return {"ok": 200 <= response.status < 300, "status": response.status, "body": raw}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "status": exc.code, "body": body}

    def testar_conexao(self) -> dict:
        return {"ok": True, "endpoint": self.url_envio()}
