"""Camada opcional para Supabase.

Para ativar no futuro, configure no Streamlit secrets:

[supabase]
url = "https://..."
key = "..."

O CRM continua usando Google Sheets enquanto essas credenciais não existirem.
"""

import streamlit as st


def supabase_configurado():
    try:
        cfg = st.secrets.get("supabase", {})
        return bool(str(cfg.get("url", "")).strip() and str(cfg.get("key", "")).strip())
    except Exception:
        return False

