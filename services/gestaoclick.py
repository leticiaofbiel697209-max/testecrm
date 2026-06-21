"""Camada de integração com a API do GestãoClick.

O app principal ainda mantém compatibilidade direta, mas este módulo concentra
o contrato esperado para a próxima etapa de extração.
"""

from app import GestaoClickAPI, api_gestaoclick, gestaoclick_cached_list_all

__all__ = ["GestaoClickAPI", "api_gestaoclick", "gestaoclick_cached_list_all"]
