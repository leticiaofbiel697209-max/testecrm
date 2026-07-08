"""Formatadores compartilhados sem dependência do Streamlit."""

from __future__ import annotations

from html import escape


def moeda(valor) -> str:
    try:
        numero = float(valor or 0)
    except Exception:
        numero = 0.0
    return f"R${numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def html_seguro(valor) -> str:
    return escape(str(valor or ""))


def percentual(valor) -> str:
    try:
        return f"{float(valor):.1f}%".replace(".", ",")
    except Exception:
        return "0,0%"
