"""Correção global de textos UTF-8 exibidos pelo Streamlit.

Este módulo é carregado automaticamente pelo Python na inicialização. Ele
intercepta a importação do Streamlit e corrige textos que foram salvos como
UTF-8, mas interpretados como Windows-1252/Latin-1 (ex.: "GestÃ£o").

A correção atua apenas em textos exibidos pela interface; não altera valores
numéricos, DataFrames, arquivos, tokens ou chamadas de API.
"""

from __future__ import annotations

import builtins
from functools import wraps
from typing import Any


_IMPORT_ORIGINAL = builtins.__import__
_PATCH_APLICADO = False


def corrigir_texto(valor: Any) -> Any:
    """Corrige mojibake comum sem modificar textos que já estejam corretos."""
    if not isinstance(valor, str):
        return valor

    texto = valor
    marcadores = ("Ã", "Â", "â€", "ðŸ", "ï¿½")
    if not any(marcador in texto for marcador in marcadores):
        return texto

    # Alguns textos passaram por mais de uma conversão incorreta. Duas
    # tentativas são suficientes para casos como "GestÃƒÂ£o".
    for _ in range(2):
        if not any(marcador in texto for marcador in marcadores):
            break
        corrigido = None
        for codificacao in ("cp1252", "latin-1"):
            try:
                candidato = texto.encode(codificacao).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if candidato != texto:
                corrigido = candidato
                break
        if corrigido is None:
            break
        texto = corrigido

    return texto


def _corrigir_objeto(valor: Any) -> Any:
    if isinstance(valor, str):
        return corrigir_texto(valor)
    if isinstance(valor, list):
        return [_corrigir_objeto(item) for item in valor]
    if isinstance(valor, tuple):
        return tuple(_corrigir_objeto(item) for item in valor)
    if isinstance(valor, dict):
        return {chave: _corrigir_objeto(item) for chave, item in valor.items()}
    return valor


def _envolver(funcao):
    if getattr(funcao, "_novaprint_utf8_patch", False):
        return funcao

    @wraps(funcao)
    def chamada(*args, **kwargs):
        args_corrigidos = tuple(_corrigir_objeto(arg) for arg in args)
        kwargs_corrigidos = {
            chave: _corrigir_objeto(valor) for chave, valor in kwargs.items()
        }
        return funcao(*args_corrigidos, **kwargs_corrigidos)

    chamada._novaprint_utf8_patch = True
    return chamada


def _aplicar_patch_streamlit(modulo) -> None:
    global _PATCH_APLICADO
    if _PATCH_APLICADO:
        return

    nomes_modulo = (
        "title", "header", "subheader", "caption", "markdown", "write",
        "text", "code", "latex", "info", "warning", "error", "success",
        "toast", "metric", "tabs", "radio", "selectbox", "multiselect",
        "button", "download_button", "text_input", "text_area",
        "number_input", "date_input", "time_input", "file_uploader",
        "checkbox", "toggle", "slider", "select_slider", "expander",
        "popover", "chat_input", "chat_message", "status", "spinner",
        "page_link", "link_button", "dataframe", "data_editor", "table",
    )

    for nome in nomes_modulo:
        funcao = getattr(modulo, nome, None)
        if callable(funcao):
            try:
                setattr(modulo, nome, _envolver(funcao))
            except Exception:
                pass

    # st.sidebar e objetos retornados por st.columns usam DeltaGenerator.
    try:
        from streamlit.delta_generator import DeltaGenerator

        for nome in nomes_modulo:
            funcao = getattr(DeltaGenerator, nome, None)
            if callable(funcao):
                try:
                    setattr(DeltaGenerator, nome, _envolver(funcao))
                except Exception:
                    pass
    except Exception:
        pass

    _PATCH_APLICADO = True


def _import_com_correcao(name, globals=None, locals=None, fromlist=(), level=0):
    modulo = _IMPORT_ORIGINAL(name, globals, locals, fromlist, level)
    if name == "streamlit" or name.startswith("streamlit."):
        try:
            streamlit = _IMPORT_ORIGINAL("streamlit", globals, locals, (), 0)
            _aplicar_patch_streamlit(streamlit)
        except Exception:
            pass
    return modulo


builtins.__import__ = _import_com_correcao
