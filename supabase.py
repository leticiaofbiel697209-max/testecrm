"""Serviços de persistência Google Sheets do CRM Novaprint."""

from app import (
    NOME_PLANILHA,
    aba_sheets,
    carregar_persistencia_crm,
    conectar_google_sheets,
    garantir_abas_crm,
)

__all__ = [
    "NOME_PLANILHA",
    "aba_sheets",
    "carregar_persistencia_crm",
    "conectar_google_sheets",
    "garantir_abas_crm",
]
