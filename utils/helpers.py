"""Helpers puros compartilhados entre módulos."""

from __future__ import annotations

import re
from datetime import datetime


def somente_digitos(valor) -> str:
    return re.sub(r"\D+", "", str(valor or ""))


def agora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def texto_valido(valor, padrao: str = "") -> str:
    texto = str(valor or "").strip()
    return texto if texto and texto.lower() not in {"nan", "none", "null"} else padrao
