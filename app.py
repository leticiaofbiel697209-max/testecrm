import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from io import BytesIO
from html import escape
import base64
import gzip
import json
import math
import pickle
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import smtplib
from email.message import EmailMessage
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials

try:
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    )
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "assets" / "logo_novaprint.png"
SNAPSHOT_DIR = BASE_DIR / ".crm_cache"
SNAPSHOT_PATH = SNAPSHOT_DIR / "ultima_base_processada.pkl"

st.set_page_config(page_title="CRM Inteligente Novaprint", layout="wide")

topo_logo, topo_titulo = st.columns([1, 6])
if LOGO_PATH.exists():
    topo_logo.image(str(LOGO_PATH), width=120)
topo_titulo.title("CRM Inteligente - Nível CEO")
topo_titulo.caption("Novaprint Brasil | Comercial, financeiro, retenção e rotina das vendedoras em um só lugar.")

if "dados_processados" not in st.session_state:
    st.session_state.dados_processados = None
if "clientes_ligados" not in st.session_state:
    st.session_state.clientes_ligados = set()
if "observacoes_orc" not in st.session_state:
    st.session_state.observacoes_orc = {}
if "gestaoclick_lojas" not in st.session_state:
    st.session_state.gestaoclick_lojas = []
if "gestaoclick_usuarios" not in st.session_state:
    st.session_state.gestaoclick_usuarios = []
if "alteracao_gestaoclick_pendente" not in st.session_state:
    st.session_state.alteracao_gestaoclick_pendente = None
if "metas_vendedor" not in st.session_state:
    st.session_state.metas_vendedor = {}
if "contatos_realizados" not in st.session_state:
    st.session_state.contatos_realizados = []
if "retornos_programados" not in st.session_state:
    st.session_state.retornos_programados = []
if "observacoes_clientes" not in st.session_state:
    st.session_state.observacoes_clientes = []
if "persistencia_crm_carregada" not in st.session_state:
    st.session_state.persistencia_crm_carregada = False
if "persistencia_crm_tentada" not in st.session_state:
    st.session_state.persistencia_crm_tentada = False
if "snapshot_local_tentado" not in st.session_state:
    st.session_state.snapshot_local_tentado = False
if "snapshot_local_carregado" not in st.session_state:
    st.session_state.snapshot_local_carregado = False

NOME_PLANILHA = "CRM_HISTORICO_NOVAPRINT"
USUARIO_PADRAO = "Gabriel"
API_BASE = "https://api.gestaoclick.com"

ABAS_CRM = {
    "ContatosRealizados": [
        "cliente_id", "cliente", "vendedor", "data", "hora",
        "status", "observacao", "origem"
    ],
    "RetornosProgramados": [
        "id", "cliente_id", "cliente", "vendedor", "data_retorno",
        "motivo", "observacao", "status", "criado_em", "concluido_em"
    ],
    "ObservacoesClientes": [
        "id", "cliente_id", "cliente", "vendedor", "data", "hora", "observacao"
    ],
    "ResumoDiario": [
        "data", "vendedor", "clientes_para_ligar", "orcamentos_sem_retorno",
        "proximos_recompra", "retornos_hoje", "risco_perda", "gerado_em"
    ],
}

class GestaoClickAPI:
    def __init__(self, access_token, secret_token):
        self.headers = {
            "Content-Type": "application/json",
            "access-token": access_token,
            "secret-access-token": secret_token,
        }
        self.last_request = 0.0

    def request(self, path, params=None, method="GET", body=None):
        elapsed = time.monotonic() - self.last_request
        if elapsed < 0.36:
            time.sleep(0.36 - elapsed)

        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GestÃ£oClick retornou erro {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"NÃ£o foi possÃ­vel acessar o GestÃ£oClick: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(
                "O GestÃ£oClick demorou para responder. Tente novamente em alguns "
                "segundos ou confira os tokens."
            ) from exc
        finally:
            self.last_request = time.monotonic()

        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("message") or "Resposta inesperada do GestÃ£oClick."
            )
        return payload

    def list_all(self, path, params=None):
        return gestaoclick_cached_list_all(
            self.headers["access-token"],
            self.headers["secret-access-token"],
            path,
            json.dumps(params or {}, sort_keys=True, default=str),
        )

    def stores(self):
        return self.list_all("/lojas")

    def users(self, store_id):
        return self.list_all("/usuarios", {"loja_id": store_id})

    def clients(self, store_id, search_text=""):
        search_text = str(search_text or "").strip()
        tentativas = []
        base = {"loja_id": store_id}
        if search_text:
            tentativas = [
                {**base, "cpf": search_text},
                {**base, "cnpj": search_text},
                {**base, "nome": search_text},
                {**base, "busca": search_text},
                {**base, "cpf_cnpj": search_text},
                {**base, "documento": search_text},
            ]
        else:
            tentativas = [base]
        ultimo_erro = None
        for params in tentativas:
            try:
                registros = self.list_all("/clientes", params)
                if registros:
                    alvo = somente_digitos(search_text)
                    if alvo and len(alvo) >= 8:
                        exatos = [
                            item for item in registros
                            if alvo in somente_digitos(documento_cliente_registro(item))
                            or alvo in somente_digitos(item.get("cnpj"))
                            or alvo in somente_digitos(item.get("cpf"))
                        ]
                        if exatos:
                            return exatos
                    return registros
            except Exception as exc:
                ultimo_erro = exc
        alvo = somente_digitos(search_text)
        if alvo and len(alvo) >= 8:
            try:
                registros = self.list_all("/clientes", {**base})
                exatos = [
                    item for item in registros
                    if alvo in somente_digitos(documento_cliente_registro(item))
                    or alvo in somente_digitos(item.get("cnpj"))
                    or alvo in somente_digitos(item.get("cpf"))
                ]
                if exatos:
                    return exatos
            except Exception as exc:
                ultimo_erro = exc
        if ultimo_erro:
            raise ultimo_erro
        return []

    def sales(self, start_date, end_date, store_id):
        return self.list_all("/vendas", {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        })

    def sale_statuses(self, store_id):
        return self.list_all("/situacoes_vendas", {"loja_id": store_id})

    def budgets(self, start_date, end_date, store_id):
        return self.list_all("/orcamentos", {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        })

    def budget_statuses(self, store_id):
        return self.list_all("/situacoes_orcamentos", {"loja_id": store_id})

    def products(self, store_id, search_text="", code=""):
        params = {"loja_id": store_id, "ativo": 1}
        if str(code or "").strip():
            params["codigo"] = str(code).strip()
        elif str(search_text or "").strip():
            params["nome"] = str(search_text).strip()
        return self.list_all("/produtos", params)

    def create_budget(self, payload, store_id):
        payload = dict(payload)
        payload["loja_id"] = int(store_id)
        return self.request(
            "/orcamentos",
            {"loja_id": store_id},
            method="POST",
            body=payload,
        ).get("data") or {}

    def find_budget_by_code(self, budget_code, store_id):
        data = self.list_all("/orcamentos", {
            "tipo": "produto",
            "codigo": budget_code,
            "loja_id": store_id,
        })
        exact = [
            budget for budget in data
            if str(budget.get("codigo") or "").strip() == str(budget_code).strip()
        ]
        return exact[0] if exact else None

    def open_receivables(self, store_id):
        records = []
        seen = set()
        for status in ("ab", "at"):
            for item in self.list_all("/recebimentos", {
                "loja_id": store_id,
                "liquidado": status,
            }):
                key = str(item.get("id") or item.get("codigo") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                copy = dict(item)
                copy["_status_financeiro"] = (
                    "ATRASADO" if status == "at" else "EM ABERTO"
                )
                records.append(copy)
        return records

    def open_payables(self, store_id):
        records = []
        seen = set()
        for status in ("ab", "at"):
            for item in self.list_all("/pagamentos", {
                "loja_id": store_id,
                "liquidado": status,
            }):
                key = str(item.get("id") or item.get("codigo") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                copy = dict(item)
                copy["_status_financeiro"] = (
                    "ATRASADO" if status == "at" else "EM ABERTO"
                )
                records.append(copy)
        return records

    def settled_movements(self, path, start_date, end_date, store_id):
        return self.list_all(path, {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
            "liquidado": "pg",
        })

    def budget(self, budget_id, store_id):
        return self.request(
            f"/orcamentos/{budget_id}", {"loja_id": store_id}
        ).get("data") or {}

    @staticmethod
    def prepare_budget(budget):
        budget["tipo"] = (
            "servico"
            if budget.get("servicos") and not budget.get("produtos")
            else "produto"
        )
        for wrapper in budget.get("produtos") or []:
            product = wrapper.get("produto") or {}
            if not product.get("id") and product.get("produto_id"):
                product["id"] = product["produto_id"]
        return budget

    def append_budget_note(self, budget_id, store_id, note, user):
        budget = self.budget(budget_id, store_id)
        if not budget:
            raise RuntimeError("O orÃ§amento nÃ£o foi encontrado no GestÃ£oClick.")

        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        entry = f"[CRM {timestamp}] {user} | {note.strip()}"
        previous = str(budget.get("observacoes_interna") or "").strip()
        budget["observacoes_interna"] = f"{previous}\n{entry}".strip()
        budget = self.prepare_budget(budget)
        return self.request(
            f"/orcamentos/{budget_id}",
            {"loja_id": store_id},
            method="PUT",
            body=budget,
        ).get("data") or {}

    def update_budget_status(self, budget_id, store_id, status_id):
        budget = self.budget(budget_id, store_id)
        if not budget:
            raise RuntimeError("O orçamento não foi encontrado no GestãoClick.")
        budget["situacao_id"] = int(status_id)
        budget = self.prepare_budget(budget)
        return self.request(
            f"/orcamentos/{budget_id}",
            {"loja_id": store_id},
            method="PUT",
            body=budget,
        ).get("data") or {}

@st.cache_data(ttl=3600, show_spinner=False)
def gestaoclick_cached_list_all(access_token, secret_token, path, params_json):
    params = json.loads(params_json or "{}")
    headers = {
        "Content-Type": "application/json",
        "access-token": access_token,
        "secret-access-token": secret_token,
    }
    records = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"pagina": page, "limite": 100})
        url = API_BASE + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GestÃ£oClick retornou erro {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"NÃ£o foi possÃ­vel acessar o GestÃ£oClick: {exc.reason}"
            ) from exc
        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("message") or "Resposta inesperada do GestÃ£oClick."
            )
        page_records = payload.get("data") or []
        if not page_records:
            break
        records.extend(page_records)
        meta = payload.get("meta") or {}
        if not meta.get("proxima_pagina") and len(page_records) < 100:
            break
        page += 1
        if page > 200:
            raise RuntimeError("A consulta excedeu 200 pÃ¡ginas.")
    return records

def deduplicar_registros(registros):
    unicos = {}
    sem_id = []
    for item in registros:
        key = str(item.get("id") or "").strip()
        if key:
            unicos[key] = item
        else:
            sem_id.append(item)
    return list(unicos.values()) + sem_id

def custo_total_venda(item):
    custo = 0.0
    for campo in ("produtos", "servicos"):
        for wrapper in item.get(campo) or []:
            detalhe = wrapper.get("produto") or wrapper.get("servico") or {}
            quantidade = pd.to_numeric(
                pd.Series([detalhe.get("quantidade") or 1]), errors="coerce"
            ).fillna(1).iloc[0]
            custo_unitario = pd.to_numeric(
                pd.Series([detalhe.get("valor_custo") or 0]), errors="coerce"
            ).fillna(0).iloc[0]
            custo += float(quantidade) * float(custo_unitario)
    if custo == 0:
        custo = float(pd.to_numeric(
            pd.Series([item.get("valor_custo") or 0]), errors="coerce"
        ).fillna(0).iloc[0])
    return custo

def valor_numerico_simples(valor, padrao=0.0):
    if valor is None:
        return padrao
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return padrao
        texto = re.sub(r"[^0-9,.\-]", "", texto)
        if "," in texto and "." in texto:
            texto = texto.replace(".", "").replace(",", ".")
        elif "," in texto:
            texto = texto.replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
            texto = texto.replace(".", "")
        try:
            return float(texto)
        except Exception:
            return padrao
    convertido = pd.to_numeric(pd.Series([valor]), errors="coerce")
    if convertido.isna().iloc[0]:
        return padrao
    return float(convertido.iloc[0])

def primeiro_valor_campos(*fontes, campos):
    for fonte in fontes:
        if not isinstance(fonte, dict):
            continue
        for campo in campos:
            valor = fonte.get(campo)
            if valor is not None and str(valor).strip():
                return str(valor).strip()
    return ""

def buscar_valor_recursivo(objeto, campos):
    if isinstance(objeto, dict):
        for campo in campos:
            valor = objeto.get(campo)
            if valor is not None and str(valor).strip():
                return str(valor).strip()
        for valor in objeto.values():
            encontrado = buscar_valor_recursivo(valor, campos)
            if encontrado:
                return encontrado
    if isinstance(objeto, list):
        for item in objeto:
            encontrado = buscar_valor_recursivo(item, campos)
            if encontrado:
                return encontrado
    return ""

def documento_cliente_registro(registro):
    campos = [
        "cpf_cnpj", "cpfcnpj", "cnpj", "cpf", "documento",
        "cliente_cpf_cnpj", "cnpj_cliente", "cpf_cliente"
    ]
    return (
        primeiro_valor_campos(registro, campos=campos)
        or buscar_valor_recursivo(registro, campos)
    )

def extrair_itens_registro(registro):
    itens = []
    for campo, chave_detalhe in (("produtos", "produto"), ("servicos", "servico")):
        for wrapper in registro.get(campo) or []:
            detalhe = wrapper.get(chave_detalhe) or {}
            campos_nome = [
                "nome", "descricao", "descriÃ§Ã£o", "nome_produto",
                "produto_nome", "descricao_produto", "descriÃ§Ã£o_produto",
                "nome_servico", "nome_serviÃ§o", "servico_nome",
                "serviÃ§o_nome", "descricao_servico", "descricao_serviÃ§o",
                "referencia", "referÃªncia", "codigo", "cÃ³digo", "sku"
            ]
            nome = (
                primeiro_valor_campos(wrapper, detalhe, campos=campos_nome)
                or buscar_valor_recursivo(wrapper, campos_nome)
            )
            if not nome:
                id_item = primeiro_valor_campos(
                    wrapper, detalhe,
                    campos=[
                        "produto_id", "servico_id", "serviÃ§o_id",
                        "id_produto", "id_servico", "id_serviÃ§o", "id"
                    ]
                )
                tipo = "Produto" if campo == "produtos" else "ServiÃ§o"
                nome = f"{tipo} ID {id_item}" if id_item else "Item sem identificaÃ§Ã£o"
            quantidade = (
                wrapper.get("quantidade")
                or detalhe.get("quantidade")
                or wrapper.get("qtd")
                or 1
            )
            qtd = valor_numerico_simples(quantidade, 1)
            valor_unitario = valor_numerico_simples(
                wrapper.get("valor_unitario")
                or detalhe.get("valor_unitario")
                or detalhe.get("valor_venda")
                or wrapper.get("valor_venda")
                or 0,
                0,
            )
            valor_total = valor_numerico_simples(
                wrapper.get("valor_total")
                or detalhe.get("valor_total")
                or wrapper.get("valor")
                or detalhe.get("valor")
                or 0,
                0,
            )
            valor_item = valor_unitario or (valor_total / qtd if qtd and valor_total else 0)
            partes = [str(nome).strip()]
            if qtd:
                partes.append(f"qtd. {qtd:g}")
            if valor_item:
                partes.append(f"unit. {fmt(valor_item)}")
            itens.append(" | ".join(partes))
    return itens

def percentual_comissao_texto(valor):
    texto = str(valor or "").strip().replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", texto)
    if match:
        pct = float(match.group(1))
    else:
        texto_num = re.sub(r"[^0-9.\-]", "", texto)
        if not texto_num:
            return None
        try:
            pct = float(texto_num)
        except Exception:
            return None
        if 0 < pct <= 0.05:
            pct = pct * 100
    if 0.25 <= pct <= 5:
        return pct
    return None

def buscar_percentual_comissao(objeto):
    candidatos = []
    campos_preferidos = [
        "tipo", "Tipo", "comissao", "comissÃ£o", "percentual_comissao",
        "percentual comissÃ£o", "percentual", "perc_comissao", "comissao_percentual"
    ]
    for campo in campos_preferidos:
        valor = buscar_valor_recursivo(objeto, [campo])
        if valor:
            candidatos.append(valor)
    if isinstance(objeto, dict):
        for chave, valor in objeto.items():
            chave_norm = norm(chave)
            if any(palavra in chave_norm for palavra in ["tipo", "comiss", "percent", "perc"]):
                candidatos.append(valor)
    for valor in candidatos:
        pct = percentual_comissao_texto(valor)
        if pct is not None:
            return pct, str(valor)
    return None, ""

def extrair_itens_comissao(registro):
    itens = []
    for campo, chave_detalhe in (("produtos", "produto"), ("servicos", "servico")):
        for wrapper in registro.get(campo) or []:
            detalhe = wrapper.get(chave_detalhe) or {}
            nome = (
                primeiro_valor_campos(
                    wrapper, detalhe,
                    campos=[
                        "nome", "descricao", "descriÃ§Ã£o", "nome_produto",
                        "produto_nome", "descricao_produto", "nome_servico",
                        "nome_serviÃ§o", "servico_nome", "serviÃ§o_nome",
                    ],
                )
                or buscar_valor_recursivo(
                    wrapper,
                    ["nome", "descricao", "descriÃ§Ã£o", "nome_produto", "produto_nome"]
                )
                or "Item sem identificaÃ§Ã£o"
            )
            quantidade = valor_numerico_simples(
                wrapper.get("quantidade") or detalhe.get("quantidade") or wrapper.get("qtd") or 1,
                1,
            )
            valor_unitario = valor_numerico_simples(
                wrapper.get("valor_unitario")
                or detalhe.get("valor_unitario")
                or wrapper.get("valor_venda")
                or detalhe.get("valor_venda")
                or 0,
                0,
            )
            valor_total = valor_numerico_simples(
                wrapper.get("valor_total")
                or detalhe.get("valor_total")
                or wrapper.get("valor")
                or detalhe.get("valor")
                or 0,
                0,
            )
            if not valor_total and valor_unitario:
                valor_total = valor_unitario * quantidade
            tipo = (
                primeiro_valor_campos(
                    wrapper, detalhe,
                    campos=["tipo", "Tipo", "comissao", "comissÃ£o", "percentual_comissao"]
                )
                or buscar_valor_recursivo(
                    wrapper,
                    ["tipo", "Tipo", "comissao", "comissÃ£o", "percentual_comissao"]
                )
            )
            percentual, tipo_detectado = buscar_percentual_comissao(wrapper)
            if percentual is None:
                percentual, tipo_detectado = buscar_percentual_comissao(detalhe)
            if percentual is None:
                percentual = percentual_comissao_texto(tipo)
                tipo_detectado = tipo
            itens.append({
                "produto": str(nome).strip(),
                "quantidade": quantidade,
                "valor_total": valor_total,
                "tipo": str(tipo_detectado or tipo or "").strip(),
                "percentual": percentual,
            })
    return itens

def agregar_itens_cliente(df, chave_coluna, itens_coluna):
    if df.empty or chave_coluna not in df.columns or itens_coluna not in df.columns:
        return pd.Series(dtype=object)

    def juntar(series):
        itens = []
        vistos = set()
        for valor in series:
            if isinstance(valor, list):
                candidatos = valor
            elif pd.isna(valor) or not str(valor).strip():
                candidatos = []
            else:
                candidatos = [str(valor).strip()]
            for item in candidatos:
                texto = str(item).strip()
                chave = norm(texto)
                if texto_valido(texto) and chave not in vistos:
                    vistos.add(chave)
                    itens.append(texto)
        return itens

    return df.groupby(chave_coluna)[itens_coluna].apply(juntar)

def credenciais_gestaoclick():
    try:
        config = st.secrets.get("gestaoclick", {})
        access = str(config.get("access_token", "")).strip()
        secret = str(config.get("secret_token", "")).strip()
    except Exception:
        access = ""
        secret = ""

    access_manual = str(st.session_state.get("gc_access_token", "")).strip()
    secret_manual = str(st.session_state.get("gc_secret_token", "")).strip()
    access = access_manual or access
    secret = secret_manual or secret
    return access, secret

def credenciais_gestaoclick_no_secrets():
    try:
        config = st.secrets.get("gestaoclick", {})
        return bool(
            str(config.get("access_token", "")).strip()
            and str(config.get("secret_token", "")).strip()
        )
    except Exception:
        return False

def api_gestaoclick():
    access, secret = credenciais_gestaoclick()
    if not access or not secret:
        raise RuntimeError("Informe os dois tokens da API do GestÃ£oClick.")
    return GestaoClickAPI(access, secret)

SUPABASE_TABELAS_CRM = [
    "observacoes",
    "ja_liguei",
    "retornos_programados",
    "historico_cliente",
    "usuarios_vendedoras",
    "crm_snapshots",
    "entregadores",
    "rotas",
    "entregas",
    "ocorrencias",
    "followup_prospeccoes",
    "followup_historico",
]

def credenciais_supabase():
    try:
        config = st.secrets.get("supabase", {})
        url = str(config.get("url", "")).strip().rstrip("/")
        key = str(
            config.get("service_role_key", "")
            or config.get("anon_key", "")
            or config.get("key", "")
        ).strip()
    except Exception:
        url = ""
        key = ""
    return url, key

def testar_conexao_supabase():
    url, key = credenciais_supabase()
    if not url or not key:
        raise RuntimeError("Configure [supabase] url e anon_key nos secrets.")
    resultados = []
    for tabela in SUPABASE_TABELAS_CRM:
        endpoint = f"{url}/rest/v1/{urllib.parse.quote(tabela)}?select=*&limit=1"
        request = urllib.request.Request(
            endpoint,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                resultados.append((tabela, response.status, "OK"))
        except urllib.error.HTTPError as exc:
            detalhe = exc.read().decode("utf-8", errors="replace")
            resultados.append((tabela, exc.code, detalhe[:120]))
        except Exception as exc:
            resultados.append((tabela, "erro", str(exc)[:120]))
    return resultados

def supabase_request(tabela, method="GET", query="", body=None, timeout=20, prefer=None):
    url, key = credenciais_supabase()
    if not url or not key:
        raise RuntimeError("Configure [supabase] url e anon_key nos secrets.")
    endpoint = f"{url}/rest/v1/{urllib.parse.quote(tabela)}{query}"
    data = None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body, default=str).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        texto = response.read().decode("utf-8", errors="replace")
        if not texto:
            return None
        return json.loads(texto)

def credenciais_watidy():
    try:
        config = st.secrets.get("watidy", {})
        send_url = str(config.get("send_url", "")).strip()
        base_url = str(
            config.get("base_url", "https://api-whatsapp.wascript.com.br")
        ).strip().rstrip("/")
        token = str(config.get("token", "")).strip()
        send_path = str(config.get("send_path", "/send-message")).strip()
        fallback_paths = config.get("fallback_paths", [])
        phone_field = str(config.get("phone_field", "phone")).strip() or "phone"
        message_field = str(config.get("message_field", "message")).strip() or "message"
        instance_id = str(config.get("instance_id", "")).strip()
        instance_field = str(config.get("instance_field", "instance_id")).strip() or "instance_id"
    except Exception:
        send_url = ""
        base_url = "https://api-whatsapp.wascript.com.br"
        token = ""
        send_path = "/send-message"
        fallback_paths = []
        phone_field = "phone"
        message_field = "message"
        instance_id = ""
        instance_field = "instance_id"
    return {
        "send_url": send_url,
        "base_url": base_url,
        "token": token,
        "send_path": send_path if send_path.startswith("/") else "/" + send_path,
        "fallback_paths": [
            p if str(p).startswith("/") else "/" + str(p)
            for p in (fallback_paths or [])
            if str(p).strip()
        ],
        "phone_field": phone_field,
        "message_field": message_field,
        "instance_id": instance_id,
        "instance_field": instance_field,
    }

def watidy_configurado():
    cfg = credenciais_watidy()
    return bool((cfg["send_url"] or (cfg["base_url"] and cfg["send_path"])) and cfg["token"])

def limpar_erro_api(texto):
    texto = re.sub(r"<[^>]+>", " ", str(texto or ""))
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:350]

def contas_email_saida():
    try:
        contas = st.secrets.get("email_smtp", {}).get("accounts", [])
    except Exception:
        contas = []
    normalizadas = []
    for conta in contas or []:
        try:
            item = dict(conta)
        except Exception:
            continue
        if str(item.get("email", "")).strip() and str(item.get("password", "")).strip():
            normalizadas.append(item)
    return normalizadas

def conta_email_para_vendedor(vendedor):
    contas = contas_email_saida()
    if not contas:
        return None
    vendedor_norm = norm(vendedor)
    for conta in contas:
        vendedores = conta.get("vendedores", [])
        if isinstance(vendedores, str):
            vendedores = [vendedores]
        if any(norm(nome) == vendedor_norm for nome in vendedores):
            return conta
    for conta in contas:
        if bool(conta.get("default", False)):
            return conta
    return contas[0]

def enviar_email_crm(conta, destinatario, assunto, corpo):
    host = str(conta.get("host", "smtp.gmail.com")).strip()
    port = int(conta.get("port", 587))
    email_origem = str(conta.get("email", "")).strip()
    senha = str(conta.get("password", "")).strip()
    nome = str(conta.get("name", email_origem)).strip()
    if not host or not email_origem or not senha:
        raise RuntimeError("Conta de e-mail incompleta no secrets.")
    msg = EmailMessage()
    msg["From"] = f"{nome} <{email_origem}>"
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg.set_content(corpo)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(email_origem, senha)
        smtp.send_message(msg)
    return email_origem

def enviar_whatsapp_watidy(numero, mensagem):
    cfg = credenciais_watidy()
    if not watidy_configurado():
        raise RuntimeError("Configure [watidy] token e endpoint nos secrets.")
    numero = somente_digitos(numero)
    if not numero:
        raise RuntimeError("Informe o WhatsApp do cliente.")
    if not numero.startswith("55"):
        numero = "55" + numero
    endpoint_get = (
        cfg["base_url"]
        + "/api/enviar-texto/"
        + urllib.parse.quote(cfg["token"])
        + "?"
        + urllib.parse.urlencode({
            "phone": numero,
            "number": numero,
            "numero": numero,
            "telefone": numero,
            "message": mensagem,
            "mensagem": mensagem,
            "text": mensagem,
        })
    )
    request_get = urllib.request.Request(
        endpoint_get,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg['token']}",
            "token": cfg["token"],
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request_get, timeout=20) as response:
            texto = response.read().decode("utf-8", errors="replace")
            return response.status, f"Endpoint usado: GET /api/enviar-texto/{{token}}\n{texto}"
    except urllib.error.HTTPError as exc:
        detalhe_get = exc.read().decode("utf-8", errors="replace")
        if exc.code not in {400, 404, 405, 422}:
            raise RuntimeError(
                f"Watidy retornou erro {exc.code}: {limpar_erro_api(detalhe_get)}"
            ) from exc
    payload_base = {
        cfg["phone_field"]: numero,
        cfg["message_field"]: mensagem,
    }
    if cfg["instance_id"]:
        payload_base[cfg["instance_field"]] = cfg["instance_id"]
    payloads = [
        payload_base,
        {"number": numero, "message": mensagem, **({cfg["instance_field"]: cfg["instance_id"]} if cfg["instance_id"] else {})},
        {"phone": numero, "text": mensagem, **({cfg["instance_field"]: cfg["instance_id"]} if cfg["instance_id"] else {})},
        {"telefone": numero, "mensagem": mensagem, **({cfg["instance_field"]: cfg["instance_id"]} if cfg["instance_id"] else {})},
    ]
    caminhos = []
    for caminho in [
        cfg["send_path"],
        *cfg.get("fallback_paths", []),
        "/send-message",
        "/api/send-message",
        "/message/send",
        "/api/message/send",
        "/messages/send",
        "/api/messages/send",
        "/sendText",
        "/api/sendText",
        "/send/text",
        "/api/send/text",
        "/send",
        "/api/send",
    ]:
        if caminho and caminho not in caminhos:
            caminhos.append(caminho)
    erros = []
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['token']}",
        "token": cfg["token"],
        "x-api-key": cfg["token"],
    }
    endpoints = []
    if cfg["send_url"]:
        endpoints.append(("URL configurada", cfg["send_url"]))
    else:
        for caminho in [
            *caminhos,
            "/api/v1/send-message",
            "/api/v1/messages/send",
            "/api/v1/whatsapp/send-message",
        ]:
            endpoint = cfg["base_url"] + caminho
            if (caminho, endpoint) not in endpoints:
                endpoints.append((caminho, endpoint))
    for rotulo_endpoint, endpoint in endpoints:
        for payload in payloads:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    texto = response.read().decode("utf-8", errors="replace")
                    return response.status, f"Endpoint usado: {rotulo_endpoint}\n{texto}"
            except urllib.error.HTTPError as exc:
                detalhe = exc.read().decode("utf-8", errors="replace")
                detalhe_limpo = limpar_erro_api(detalhe)
                erros.append(f"{rotulo_endpoint}: {exc.code} - {detalhe_limpo}")
                if exc.code not in {400, 404, 405, 422}:
                    raise RuntimeError(f"Watidy retornou erro {exc.code}: {detalhe_limpo}") from exc
            except Exception as exc:
                erros.append(f"{rotulo_endpoint}: {exc}")
                break
    if erros and all(": 404 -" in erro for erro in erros):
        raise RuntimeError(
            "Endpoint Watidy nÃ£o encontrado. Configure [watidy].send_url com a URL completa "
            "exata da opÃ§Ã£o de envio exibida na documentaÃ§Ã£o da sua conta. "
            f"Base atual: {cfg['base_url']}."
        )
    raise RuntimeError(
        "Nenhum endpoint Watidy aceitou o envio. "
        "Confira o send_path da documentaÃ§Ã£o da sua conta. Tentativas: "
        + " | ".join(erros[:4])
    )

def fmt(v):
    try:
        numero = numero_seguro(v, 0.0)
        return f"R${numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$0,00"

def fmt_html(v):
    return fmt(v).replace("$", "&#36;")

def html_seguro(v):
    return escape(str(v), quote=True)

def link_download_bytes(rotulo, conteudo, nome_arquivo, mime):
    if isinstance(conteudo, str):
        conteudo = conteudo.encode("utf-8")
    b64 = base64.b64encode(conteudo).decode("ascii")
    return (
        f'<a href="data:{mime};base64,{b64}" download="{html_seguro(nome_arquivo)}" '
        'style="display:inline-block;background:#ff4b4b;color:white;'
        'padding:0.55rem 0.9rem;border-radius:0.5rem;'
        'text-decoration:none;font-weight:600;">'
        f'{html_seguro(rotulo)}</a>'
    )

def norm(x):
    return str(x).strip().lower().replace("Âº", "o").replace("Â°", "o")

def somente_digitos(x):
    return re.sub(r"\D+", "", str(x or ""))

@st.cache_resource(show_spinner=False)
def conectar_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    service_account_info = dict(st.secrets["gcp_service_account"])

    service_account_info["private_key"] = (
        service_account_info["private_key"]
        .replace("\\n", "\n")
        .strip()
    )

    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=scope
    )

    gc = gspread.authorize(creds)

    return gc.open(NOME_PLANILHA)

def aba_sheets(nome):
    planilha = conectar_google_sheets()
    return planilha.worksheet(nome)

def garantir_abas_crm():
    planilha = conectar_google_sheets()
    existentes = {ws.title: ws for ws in planilha.worksheets()}
    resultado = {}
    for nome, cabecalhos in ABAS_CRM.items():
        ws = existentes.get(nome)
        if ws is None:
            ws = planilha.add_worksheet(
                title=nome, rows=1000, cols=max(10, len(cabecalhos))
            )
            try:
                ws.append_row(cabecalhos)
            except Exception as e:
                if not erro_apenas_response_200(e):
                    raise
        elif not ws.row_values(1):
            try:
                ws.append_row(cabecalhos)
            except Exception as e:
                if not erro_apenas_response_200(e):
                    raise
        resultado[nome] = ws
    return resultado

def carregar_persistencia_crm():
    st.session_state.persistencia_crm_tentada = True
    try:
        abas = garantir_abas_crm()
        st.session_state.contatos_realizados = (
            abas["ContatosRealizados"].get_all_records()
        )
        st.session_state.retornos_programados = (
            abas["RetornosProgramados"].get_all_records()
        )
        st.session_state.observacoes_clientes = (
            abas["ObservacoesClientes"].get_all_records()
        )
        st.session_state.persistencia_crm_carregada = True
        return True
    except Exception as e:
        st.warning(
            f"NÃ£o foi possÃ­vel carregar a persistÃªncia do Google Sheets: {e}"
        )
        return False

def cliente_corresponde(registro, cliente_id, cliente):
    reg_id = str(registro.get("cliente_id", "")).strip()
    if cliente_id and reg_id:
        return reg_id == str(cliente_id).strip()
    return norm(registro.get("cliente", "")) == norm(cliente)

def erro_apenas_response_200(exc):
    texto = f"{exc} {exc!r}"
    resposta = getattr(exc, "response", None)
    status = getattr(resposta, "status_code", None)
    return (
        status == 200
        or "<Response [200]>" in texto
        or "Response [200]" in texto
        or "status_code=200" in texto
    )

def salvar_snapshot_local(dados):
    if not dados:
        return False
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        pacote = {
            "salvo_em": datetime.now(),
            "dados": dados,
        }
        with SNAPSHOT_PATH.open("wb") as arquivo:
            pickle.dump(pacote, arquivo, protocol=pickle.HIGHEST_PROTOCOL)
        return True
    except Exception as e:
        st.warning(f"NÃƒÂ£o consegui salvar a base local estÃƒÂ¡vel: {e}")
        return False

def serializar_snapshot(dados):
    pacote = {
        "versao": 1,
        "salvo_em": datetime.now(),
        "dados": dados,
    }
    bruto = pickle.dumps(pacote, protocol=pickle.HIGHEST_PROTOCOL)
    return base64.b64encode(gzip.compress(bruto)).decode("ascii")

def desserializar_snapshot(payload_base64):
    bruto = base64.b64decode(str(payload_base64 or "").encode("ascii"))
    try:
        bruto = gzip.decompress(bruto)
    except Exception:
        pass
    pacote = pickle.loads(bruto)
    if isinstance(pacote, dict) and "dados" in pacote:
        return pacote.get("dados")
    return pacote

def salvar_snapshot_supabase(dados):
    if not dados:
        return False
    payload = {
        "chave": "ultima_base",
        "salvo_em": datetime.now().isoformat(),
        "origem": str(dados.get("origem", "crm")),
        "payload_base64": serializar_snapshot(dados),
    }
    try:
        supabase_request(
            "crm_snapshots",
            method="POST",
            query="?on_conflict=chave",
            body=payload,
            timeout=30,
            prefer="resolution=merge-duplicates",
        )
        return True
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")
        st.warning(f"NÃƒÂ£o consegui salvar a base no Supabase: {exc.code} - {detalhe[:180]}")
    except Exception as e:
        st.warning(f"NÃƒÂ£o consegui salvar a base no Supabase: {e}")
    return False

def carregar_snapshot_supabase():
    try:
        registros = supabase_request(
            "crm_snapshots",
            method="GET",
            query="?select=payload_base64,salvo_em&chave=eq.ultima_base&limit=1",
            timeout=30,
        )
        if registros:
            dados = desserializar_snapshot(registros[0].get("payload_base64"))
            if isinstance(dados, dict):
                return dados
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")
        st.warning(f"NÃƒÂ£o consegui carregar a base do Supabase: {exc.code} - {detalhe[:180]}")
    except Exception as e:
        st.warning(f"NÃƒÂ£o consegui carregar a base do Supabase: {e}")
    return None

def idade_snapshot_supabase():
    try:
        registros = supabase_request(
            "crm_snapshots",
            method="GET",
            query="?select=salvo_em&chave=eq.ultima_base&limit=1",
            timeout=12,
        )
        if registros:
            salvo_em = pd.to_datetime(registros[0].get("salvo_em"), errors="coerce")
            if pd.notna(salvo_em):
                return salvo_em.strftime("%d/%m/%Y %H:%M")
    except Exception:
        pass
    return ""

def salvar_snapshot_estavel(dados):
    salvo_supabase = salvar_snapshot_supabase(dados)
    salvo_local = salvar_snapshot_local(dados)
    return salvo_supabase or salvo_local

def carregar_snapshot_local():
    st.session_state.snapshot_local_tentado = True
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        with SNAPSHOT_PATH.open("rb") as arquivo:
            pacote = pickle.load(arquivo)
        dados = pacote.get("dados") if isinstance(pacote, dict) else pacote
        if isinstance(dados, dict):
            st.session_state.snapshot_local_carregado = True
            return dados
    except Exception as e:
        st.warning(f"NÃƒÂ£o consegui carregar a ÃƒÂºltima base local: {e}")
    return None

def idade_snapshot_local():
    if not SNAPSHOT_PATH.exists():
        return ""
    try:
        modificado = datetime.fromtimestamp(SNAPSHOT_PATH.stat().st_mtime)
        return modificado.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""

def carregar_snapshot_estavel():
    st.session_state.snapshot_local_tentado = True
    dados = carregar_snapshot_supabase()
    if dados is not None:
        st.session_state.snapshot_local_carregado = True
        return dados
    return carregar_snapshot_local()

def idade_snapshot_estavel():
    return idade_snapshot_supabase() or idade_snapshot_local()

def salvar_contato_realizado(
    cliente_id, cliente, vendedor, observacao="", origem="prioridade", status="jÃ¡ liguei"
):
    agora = datetime.now()
    registro = {
        "cliente_id": str(cliente_id or ""),
        "cliente": str(cliente),
        "vendedor": str(vendedor or "Sem vendedor"),
        "data": agora.strftime("%d/%m/%Y"),
        "hora": agora.strftime("%H:%M:%S"),
        "status": str(status or "jÃ¡ liguei"),
        "observacao": str(observacao or ""),
        "origem": str(origem),
    }
    st.session_state.contatos_realizados.append(registro)
    st.session_state.clientes_ligados.add(str(cliente))

    try:
        abas = garantir_abas_crm()
        abas["ContatosRealizados"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            st.warning(f"Contato registrado na tela, mas nÃ£o consegui salvar na planilha: {e}")

    if observacao:
        try:
            salvar_observacao_cliente(
                cliente_id, cliente, vendedor, observacao
            )
        except Exception as e:
            st.warning(
                f"Contato salvo, mas a observaÃ§Ã£o nÃ£o pÃ´de ser duplicada "
                f"no histÃ³rico: {e}"
            )
    try:
        concluir_retornos_do_cliente(cliente_id, cliente, agora.date())
    except Exception as e:
        if not erro_apenas_response_200(e):
            st.warning(f"Contato salvo, mas o retorno nÃ£o pÃ´de ser concluÃ­do: {e}")
    return registro

def salvar_observacao_cliente(cliente_id, cliente, vendedor, observacao):
    agora = datetime.now()
    registro = {
        "id": str(uuid.uuid4()),
        "cliente_id": str(cliente_id or ""),
        "cliente": str(cliente),
        "vendedor": str(vendedor or "Sem vendedor"),
        "data": agora.strftime("%d/%m/%Y"),
        "hora": agora.strftime("%H:%M:%S"),
        "observacao": str(observacao).strip(),
    }
    st.session_state.observacoes_clientes.append(registro)
    try:
        abas = garantir_abas_crm()
        abas["ObservacoesClientes"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            st.warning(f"ObservaÃ§Ã£o registrada na tela, mas nÃ£o consegui salvar na planilha: {e}")
    return registro

def agendar_retorno_cliente(
    cliente_id, cliente, vendedor, data_retorno, motivo, observacao
):
    agora = datetime.now()
    registro = {
        "id": str(uuid.uuid4()),
        "cliente_id": str(cliente_id or ""),
        "cliente": str(cliente),
        "vendedor": str(vendedor or "Sem vendedor"),
        "data_retorno": data_retorno.strftime("%d/%m/%Y"),
        "motivo": str(motivo or "Retorno comercial"),
        "observacao": str(observacao or ""),
        "status": "pendente",
        "criado_em": agora.strftime("%d/%m/%Y %H:%M:%S"),
        "concluido_em": "",
    }
    st.session_state.retornos_programados.append(registro)
    try:
        abas = garantir_abas_crm()
        abas["RetornosProgramados"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            st.warning(f"Retorno registrado na tela, mas nÃ£o consegui salvar na planilha: {e}")
    return registro

def concluir_retornos_do_cliente(cliente_id, cliente, data_limite):
    pendentes = []
    for retorno in st.session_state.retornos_programados:
        data_retorno = pd.to_datetime(
            retorno.get("data_retorno"), dayfirst=True, errors="coerce"
        )
        if (
            str(retorno.get("status", "")).strip().lower() == "pendente"
            and cliente_corresponde(retorno, cliente_id, cliente)
            and pd.notna(data_retorno)
            and data_retorno.date() <= data_limite
        ):
            pendentes.append(retorno)
    if not pendentes:
        return
    try:
        ws = garantir_abas_crm()["RetornosProgramados"]
        registros = ws.get_all_records()
        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        for retorno in pendentes:
            for linha, atual in enumerate(registros, start=2):
                if str(atual.get("id")) == str(retorno.get("id")):
                    ws.update_cell(linha, 8, "concluÃ­do")
                    ws.update_cell(linha, 10, agora)
                    retorno["status"] = "concluÃ­do"
                    retorno["concluido_em"] = agora
                    break
    except Exception as e:
        st.warning(f"Contato salvo, mas o retorno nÃ£o pÃ´de ser concluÃ­do: {e}")

def carregar_clientes_ligados_hoje():
    try:
        ws = aba_sheets("clientes_ligados")
        dados = ws.get_all_records()
        hoje = datetime.now().strftime("%d/%m/%Y")
        return {str(l["cliente"]).strip() for l in dados if str(l.get("data", "")).strip() == hoje}
    except Exception:
        return set()

def salvar_cliente_ligado(cliente, origem):
    try:
        ws = aba_sheets("clientes_ligados")
        hoje = datetime.now().strftime("%d/%m/%Y")
        ws.append_row([hoje, cliente, USUARIO_PADRAO, origem])
    except Exception as e:
        st.warning(f"NÃ£o consegui salvar no Google Sheets: {e}")

def carregar_observacoes_orcamentos():
    try:
        ws = aba_sheets("orcamentos_observacoes")
        dados = ws.get_all_records()
        obs = {}
        for l in dados:
            num = str(l.get("numero_orcamento", "")).strip()
            if num:
                obs[num] = str(l.get("observacao", ""))
        return obs
    except Exception:
        return {}

def salvar_observacao_orcamento(numero, cliente, observacao):
    try:
        ws = aba_sheets("orcamentos_observacoes")
        hoje = datetime.now().strftime("%d/%m/%Y")
        registros = ws.get_all_records()
        numero = str(numero)

        linha_existente = None
        for i, r in enumerate(registros, start=2):
            if str(r.get("numero_orcamento", "")).strip() == numero:
                linha_existente = i
                break

        if linha_existente:
            ws.update(f"A{linha_existente}:E{linha_existente}", [[numero, cliente, observacao, USUARIO_PADRAO, hoje]])
        else:
            ws.append_row([numero, cliente, observacao, USUARIO_PADRAO, hoje])
    except Exception as e:
        st.warning(f"NÃ£o consegui salvar observaÃ§Ã£o: {e}")

def achar_coluna(df, termos):
    for c in df.columns:
        nc = norm(c)
        for t in termos:
            if norm(t) in nc:
                return c
    return None

def carregar_excel(file, grupos_busca):
    bruto = pd.read_excel(file, header=None, engine="openpyxl")
    melhor_linha, melhor_score = 0, -1
    for i in range(min(15, len(bruto))):
        valores = [norm(x) for x in bruto.iloc[i].tolist()]
        score = 0
        for grupo in grupos_busca:
            if any(any(norm(t) in v for v in valores) for t in grupo):
                score += 1
        if score > melhor_score:
            melhor_linha, melhor_score = i, score

    df = pd.read_excel(file, header=melhor_linha, engine="openpyxl")
    df = df.dropna(how="all")
    df.columns = [str(c).strip() for c in df.columns]
    return df

def data_coluna(s):
    return pd.to_datetime(s, dayfirst=True, errors="coerce")

def numero_coluna(s):
    def converter(v):
        if pd.isna(v):
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)

        texto = re.sub(r"[^\d,.\-]", "", str(v).strip())
        if not texto:
            return 0.0

        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")
        elif "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
            texto = texto.replace(".", "")

        try:
            return float(texto)
        except ValueError:
            return 0.0

    return s.apply(converter)

def texto_valido(valor, padrao=""):
    if valor is None:
        return padrao
    try:
        if pd.isna(valor):
            return padrao
    except Exception:
        pass
    texto = str(valor).strip()
    if texto.lower() in {"", "nan", "none", "nat", "<na>"}:
        return padrao
    return texto

def valor_informado(valor):
    return texto_valido(valor) != ""

def numero_seguro(valor, padrao=0.0):
    try:
        numero = float(valor)
        if pd.isna(numero) or not math.isfinite(numero):
            return padrao
        return numero
    except Exception:
        return padrao

def status_fechado_orcamento(status):
    texto = norm(status).upper()
    if not texto:
        return False
    bloqueados = (
        "APROVADO|CANCELADO|CONFIRMADO|CONCRETIZADO|CONVERTIDO|"
        "FINALIZADO|FATURADO|PERDIDO|RECUSADO|VENDIDO|FECHADO|"
        "GANHO|REPROVADO"
    )
    return bool(re.search(bloqueados, texto))

def status_liquidado_financeiro(status):
    texto = norm(status).upper()
    if not texto:
        return False
    return bool(re.search("LIQUIDADO|PAGO|QUITADO|RECEBIDO|BAIXADO", texto))

def status_orcamento(dias):
    if dias <= 1:
        return "âœ… AceitÃ¡vel"
    if dias == 2:
        return "ðŸ“ž Ligar hoje"
    if dias == 3:
        return "âš ï¸ Urgente"
    return "ðŸš¨ Risco de ter perdido"

def score_risco(media_atraso):
    if pd.isna(media_atraso) or media_atraso <= 0:
        return 100
    return max(0, min(100, int(100 - media_atraso * 2)))

def descricao_score(score):
    if score >= 85:
        return "ðŸŸ¢ Baixo risco de inadimplÃªncia"
    if score >= 65:
        return "ðŸŸ¡ Risco moderado de inadimplÃªncia"
    if score >= 40:
        return "ðŸŸ  Alto risco de inadimplÃªncia"
    return "ðŸ”´ Risco crÃ­tico de inadimplÃªncia"

def temperatura_cliente(dias, intervalo):
    if intervalo <= 0:
        if dias <= 30:
            return "ðŸŸ£ NOVO"
        if dias <= 60:
            return "ðŸŸ¡ ATENÃ‡ÃƒO"
        return "âš« CLIENTE INATIVO"
    if intervalo * 0.9 <= dias <= intervalo * 1.2:
        return "ðŸŸ¢ QUENTE"
    if intervalo * 1.2 < dias <= intervalo * 1.5:
        return "ðŸŸ¡ ATENÃ‡ÃƒO"
    if intervalo * 1.5 < dias <= intervalo * 2:
        return "ðŸ”´ ATRASADO NA RECOMPRA"
    if dias > intervalo * 2:
        return "âš« CLIENTE INATIVO"
    return "ðŸ”µ CEDO"

def sugestao_ia(dias, intervalo, orcs, inad, potencial):
    temp = temperatura_cliente(dias, intervalo)
    if inad > 0:
        return "ðŸ’¸ Cliente com inadimplÃªncia. Priorizar cobranÃ§a antes de nova venda."
    if orcs > 0 and temp in ["ðŸŸ¢ QUENTE", "ðŸŸ¡ ATENÃ‡ÃƒO"]:
        return "ðŸ“„ Cliente com orÃ§amento em aberto e bom momento de compra. Priorizar fechamento hoje."
    if temp == "ðŸŸ¢ QUENTE":
        return f"ðŸŸ¢ Momento ideal. Ligar com oferta direta. Potencial mensal: {fmt(potencial)}."
    if temp == "ðŸŸ¡ ATENÃ‡ÃƒO":
        return "ðŸŸ¡ Cliente passou levemente do ciclo. Fazer contato de retomada antes que esfrie."
    if temp == "ðŸ”´ ATRASADO NA RECOMPRA":
        return "ðŸ”´ Cliente atrasado na recompra. Entender se comprou de concorrente ou se esqueceu."
    if temp == "âš« CLIENTE INATIVO":
        return "âš« Cliente inativo. Usar abordagem de reativaÃ§Ã£o com condiÃ§Ã£o especial."
    if orcs > 0:
        return "ðŸ“„ Cliente com orÃ§amento em aberto. Fazer follow-up comercial."
    if temp == "ðŸ”µ CEDO":
        return "ðŸ”µ Ainda cedo para venda direta. Manter relacionamento ou aquecer contato."
    return "ðŸŸ£ Cliente novo. Iniciar relacionamento comercial."

def score_comercial(row):
    score = 0
    temp = row["temperatura"]
    if temp == "ðŸŸ¢ QUENTE":
        score += 40
    elif temp == "ðŸŸ¡ ATENÃ‡ÃƒO":
        score += 30
    elif temp == "ðŸ”´ ATRASADO NA RECOMPRA":
        score += 20
    elif temp == "âš« CLIENTE INATIVO":
        score += 10
    if row["orcamentos_em_aberto"] > 0:
        score += 20
    if row["score_risco"] >= 85:
        score += 20
    elif row["score_risco"] >= 65:
        score += 10
    if row["potencial_mensal"] > 0:
        score += 20
    return min(score, 100)

def preparar_financeiro(contas, col_cliente, col_vencimento, col_valor, col_status):
    financeiro = pd.DataFrame({
        "Cliente": contas[col_cliente].astype(str).str.strip(),
        "Vencimento": (
            contas[col_vencimento]
            if col_vencimento
            else pd.Series(pd.NaT, index=contas.index)
        ),
        "Valor": contas[col_valor],
        "Situacao": (
            contas[col_status].astype(str)
            if col_status
            else pd.Series("EM ABERTO", index=contas.index)
        ),
    })
    financeiro["Vencimento"] = pd.to_datetime(
        financeiro["Vencimento"], errors="coerce"
    )
    financeiro["Valor"] = pd.to_numeric(
        financeiro["Valor"], errors="coerce"
    ).fillna(0)
    financeiro["Situacao"] = financeiro["Situacao"].str.upper().str.strip()

    status_pago = "PAGO|LIQUIDADO|RECEBIDO|CONFIRMADO|QUITADO"
    financeiro["Liquidado"] = financeiro["Situacao"].str.contains(
        status_pago, na=False, regex=True
    )
    financeiro = financeiro[
        (~financeiro["Liquidado"]) &
        financeiro["Cliente"].ne("") &
        financeiro["Vencimento"].notna() &
        financeiro["Valor"].gt(0)
    ].copy()

    hoje = pd.Timestamp(date.today())
    financeiro["Dias_para_vencer"] = (
        financeiro["Vencimento"].dt.normalize() - hoje
    ).dt.days
    financeiro["Vencida"] = (
        financeiro["Dias_para_vencer"].lt(0) |
        financeiro["Situacao"].str.contains("ATRASADO|VENCIDO", na=False, regex=True)
    )
    financeiro["Dias_atraso"] = (
        -financeiro["Dias_para_vencer"]
    ).clip(lower=0)

    def faixa_recebimento(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido atÃ© 7 dias"
            if atraso <= 15:
                return "Vencido de 8 a 15 dias"
            if atraso <= 30:
                return "Vencido de 16 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A vencer em atÃ© 7 dias"
        if dias <= 15:
            return "A vencer de 8 a 15 dias"
        if dias <= 30:
            return "A vencer de 16 a 30 dias"
        if dias <= 60:
            return "A vencer de 31 a 60 dias"
        return "A vencer acima de 60 dias"

    financeiro["Faixa"] = financeiro.apply(faixa_recebimento, axis=1)
    return financeiro.sort_values(["Vencida", "Vencimento"], ascending=[False, True])

def calcular_metricas_financeiras(financeiro):
    vazio = {
        "total_aberto": 0.0,
        "total_vencido": 0.0,
        "percentual_vencido": 0.0,
        "vence_7": 0.0,
        "vence_15": 0.0,
        "vence_30": 0.0,
        "prazo_medio": 0.0,
        "concentracao_top5": 0.0,
        "clientes_devedores": 0,
    }
    if financeiro is None or financeiro.empty:
        return vazio

    total = float(financeiro["Valor"].sum())
    vencido = float(financeiro.loc[financeiro["Vencida"], "Valor"].sum())
    futuro = financeiro[~financeiro["Vencida"]]
    por_cliente = financeiro.groupby("Cliente")["Valor"].sum().sort_values(ascending=False)
    metricas = dict(vazio)
    metricas.update({
        "total_aberto": total,
        "total_vencido": vencido,
        "percentual_vencido": (vencido / total * 100) if total else 0.0,
        "vence_7": float(futuro.loc[futuro["Dias_para_vencer"].between(0, 7), "Valor"].sum()),
        "vence_15": float(futuro.loc[futuro["Dias_para_vencer"].between(8, 15), "Valor"].sum()),
        "vence_30": float(futuro.loc[futuro["Dias_para_vencer"].between(16, 30), "Valor"].sum()),
        "prazo_medio": float(
            (futuro["Dias_para_vencer"] * futuro["Valor"]).sum() /
            futuro["Valor"].sum()
        ) if futuro["Valor"].sum() else 0.0,
        "concentracao_top5": (
            float(por_cliente.head(5).sum()) / total * 100
        ) if total else 0.0,
        "clientes_devedores": int(
            financeiro.loc[financeiro["Vencida"], "Cliente"].nunique()
        ),
    })
    return metricas

def preparar_contas_pagar(pagamentos):
    colunas = [
        "Fornecedor", "Descricao", "Vencimento", "Valor", "Situacao",
        "Dias_para_vencer", "Vencida", "Dias_atraso", "Faixa",
        "Plano_conta", "Forma_pagamento"
    ]
    if not pagamentos:
        return pd.DataFrame(columns=colunas)

    pagar = pd.DataFrame([{
        "Fornecedor": (
            item.get("nome_fornecedor")
            or item.get("nome_transportadora")
            or item.get("nome_funcionario")
            or item.get("nome_cliente")
            or "Sem fornecedor informado"
        ),
        "Descricao": item.get("descricao") or "",
        "Vencimento": pd.to_datetime(
            item.get("data_vencimento"), format="%Y-%m-%d", errors="coerce"
        ),
        "Valor": item.get("valor_total") or item.get("valor") or 0,
        "Situacao": item.get("_status_financeiro") or "EM ABERTO",
        "Plano_conta": item.get("nome_plano_conta") or "",
        "Forma_pagamento": item.get("nome_forma_pagamento") or "",
    } for item in pagamentos])
    pagar["Valor"] = numero_coluna(pagar["Valor"])
    pagar = pagar[
        pagar["Vencimento"].notna() & pagar["Valor"].gt(0)
    ].copy()
    hoje = pd.Timestamp(date.today())
    pagar["Dias_para_vencer"] = (
        pagar["Vencimento"].dt.normalize() - hoje
    ).dt.days
    pagar["Vencida"] = (
        pagar["Dias_para_vencer"].lt(0) |
        pagar["Situacao"].str.upper().str.contains(
            "ATRASADO|VENCIDO", na=False, regex=True
        )
    )
    pagar["Dias_atraso"] = (-pagar["Dias_para_vencer"]).clip(lower=0)

    def faixa_pagamento(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido atÃ© 7 dias"
            if atraso <= 30:
                return "Vencido de 8 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A pagar em atÃ© 7 dias"
        if dias <= 15:
            return "A pagar de 8 a 15 dias"
        if dias <= 30:
            return "A pagar de 16 a 30 dias"
        return "A pagar acima de 30 dias"

    pagar["Faixa"] = pagar.apply(faixa_pagamento, axis=1)
    return pagar.sort_values(["Vencida", "Vencimento"], ascending=[False, True])

def total_movimentos_liquidados(movimentos):
    if not movimentos:
        return 0.0
    valores = pd.Series([
        item.get("valor_total") or item.get("valor") or 0
        for item in movimentos
    ])
    return float(numero_coluna(valores).sum())

def ciclo_meta_comercial(referencia):
    ref = pd.Timestamp(referencia).normalize()
    if ref.day >= 21:
        inicio = ref.replace(day=21)
        fim = (inicio + pd.DateOffset(months=1)).replace(day=20)
    else:
        fim = ref.replace(day=20)
        inicio = (fim - pd.DateOffset(months=1)).replace(day=21)
    return inicio, fim

def ciclo_comissao_fechado(referencia):
    ref = pd.Timestamp(referencia).normalize()
    if ref.day > 20:
        fim = ref.replace(day=20)
    else:
        fim = (ref - pd.DateOffset(months=1)).replace(day=20)
    inicio = (fim - pd.DateOffset(months=1)).replace(day=21)
    prazo_pagamento = fim + pd.offsets.MonthEnd(0)
    data_pagamento = (prazo_pagamento + pd.DateOffset(months=1)).replace(day=5)
    return inicio, fim, prazo_pagamento, data_pagamento

def calcular_resultado_financeiro(financeiro, contas_pagar, recebido_mes, pago_mes):
    receber = calcular_metricas_financeiras(financeiro)
    total_pagar = (
        float(contas_pagar["Valor"].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_vencido = (
        float(contas_pagar.loc[contas_pagar["Vencida"], "Valor"].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_7 = (
        float(contas_pagar.loc[
            (~contas_pagar["Vencida"]) &
            contas_pagar["Dias_para_vencer"].between(0, 7),
            "Valor"
        ].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_30 = (
        float(contas_pagar.loc[
            (~contas_pagar["Vencida"]) &
            contas_pagar["Dias_para_vencer"].between(0, 30),
            "Valor"
        ].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    receber_30 = receber["vence_7"] + receber["vence_15"] + receber["vence_30"]
    resultado_mes = float(recebido_mes) - float(pago_mes)
    margem_caixa = (
        resultado_mes / float(recebido_mes) * 100
        if recebido_mes else 0.0
    )
    return {
        **receber,
        "total_pagar": total_pagar,
        "pagar_vencido": pagar_vencido,
        "pagar_7": pagar_7,
        "pagar_30": pagar_30,
        "saldo_carteira": receber["total_aberto"] - total_pagar,
        "saldo_30_dias": receber_30 - pagar_30,
        "recebido_mes": float(recebido_mes),
        "pago_mes": float(pago_mes),
        "resultado_mes": resultado_mes,
        "margem_caixa": margem_caixa,
    }

def estrategia_financeira(metricas):
    resultado = metricas["resultado_mes"]
    saldo_30 = metricas["saldo_30_dias"]
    vencido_pct = metricas["percentual_vencido"]
    pagar_vencido = metricas["pagar_vencido"]
    dicas = []
    if resultado < 0:
        dicas.append(
            "O mÃªs apresenta prejuÃ­zo financeiro: pagamentos liquidados superam "
            "os recebimentos. Congele despesas nÃ£o essenciais e renegocie vencimentos."
        )
    elif resultado > 0:
        dicas.append(
            "O mÃªs apresenta lucro financeiro. Preserve uma parcela como reserva "
            "antes de ampliar compras, despesas ou retiradas."
        )
    else:
        dicas.append(
            "O resultado financeiro mensal estÃ¡ no ponto de equilÃ­brio. "
            "Evite novos compromissos fixos atÃ© formar margem de seguranÃ§a."
        )
    if saldo_30 < 0:
        dicas.append(
            f"HÃ¡ dÃ©ficit projetado de {fmt(abs(saldo_30))} para os prÃ³ximos 30 dias. "
            "Antecipe cobranÃ§as e negocie fornecedores antes dos vencimentos."
        )
    else:
        dicas.append(
            f"A projeÃ§Ã£o de 30 dias indica sobra de {fmt(saldo_30)} entre entradas "
            "e saÃ­das jÃ¡ registradas."
        )
    if vencido_pct >= 15:
        dicas.append(
            "A inadimplÃªncia estÃ¡ pressionando o caixa. Priorize cobranÃ§as por valor, "
            "idade da dÃ­vida e probabilidade de recuperaÃ§Ã£o."
        )
    if pagar_vencido > 0:
        dicas.append(
            f"Existem {fmt(pagar_vencido)} em contas a pagar vencidas; regularize "
            "primeiro obrigaÃ§Ãµes crÃ­ticas para operaÃ§Ã£o e crÃ©dito."
        )
    return dicas

def calcular_financeiro_real(dados, configuracao):
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if vendas.empty:
        return {}
    data_col = achar_coluna(vendas, ["data"])
    valor_col = achar_coluna(vendas, ["valor"])
    custo_col = achar_coluna(vendas, ["custo"])
    fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
    inicio_mes, fim_meta = ciclo_meta_comercial(fim)
    fim_realizado = min(fim, fim_meta)
    vendas_mes = vendas[
        (vendas[data_col] >= inicio_mes) & (vendas[data_col] <= fim_realizado)
    ].copy()
    receita = float(vendas_mes[valor_col].sum())
    custo = float(vendas_mes[custo_col].sum()) if custo_col else 0.0
    lucro_bruto = receita - custo
    impostos = receita * float(configuracao.get("impostos_pct", 0)) / 100
    folha = float(configuracao.get("folha_mensal", 0))
    despesas_fixas = float(configuracao.get("despesas_fixas", 0))
    outras_despesas = float(configuracao.get("outras_despesas", 0))
    lucro_operacional = (
        lucro_bruto - impostos - folha - despesas_fixas - outras_despesas
    )
    margem_bruta = lucro_bruto / receita * 100 if receita else 0
    margem_operacional = lucro_operacional / receita * 100 if receita else 0

    financeiro = dados.get("financeiro", pd.DataFrame())
    pagar = dados.get("contas_pagar", pd.DataFrame())
    base_caixa = calcular_resultado_financeiro(
        financeiro, pagar, dados.get("recebido_mes", 0), dados.get("pago_mes", 0)
    )
    saldo_inicial = float(configuracao.get("saldo_inicial", 0))
    caixa_projetado_30 = saldo_inicial + base_caixa["saldo_30_dias"]

    cenarios = {}
    for nome, fator_receber in (
        ("Conservador", 0.70), ("ProvÃ¡vel", 0.90), ("Otimista", 1.00)
    ):
        cenarios[nome] = (
            saldo_inicial +
            base_caixa["total_aberto"] * fator_receber -
            base_caixa["total_pagar"]
        )
    return {
        "receita_mes": receita,
        "custo_mes": custo,
        "lucro_bruto": lucro_bruto,
        "impostos_estimados": impostos,
        "folha": folha,
        "despesas_fixas": despesas_fixas,
        "outras_despesas": outras_despesas,
        "lucro_operacional": lucro_operacional,
        "margem_bruta": margem_bruta,
        "margem_operacional": margem_operacional,
        "saldo_inicial": saldo_inicial,
        "caixa_projetado_30": caixa_projetado_30,
        "cenarios": cenarios,
        "custos_disponiveis": bool(custo_col and vendas_mes[custo_col].gt(0).any()),
    }

def calcular_gestao_comercial(dados, configuracao):
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame()).copy()
    if vendas.empty:
        return {}, pd.DataFrame()
    data_col = achar_coluna(vendas, ["data"])
    valor_col = achar_coluna(vendas, ["valor"])
    custo_col = achar_coluna(vendas, ["custo"])
    vendedor_col = achar_coluna(vendas, ["vendedor"])
    fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
    if pd.isna(fim):
        fim = pd.Timestamp(date.today())
    inicio_mes, fim_meta = ciclo_meta_comercial(fim)
    inicio_mes = pd.Timestamp(inicio_mes).normalize()
    fim_meta = pd.Timestamp(fim_meta).normalize()
    fim = pd.Timestamp(fim).normalize()
    fim_realizado = min(fim, fim_meta)
    vendas_mes = vendas[
        (vendas[data_col] >= inicio_mes) & (vendas[data_col] <= fim_realizado)
    ].copy()
    if not vendedor_col:
        vendas_mes["Vendedor"] = "Sem vendedor"
        vendedor_col = "Vendedor"
    resumo = vendas_mes.groupby(vendedor_col).agg(
        Faturamento=(valor_col, "sum"),
        Vendas=(valor_col, "count"),
        Ticket_medio=(valor_col, "mean"),
    )
    if custo_col:
        custos = vendas_mes.groupby(vendedor_col)[custo_col].sum()
        resumo["Custo"] = resumo.index.map(custos).fillna(0)
    else:
        resumo["Custo"] = 0.0
    resumo["Margem"] = resumo["Faturamento"] - resumo["Custo"]
    resumo["Margem_pct"] = (
        resumo["Margem"] / resumo["Faturamento"].replace(0, pd.NA) * 100
    ).fillna(0)

    meta_geral = float(configuracao.get("meta_geral", 0))
    metas_vendedor = configuracao.get("metas_vendedor", {})
    resumo["Meta"] = [
        float(metas_vendedor.get(str(vendedor), 0))
        for vendedor in resumo.index
    ]
    resumo["Atingimento_pct"] = (
        resumo["Faturamento"] / resumo["Meta"].replace(0, pd.NA) * 100
    ).fillna(0)
    resumo["Distancia_meta"] = (resumo["Meta"] - resumo["Faturamento"]).clip(lower=0)

    dias_decorridos = max(1, int((fim_realizado - inicio_mes).days) + 1)
    dias_mes = max(1, int((fim_meta - inicio_mes).days) + 1)
    projecao = float(vendas_mes[valor_col].sum()) / dias_decorridos * dias_mes

    conversao = 0.0
    perdidos = 0
    idade_media_abertos = 0.0
    motivos_perda = pd.DataFrame()
    total_orc = len(orcamentos)
    if total_orc:
        status_col = achar_coluna(orcamentos, ["situacao", "status"])
        data_orc_col = achar_coluna(orcamentos, ["data"])
        status = orcamentos[status_col].astype(str).str.upper()
        convertidos = status.str.contains(
            "CONCRETIZ|FATURAD|VENDID|FECHAD|CONFIRMAD", na=False, regex=True
        ).sum()
        perdidos = status.str.contains(
            "PERDID|CANCEL|REPROV", na=False, regex=True
        ).sum()
        conversao = convertidos / total_orc * 100
        abertos = ~status.str.contains(
            "CONCRETIZ|FATURAD|VENDID|FECHAD|CONFIRMAD|PERDID|CANCEL|REPROV",
            na=False, regex=True
        )
        if data_orc_col and abertos.any():
            idade_media_abertos = float(
                (fim - pd.to_datetime(
                    orcamentos.loc[abertos, data_orc_col], errors="coerce"
                )).dt.days.mean()
            )
        notas_col = achar_coluna(orcamentos, ["observacoes internas", "observacoes"])
        if notas_col and perdidos:
            perdas = orcamentos[status.str.contains(
                "PERDID|CANCEL|REPROV", na=False, regex=True
            )].copy()
            perdas["Motivo informado"] = perdas[notas_col].astype(str).str.strip()
            perdas.loc[
                perdas["Motivo informado"].isin(["", "nan", "None"]),
                "Motivo informado"
            ] = "NÃ£o informado"
            motivos_perda = (
                perdas["Motivo informado"].value_counts()
                .head(10).rename_axis("Motivo").reset_index(name="Quantidade")
            )
    indicadores = {
        "meta_geral": meta_geral,
        "realizado": float(vendas_mes[valor_col].sum()),
        "projecao": projecao,
        "distancia_meta": max(0, meta_geral - float(vendas_mes[valor_col].sum())),
        "ciclo_meta_inicio": inicio_mes,
        "ciclo_meta_fim": fim_meta,
        "conversao_orcamentos": conversao,
        "orcamentos_total": total_orc,
        "orcamentos_perdidos": int(perdidos),
        "idade_media_abertos": idade_media_abertos,
        "motivos_perda": motivos_perda,
    }
    return indicadores, resumo.reset_index().rename(columns={vendedor_col: "Vendedor"})

def calcular_churn_avancado(dados):
    clientes = dados["clientes"].copy()
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if "Cliente ID" not in clientes.columns:
        clientes["Cliente ID"] = clientes["Cliente"].map(norm)
    churn = listar_clientes_churn(clientes)
    faturamento_total = float(clientes["faturamento"].sum())
    churn_ponderado = (
        float(churn["faturamento"].sum()) / faturamento_total * 100
        if faturamento_total else 0.0
    )
    migrando = clientes[
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] > clientes["intervalo"] * 1.2) &
        (clientes["dias_sem_comprar"] <= clientes["intervalo"] * 2)
    ].copy()

    recuperados = set()
    sazonais = set()
    tendencia = []
    if not vendas.empty:
        data_col = achar_coluna(vendas, ["data"])
        for chave, grupo in vendas.sort_values(data_col).groupby("_cliente_chave"):
            intervalos = grupo[data_col].diff().dt.days.dropna()
            if len(intervalos) >= 3:
                media = intervalos.mean()
                desvio = intervalos.std()
                if media > 0 and desvio / media > 0.65:
                    sazonais.add(str(chave))
                if any(intervalos.iloc[:-1] > media * 2):
                    recuperados.add(str(chave))
        fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
        for meses_atras in range(5, -1, -1):
            referencia = (fim - pd.DateOffset(months=meses_atras)) + pd.offsets.MonthEnd(0)
            base = vendas[vendas[data_col] <= referencia]
            conhecidos = 0
            churn_mes = 0
            for _chave, grupo in base.sort_values(data_col).groupby("_cliente_chave"):
                datas = grupo[data_col].dropna()
                if len(datas) < 2:
                    continue
                intervalo = datas.diff().dt.days.dropna().mean()
                if intervalo <= 0:
                    continue
                conhecidos += 1
                if (referencia - datas.max()).days > intervalo * 2:
                    churn_mes += 1
            tendencia.append({
                "MÃªs": referencia.strftime("%m/%Y"),
                "Churn %": churn_mes / conhecidos * 100 if conhecidos else 0.0
            })
    clientes["sazonal"] = clientes["Cliente ID"].astype(str).isin(sazonais)
    if "Cliente ID" not in churn.columns:
        churn["Cliente ID"] = churn["Cliente"].map(norm)
    churn["sazonal"] = churn["Cliente ID"].astype(str).isin(sazonais)
    return {
        "churn_ponderado": churn_ponderado,
        "migrando": migrando,
        "recuperados_historicos": len(recuperados),
        "taxa_recuperacao_historica": (
            len(recuperados) / max(1, int((clientes["intervalo"] > 0).sum())) * 100
        ),
        "sazonais": len(sazonais),
        "tendencia_mensal": pd.DataFrame(tendencia),
        "clientes_churn": churn,
        "clientes": clientes,
    }

def variacao_percentual(atual, anterior):
    if anterior in (0, None) or pd.isna(anterior):
        return None
    return (atual - anterior) / anterior * 100

def texto_variacao(valor):
    if valor is None or pd.isna(valor):
        return "Sem base anterior"
    sinal = "+" if valor >= 0 else ""
    return f"{sinal}{valor:.1f}%"

def classificar_cliente_retencao(row):
    intervalo = float(row.get("intervalo", 0) or 0)
    dias = float(row.get("dias_sem_comprar", 0) or 0)
    if intervalo <= 0:
        return "SAUDÃVEL"
    if dias > intervalo * 2:
        return "CHURN"
    if dias > intervalo:
        return "EM RISCO"
    return "SAUDÃVEL"

def classificar_status_em_data(datas, referencia):
    datas = pd.to_datetime(datas, errors="coerce").dropna().sort_values()
    datas = datas[datas <= referencia]
    if datas.empty:
        return None
    if len(datas) < 2:
        return "SAUDÃVEL"
    intervalo = datas.diff().dt.days.dropna().mean()
    if intervalo <= 0:
        return "SAUDÃVEL"
    dias_sem_comprar = (referencia - datas.max()).days
    if dias_sem_comprar > intervalo * 2:
        return "CHURN"
    if dias_sem_comprar > intervalo:
        return "EM RISCO"
    return "SAUDÃVEL"

@st.cache_data(show_spinner=False)
def calcular_indicadores_retencao_ceo(
    clientes, vendas, periodo_inicio, periodo_fim,
    custo_comercial, custo_marketing, custo_ferramentas
):
    vazio = {
        "clientes": pd.DataFrame(),
        "contagem_status": {"SAUDÃVEL": 0, "EM RISCO": 0, "CHURN": 0},
        "churn_financeiro_mensal": 0.0,
        "churn_financeiro_anual": 0.0,
        "carteira_risco_mensal": 0.0,
        "qtd_risco": 0,
        "potencial_recuperavel_mensal": 0.0,
        "potencial_recuperavel_anual": 0.0,
        "qtd_recuperaveis": 0,
        "cac_atual": 0.0,
        "cac_anterior": 0.0,
        "cac_variacao": None,
        "novos_clientes_atual": 0,
        "novos_clientes_anterior": 0,
        "taxa_recuperacao": 0.0,
        "historico": pd.DataFrame(),
    }
    if clientes is None or clientes.empty:
        return vazio

    clientes_calc = clientes.copy()
    clientes_calc["status_retencao"] = clientes_calc.apply(
        classificar_cliente_retencao, axis=1
    )
    contagem = clientes_calc["status_retencao"].value_counts().to_dict()
    for status in ("SAUDÃVEL", "EM RISCO", "CHURN"):
        contagem.setdefault(status, 0)

    churn = clientes_calc[clientes_calc["status_retencao"] == "CHURN"]
    risco = clientes_calc[clientes_calc["status_retencao"] == "EM RISCO"]
    recuperaveis = clientes_calc[
        clientes_calc["status_retencao"].isin(["EM RISCO", "CHURN"])
    ]
    churn_fin_mensal = float(churn["potencial_mensal"].sum())
    risco_mensal = float(risco["potencial_mensal"].sum())
    recuperavel_mensal = float(recuperaveis["potencial_mensal"].sum())

    total_custos = (
        float(custo_comercial or 0)
        + float(custo_marketing or 0)
        + float(custo_ferramentas or 0)
    )
    vendas_calc = vendas.copy() if vendas is not None else pd.DataFrame()
    historico = pd.DataFrame()
    cac_atual = 0.0
    cac_anterior = 0.0
    novos_atual = 0
    novos_anterior = 0
    taxa_recuperacao = 0.0

    if not vendas_calc.empty:
        data_col = achar_coluna(vendas_calc, ["data"])
        valor_col = achar_coluna(vendas_calc, ["valor"])
        chave_col = "_cliente_chave" if "_cliente_chave" in vendas_calc.columns else achar_coluna(vendas_calc, ["cliente id", "cliente"])
        if data_col and valor_col and chave_col:
            vendas_calc[data_col] = pd.to_datetime(vendas_calc[data_col], errors="coerce")
            vendas_calc[valor_col] = pd.to_numeric(vendas_calc[valor_col], errors="coerce").fillna(0)
            vendas_calc = vendas_calc.dropna(subset=[data_col]).copy()
            if not vendas_calc.empty:
                vendas_calc["_mes"] = vendas_calc[data_col].dt.to_period("M")
                primeiro_mes = vendas_calc.groupby(chave_col)[data_col].min().dt.to_period("M")
                meses = sorted(vendas_calc["_mes"].dropna().unique())
                linhas = []
                for mes in meses:
                    inicio_mes = mes.to_timestamp()
                    fim_mes = inicio_mes + pd.offsets.MonthEnd(0)
                    novos = int((primeiro_mes == mes).sum())
                    cac_mes = total_custos / novos if novos else 0.0
                    status_ref = {}
                    potencial_ref = {}
                    for cliente_id, grupo in vendas_calc.groupby(chave_col):
                        datas = grupo[data_col]
                        status = classificar_status_em_data(datas, fim_mes)
                        if status is None:
                            continue
                        status_ref[cliente_id] = status
                        ultimos_3m = grupo[
                            (grupo[data_col] >= fim_mes - pd.DateOffset(months=3))
                            & (grupo[data_col] <= fim_mes)
                        ]
                        potencial_ref[cliente_id] = float(ultimos_3m[valor_col].sum()) / 3
                    churn_mes = sum(
                        potencial_ref.get(cliente_id, 0.0)
                        for cliente_id, status in status_ref.items()
                        if status == "CHURN"
                    )
                    risco_mes = sum(
                        potencial_ref.get(cliente_id, 0.0)
                        for cliente_id, status in status_ref.items()
                        if status == "EM RISCO"
                    )
                    saudaveis = sum(1 for status in status_ref.values() if status == "SAUDÃVEL")
                    em_risco = sum(1 for status in status_ref.values() if status == "EM RISCO")
                    churn_qtd = sum(1 for status in status_ref.values() if status == "CHURN")

                    mes_anterior = mes - 1
                    fim_anterior = mes_anterior.to_timestamp() + pd.offsets.MonthEnd(0)
                    risco_anterior = set()
                    for cliente_id, grupo in vendas_calc.groupby(chave_col):
                        status_ant = classificar_status_em_data(grupo[data_col], fim_anterior)
                        if status_ant == "EM RISCO":
                            risco_anterior.add(cliente_id)
                    compras_mes = set(vendas_calc.loc[vendas_calc["_mes"] == mes, chave_col])
                    recuperados = len(risco_anterior & compras_mes)
                    taxa_mes = recuperados / len(risco_anterior) * 100 if risco_anterior else 0.0

                    linhas.append({
                        "MÃªs": mes.strftime("%m/%Y"),
                        "_mes": mes,
                        "CAC": cac_mes,
                        "Novos clientes": novos,
                        "Churn financeiro": churn_mes,
                        "Carteira em risco": risco_mes,
                        "SaudÃ¡veis": saudaveis,
                        "Em risco": em_risco,
                        "Churn": churn_qtd,
                        "Clientes em risco anterior": len(risco_anterior),
                        "Clientes recuperados": recuperados,
                        "Taxa de recuperaÃ§Ã£o": taxa_mes,
                    })
                historico = pd.DataFrame(linhas)
                if not historico.empty:
                    atual = historico.iloc[-1]
                    anterior = historico.iloc[-2] if len(historico) > 1 else None
                    cac_atual = float(atual["CAC"])
                    novos_atual = int(atual["Novos clientes"])
                    taxa_recuperacao = float(atual["Taxa de recuperaÃ§Ã£o"])
                    if anterior is not None:
                        cac_anterior = float(anterior["CAC"])
                        novos_anterior = int(anterior["Novos clientes"])
                inicio_periodo = pd.to_datetime(periodo_inicio, errors="coerce")
                fim_periodo = pd.to_datetime(periodo_fim, errors="coerce")
                if pd.notna(inicio_periodo) and pd.notna(fim_periodo):
                    primeira_compra = vendas_calc.groupby(chave_col)[data_col].min()
                    novos_periodo = int(
                        ((primeira_compra >= inicio_periodo) & (primeira_compra <= fim_periodo)).sum()
                    )
                    if novos_periodo:
                        cac_atual = total_custos / novos_periodo
                        novos_atual = novos_periodo
                    dias_periodo = max(1, int((fim_periodo - inicio_periodo).days) + 1)
                    inicio_anterior = inicio_periodo - pd.Timedelta(days=dias_periodo)
                    fim_anterior = inicio_periodo - pd.Timedelta(days=1)
                    novos_periodo_anterior = int(
                        ((primeira_compra >= inicio_anterior) & (primeira_compra <= fim_anterior)).sum()
                    )
                    if novos_periodo_anterior:
                        cac_anterior = total_custos / novos_periodo_anterior
                        novos_anterior = novos_periodo_anterior

    return {
        "clientes": clientes_calc,
        "contagem_status": contagem,
        "churn_financeiro_mensal": churn_fin_mensal,
        "churn_financeiro_anual": churn_fin_mensal * 12,
        "carteira_risco_mensal": risco_mensal,
        "qtd_risco": int(len(risco)),
        "potencial_recuperavel_mensal": recuperavel_mensal,
        "potencial_recuperavel_anual": recuperavel_mensal * 12,
        "qtd_recuperaveis": int(len(recuperaveis)),
        "cac_atual": cac_atual,
        "cac_anterior": cac_anterior,
        "cac_variacao": variacao_percentual(cac_atual, cac_anterior),
        "novos_clientes_atual": novos_atual,
        "novos_clientes_anterior": novos_anterior,
        "taxa_recuperacao": taxa_recuperacao,
        "historico": historico,
    }

def data_movimento_recebimento(item):
    for campo in (
        "data_pagamento", "data_recebimento", "data_liquidacao",
        "data_liquidaÃ§Ã£o", "data_baixa", "data"
    ):
        data = pd.to_datetime(item.get(campo), format="%Y-%m-%d", errors="coerce")
        if pd.notna(data):
            return data
    return pd.NaT

def chave_cliente_movimento(item):
    cliente_id = str(item.get("cliente_id") or item.get("Cliente ID") or "").strip()
    if cliente_id and cliente_id.lower() not in {"nan", "none"}:
        return cliente_id
    documento = documento_cliente_registro(item)
    if documento:
        return somente_digitos(documento)
    return norm(item.get("nome_cliente") or item.get("cliente") or item.get("destinado") or "")

def calcular_comissoes(dados, referencia=None):
    referencia = pd.Timestamp(referencia or date.today())
    inicio, fim, prazo_pagamento, data_pagamento = ciclo_comissao_fechado(referencia)
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if vendas.empty:
        return {
            "inicio": inicio, "fim": fim, "prazo": prazo_pagamento,
            "pagamento": data_pagamento, "itens": pd.DataFrame(),
            "pendentes": pd.DataFrame(), "resumo": pd.DataFrame(),
        }
    data_col = achar_coluna(vendas, ["data"])
    valor_col = achar_coluna(vendas, ["valor"])
    vendedor_col = achar_coluna(vendas, ["vendedor"])
    cliente_col = achar_coluna(vendas, ["cliente"])
    cliente_id_col = achar_coluna(vendas, ["cliente id"])
    if not data_col:
        return {
            "inicio": inicio, "fim": fim, "prazo": prazo_pagamento,
            "pagamento": data_pagamento, "itens": pd.DataFrame(),
            "pendentes": pd.DataFrame(), "resumo": pd.DataFrame(),
        }
    vendas[data_col] = pd.to_datetime(vendas[data_col], errors="coerce")
    vendas_ciclo = vendas[(vendas[data_col] >= inicio) & (vendas[data_col] <= fim)].copy()

    recebimentos = dados.get("recebimentos_liquidados", []) or []
    clientes_pagos = set()
    for rec in recebimentos:
        data_rec = data_movimento_recebimento(rec)
        if pd.notna(data_rec) and inicio <= data_rec <= prazo_pagamento:
            chave = chave_cliente_movimento(rec)
            if chave:
                clientes_pagos.add(chave)

    linhas = []
    pendentes = []
    for _, venda in vendas_ciclo.iterrows():
        cliente = str(venda.get(cliente_col, "Cliente sem nome")) if cliente_col else "Cliente sem nome"
        cliente_id = str(venda.get(cliente_id_col, "")).strip() if cliente_id_col else ""
        documento = str(venda.get("Documento", "") or "")
        chaves_venda = {
            cliente_id,
            somente_digitos(documento),
            norm(cliente),
        }
        pago = bool(clientes_pagos & {c for c in chaves_venda if c})
        itens = venda.get("_itens_comissao", [])
        if not isinstance(itens, list):
            itens = []
        if not itens:
            total_venda = float(venda.get(valor_col, 0) or 0) if valor_col else 0.0
            pendentes.append({
                "Vendedor": venda.get(vendedor_col, "Sem vendedor") if vendedor_col else "Sem vendedor",
                "Cliente": cliente,
                "Data venda": venda.get(data_col),
                "Produto": "Venda sem itens detalhados",
                "Valor": total_venda,
                "Tipo": "",
                "Motivo": "Sem campo Tipo/percentual nos itens",
            })
            continue
        for item in itens:
            pct = item.get("percentual")
            base = float(item.get("valor_total", 0) or 0)
            linha_base = {
                "Vendedor": venda.get(vendedor_col, "Sem vendedor") if vendedor_col else "Sem vendedor",
                "Cliente": cliente,
                "Data venda": venda.get(data_col),
                "Produto": item.get("produto", ""),
                "Valor": base,
                "Tipo": item.get("tipo", ""),
            }
            if pct is None:
                pendentes.append({**linha_base, "Motivo": "Percentual nao cadastrado no Tipo", "Tipo detectado": str(item.get("tipo", ""))})
                continue
            comissao = base * float(pct) / 100
            linhas.append({
                **linha_base,
                "Percentual": float(pct),
                "ComissÃ£o": comissao,
                "Pago no prazo": pago,
                "Status": "A pagar no dia 5" if pago else (
                    "Aguardando pagamento do cliente" if referencia <= prazo_pagamento else "NÃ£o pago no prazo"
                ),
            })

    itens_df = pd.DataFrame(linhas)
    pendentes_df = pd.DataFrame(pendentes)
    if not itens_df.empty:
        resumo = itens_df[itens_df["Pago no prazo"]].groupby("Vendedor").agg(
            Vendas=("Valor", "sum"),
            Comissao=("ComissÃ£o", "sum"),
            Itens=("Produto", "count"),
        ).reset_index()
    else:
        resumo = pd.DataFrame(columns=["Vendedor", "Vendas", "Comissao", "Itens"])
    return {
        "inicio": inicio,
        "fim": fim,
        "prazo": prazo_pagamento,
        "pagamento": data_pagamento,
        "itens": itens_df,
        "pendentes": pendentes_df,
        "resumo": resumo,
    }

def processar_dataframes(vendas, orc, contas):
    hoje = datetime.now()

    cv_cli = achar_coluna(vendas, ["cliente"])
    cv_cli_id = achar_coluna(vendas, ["cliente id"])
    cv_data = achar_coluna(vendas, ["data"])
    cv_valor = achar_coluna(vendas, ["valor"])
    cv_custo = achar_coluna(vendas, ["custo"])
    cv_status = achar_coluna(vendas, ["situacao", "status"])
    cv_vendedor = achar_coluna(vendas, ["vendedor"])
    cv_vendedor_id = achar_coluna(vendas, ["vendedor id"])
    cv_documento = achar_coluna(vendas, ["documento", "cnpj", "cpf"])
    cv_telefone = achar_coluna(vendas, ["telefone", "celular", "whatsapp", "fone"])
    cv_item = achar_coluna(vendas, ["produto", "servico", "serviÃ§o", "item", "descricao", "descriÃ§Ã£o"])
    co_num = achar_coluna(orc, ["nÂº", "nÂ°", "numero", "nÃºmero"])
    co_cli = achar_coluna(orc, ["cliente"])
    co_cli_id = achar_coluna(orc, ["cliente id"])
    co_data = achar_coluna(orc, ["data"])
    co_status = achar_coluna(orc, ["situaÃ§Ã£o", "situacao", "status"])
    co_valor = achar_coluna(orc, ["valor"])
    co_item = achar_coluna(orc, ["produto", "servico", "serviÃ§o", "item", "descricao", "descriÃ§Ã£o"])
    cc_cli = achar_coluna(contas, ["cliente", "destinado"])
    cc_cli_id = achar_coluna(contas, ["cliente id"])
    cc_venc = achar_coluna(contas, ["vencimento"])
    cc_status = achar_coluna(contas, ["situaÃ§Ã£o", "situacao", "status"])
    cc_valor = achar_coluna(contas, ["valor total", "valor"])

    faltando = []
    for nome, col in {
        "Cliente vendas": cv_cli,
        "Data vendas": cv_data,
        "Valor vendas": cv_valor,
        "NÂº orÃ§amento": co_num,
        "Cliente orÃ§amento": co_cli,
        "Data orÃ§amento": co_data,
        "Status orÃ§amento": co_status,
        "Cliente contas": cc_cli,
        "Valor contas": cc_valor,
    }.items():
        if col is None:
            faltando.append(nome)
    if faltando:
        raise Exception("Colunas nÃ£o encontradas: " + ", ".join(faltando))

    vendas[cv_data] = data_coluna(vendas[cv_data])
    vendas[cv_valor] = numero_coluna(vendas[cv_valor])
    if cv_custo:
        vendas[cv_custo] = numero_coluna(vendas[cv_custo])
    vendas = vendas.dropna(subset=[cv_cli, cv_data])
    vendas["_cliente_chave"] = (
        vendas[cv_cli_id].astype(str).str.strip()
        if cv_cli_id
        else vendas[cv_cli].map(norm)
    )
    vendas.loc[
        vendas["_cliente_chave"].isin(["", "none", "nan"]),
        "_cliente_chave"
    ] = vendas.loc[
        vendas["_cliente_chave"].isin(["", "none", "nan"]), cv_cli
    ].map(norm)
    vendas["_cliente_nome"] = vendas[cv_cli].astype(str).str.strip()
    if "_itens_texto" not in vendas.columns:
        vendas["_itens_texto"] = (
            vendas[cv_item].astype(str).str.strip()
            if cv_item else ""
        )

    vendas_canceladas = pd.DataFrame()
    if cv_status:
        cancelada = vendas[cv_status].astype(str).str.upper().str.contains(
            "CANCEL|DEVOL|ESTORN|REPROV|PERDID", na=False, regex=True
        )
        vendas_canceladas = vendas[cancelada].copy()
        vendas = vendas[~cancelada].copy()

    orc[co_data] = data_coluna(orc[co_data])
    if co_valor:
        orc[co_valor] = numero_coluna(orc[co_valor])
    orc["_cliente_chave"] = (
        orc[co_cli_id].astype(str).str.strip()
        if co_cli_id
        else orc[co_cli].map(norm)
    )
    orc.loc[
        orc["_cliente_chave"].isin(["", "none", "nan"]),
        "_cliente_chave"
    ] = orc.loc[
        orc["_cliente_chave"].isin(["", "none", "nan"]), co_cli
    ].map(norm)
    if "_itens_texto" not in orc.columns:
        orc["_itens_texto"] = (
            orc[co_item].astype(str).str.strip()
            if co_item else ""
        )

    contas[cc_valor] = numero_coluna(contas[cc_valor])
    if cc_venc:
        contas[cc_venc] = data_coluna(contas[cc_venc])
    financeiro = preparar_financeiro(
        contas, cc_cli, cc_venc, cc_valor, cc_status
    )

    contas["_cliente_chave"] = (
        contas[cc_cli_id].astype(str).str.strip()
        if cc_cli_id
        else contas[cc_cli].map(norm)
    )
    contas.loc[
        contas["_cliente_chave"].isin(["", "none", "nan"]),
        "_cliente_chave"
    ] = contas.loc[
        contas["_cliente_chave"].isin(["", "none", "nan"]), cc_cli
    ].map(norm)

    clientes = vendas.groupby("_cliente_chave").agg({
        "_cliente_nome": "last",
        cv_data: ["max", "count"],
        cv_valor: "sum"
    })
    clientes.columns = ["Cliente", "ultima_compra", "qtd_compras", "faturamento"]
    clientes = clientes.reset_index().rename(columns={"_cliente_chave": "Cliente ID"})

    vendas_recentes = vendas.sort_values(cv_data).drop_duplicates(
        "_cliente_chave", keep="last"
    ).set_index("_cliente_chave")
    if cv_vendedor:
        clientes["Vendedor"] = clientes["Cliente ID"].map(
            vendas_recentes[cv_vendedor]
        ).fillna("Sem vendedor")
    else:
        clientes["Vendedor"] = "Sem vendedor"
    if cv_vendedor_id:
        clientes["Vendedor ID"] = clientes["Cliente ID"].map(
            vendas_recentes[cv_vendedor_id]
        ).fillna("")
    else:
        clientes["Vendedor ID"] = ""
    if cv_documento:
        clientes["Documento"] = clientes["Cliente ID"].map(
            vendas_recentes[cv_documento]
        ).fillna("")
    else:
        clientes["Documento"] = ""
    if cv_telefone:
        clientes["Telefone"] = clientes["Cliente ID"].map(
            vendas_recentes[cv_telefone]
        ).fillna("")
    else:
        clientes["Telefone"] = ""

    intervalo = vendas.sort_values(cv_data).groupby("_cliente_chave")[cv_data].apply(
        lambda x: x.diff().mean().days if len(x.dropna()) > 1 else 0
    )

    clientes["intervalo"] = clientes["Cliente ID"].map(intervalo).fillna(0)
    clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days
    clientes["ticket_medio"] = (
        clientes["faturamento"] / clientes["qtd_compras"]
    ).replace([float("inf"), float("-inf")], 0).fillna(0)

    data_limite_3m = hoje - pd.DateOffset(months=3)
    vendas_3m = vendas[vendas[cv_data] >= data_limite_3m].copy()
    potencial_3m = vendas_3m.groupby("_cliente_chave")[cv_valor].sum() / 3
    clientes["potencial_mensal"] = clientes["Cliente ID"].map(potencial_3m).fillna(0)
    itens_comprados = agregar_itens_cliente(vendas, "_cliente_chave", "_itens_texto")
    clientes["itens_comprados"] = clientes["Cliente ID"].map(itens_comprados).apply(
        lambda x: x if isinstance(x, list) else []
    )
    itens_orcados = agregar_itens_cliente(orc, "_cliente_chave", "_itens_texto")
    clientes["itens_orcados"] = clientes["Cliente ID"].map(itens_orcados).apply(
        lambda x: x if isinstance(x, list) else []
    )

    orcamentos_todos = orc.copy()
    orc_aberto = orc.copy()
    orc_aberto = orc_aberto[
        ~orc_aberto[co_status].apply(status_fechado_orcamento)
    ]
    orc_aberto = orc_aberto[
        orc_aberto[co_data] >= (hoje - pd.Timedelta(days=90))
    ].copy()

    orc_aberto["dias_no_sistema"] = (hoje - orc_aberto[co_data]).dt.days
    orc_aberto["acao_recomendada_orcamento"] = orc_aberto["dias_no_sistema"].apply(status_orcamento)

    orc_count = orc_aberto.groupby("_cliente_chave")[co_num].count()
    clientes["orcamentos_em_aberto"] = clientes["Cliente ID"].map(orc_count).fillna(0)

    orc_nums = orc_aberto.groupby("_cliente_chave")[co_num].apply(lambda x: list(x.astype(str)))
    clientes["numeros_orcamentos"] = clientes["Cliente ID"].map(orc_nums).apply(lambda x: x if isinstance(x, list) else [])

    status_descartado_movimento = orc[co_status].astype(str).str.upper().str.contains(
        "CANCEL|PERDID|RECUSAD|REPROV", na=False, regex=True
    )
    orc_movimento = orc[
        (~status_descartado_movimento)
        & orc[co_data].notna()
        & (orc[co_data] >= hoje - pd.Timedelta(days=7))
    ].copy()
    if not orc_movimento.empty:
        ult_orc = orc_movimento.groupby("_cliente_chave")[co_data].max()
        clientes["ultimo_movimento_comercial"] = clientes["Cliente ID"].map(ult_orc)
        clientes["ultimo_movimento_comercial"] = clientes[
            ["ultima_compra", "ultimo_movimento_comercial"]
        ].max(axis=1)
        clientes["dias_sem_comprar"] = (
            hoje - clientes["ultimo_movimento_comercial"]
        ).dt.days.fillna(clientes["dias_sem_comprar"]).clip(lower=0)
    else:
        clientes["ultimo_movimento_comercial"] = clientes["ultima_compra"]

    if cc_venc:
        vencidas_por_data = contas[cc_venc].notna() & (contas[cc_venc] < hoje)
    else:
        vencidas_por_data = pd.Series(False, index=contas.index)
    if cc_status:
        status_atrasado = contas[cc_status].astype(str).str.upper().str.contains(
            "ATRASADO|VENCIDO", na=False, regex=True
        )
        status_liquidado = contas[cc_status].apply(status_liquidado_financeiro)
    else:
        status_atrasado = pd.Series(False, index=contas.index)
        status_liquidado = pd.Series(False, index=contas.index)
    contas_atraso = contas[
        (vencidas_por_data | status_atrasado) & (~status_liquidado)
    ].copy()

    if cc_venc and not contas_atraso.empty:
        contas_atraso["dias_atraso"] = (hoje - contas_atraso[cc_venc]).dt.days.clip(lower=0)
        media_atraso = contas_atraso.groupby(cc_cli)["dias_atraso"].mean()
    else:
        media_atraso = pd.Series(dtype=float)

    inad = contas_atraso.groupby("_cliente_chave")[cc_valor].sum() if not contas_atraso.empty else pd.Series(dtype=float)

    if not contas_atraso.empty and "dias_atraso" in contas_atraso:
        media_atraso = contas_atraso.groupby("_cliente_chave")["dias_atraso"].mean()
    clientes["inadimplencia"] = clientes["Cliente ID"].map(inad).fillna(0)
    clientes["media_dias_atraso"] = clientes["Cliente ID"].map(media_atraso).fillna(0)
    clientes["score_risco"] = clientes["media_dias_atraso"].apply(score_risco)
    clientes["risco_inadimplencia"] = clientes["score_risco"].apply(descricao_score)

    clientes["temperatura"] = clientes.apply(lambda x: temperatura_cliente(x["dias_sem_comprar"], x["intervalo"]), axis=1)

    limite_estrategico = clientes["faturamento"].quantile(0.90)
    clientes["cliente_estrategico"] = clientes["faturamento"] >= limite_estrategico

    clientes["potencial_recuperavel"] = clientes.apply(
        lambda x: x["potencial_mensal"] if x["temperatura"] in ["ðŸ”´ ATRASADO NA RECOMPRA", "âš« CLIENTE INATIVO"] else 0,
        axis=1
    )

    clientes["acao_ia"] = clientes.apply(
        lambda x: sugestao_ia(
            x["dias_sem_comprar"],
            x["intervalo"],
            x["orcamentos_em_aberto"],
            x["inadimplencia"],
            x["potencial_mensal"]
        ),
        axis=1
    )

    clientes["score_comercial"] = clientes.apply(score_comercial, axis=1)

    nomes_duplicados = (
        vendas.groupby(vendas["_cliente_nome"].map(norm))["_cliente_chave"]
        .nunique()
    )
    nomes_duplicados = nomes_duplicados[nomes_duplicados > 1]
    qualidade = {
        "vendas_canceladas": len(vendas_canceladas),
        "vendas_sem_cliente_id": int(
            vendas[cv_cli_id].isna().sum() if cv_cli_id else len(vendas)
        ),
        "clientes_nomes_duplicados": int(len(nomes_duplicados)),
        "vendas_sem_custo": int(
            vendas[cv_custo].le(0).sum() if cv_custo else len(vendas)
        ),
        "vendas_sem_vendedor": int(
            vendas[achar_coluna(vendas, ["vendedor"])].astype(str)
            .str.strip().isin(["", "Sem vendedor", "nan"]).sum()
            if achar_coluna(vendas, ["vendedor"]) else len(vendas)
        ),
    }

    return {
        "clientes": clientes,
        "orc_aberto": orc_aberto,
        "orcamentos_todos": orcamentos_todos,
        "co_num": co_num,
        "co_cli": co_cli,
        "co_data": co_data,
        "co_valor": co_valor,
        "financeiro": financeiro,
        "vendas_validas": vendas,
        "qualidade_dados": qualidade,
        "periodo_inicio": vendas[cv_data].min(),
        "periodo_fim": vendas[cv_data].max(),
    }

def processar_dados(vendas_file, orc_file, contas_file):
    vendas = carregar_excel(vendas_file, [["cliente"], ["data"], ["valor"]])
    orc = carregar_excel(
        orc_file,
        [["nÂº", "nÂ°", "numero", "nÃºmero"], ["cliente"], ["data"], ["situaÃ§Ã£o", "status"]]
    )
    contas = carregar_excel(
        contas_file,
        [["cliente", "destinado"], ["vencimento"], ["valor"], ["situaÃ§Ã£o", "status"]]
    )
    dados = processar_dataframes(vendas, orc, contas)
    dados["origem"] = "excel"
    dados["resultado_financeiro_disponivel"] = False
    return dados

def api_para_dataframes(vendas_api, orcamentos_api, recebimentos_api, vendedor_id=None):
    vendas_api = deduplicar_registros(vendas_api)
    orcamentos_api = deduplicar_registros(orcamentos_api)
    recebimentos_api = deduplicar_registros(recebimentos_api)
    if vendedor_id:
        vendas_api = [
            item for item in vendas_api
            if str(item.get("vendedor_id") or "") == str(vendedor_id)
        ]
        orcamentos_api = [
            item for item in orcamentos_api
            if str(item.get("vendedor_id") or "") == str(vendedor_id)
        ]

    vendas = pd.DataFrame([{
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Cliente ID": item.get("cliente_id"),
        "Documento": documento_cliente_registro(item),
        "Telefone": (
            item.get("celular") or item.get("telefone") or item.get("fone")
            or item.get("cliente_celular") or item.get("cliente_telefone") or ""
        ),
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Valor": item.get("valor_total") or 0,
        "Custo": custo_total_venda(item),
        "Situacao": item.get("nome_situacao") or "",
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "Observacoes": item.get("observacoes") or "",
        "Observacoes internas": item.get("observacoes_interna") or "",
        "_itens_texto": extrair_itens_registro(item),
        "_itens_comissao": extrair_itens_comissao(item),
        "Vendedor ID": item.get("vendedor_id"),
        "_venda_id": item.get("id"),
    } for item in vendas_api])

    orcamentos = pd.DataFrame([{
        "Numero": item.get("codigo") or item.get("id"),
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Cliente ID": item.get("cliente_id"),
        "Documento": documento_cliente_registro(item),
        "Telefone": (
            item.get("celular") or item.get("telefone") or item.get("fone")
            or item.get("cliente_celular") or item.get("cliente_telefone") or ""
        ),
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Situacao": item.get("nome_situacao") or "",
        "Valor": item.get("valor_total") or 0,
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "_itens_texto": extrair_itens_registro(item),
        "_orcamento_id": item.get("id"),
        "_observacoes_interna": item.get("observacoes_interna") or "",
    } for item in orcamentos_api])

    contas = pd.DataFrame([{
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Cliente ID": item.get("cliente_id"),
        "Documento": documento_cliente_registro(item),
        "Vencimento": pd.to_datetime(
            item.get("data_vencimento"), format="%Y-%m-%d", errors="coerce"
        ),
        "Valor Total": item.get("valor_total") or item.get("valor") or 0,
        "Situacao": item.get("_status_financeiro") or "EM ABERTO",
        "Juros": item.get("juros") or 0,
        "Desconto": item.get("desconto") or 0,
        "Forma Pagamento": item.get("nome_forma_pagamento") or "",
        "Loja": item.get("nome_loja") or "",
        "_recebimento_id": item.get("id"),
    } for item in recebimentos_api])

    if vendas.empty:
        vendas = pd.DataFrame(columns=[
            "Cliente", "Cliente ID", "Data", "Valor", "Custo", "Situacao",
            "Vendedor", "Vendedor ID", "Documento", "Telefone", "_itens_texto", "_itens_comissao", "_venda_id"
        ])
    if orcamentos.empty:
        orcamentos = pd.DataFrame(columns=[
            "Numero", "Cliente", "Cliente ID", "Documento", "Telefone", "Data", "Situacao", "Valor", "Vendedor",
            "Observacoes", "Observacoes internas",
            "_itens_texto", "_orcamento_id", "_observacoes_interna"
        ])
    if contas.empty:
        contas = pd.DataFrame(columns=[
            "Cliente", "Cliente ID", "Documento", "Vencimento", "Valor Total", "Situacao", "Juros",
            "Desconto", "Forma Pagamento", "Loja", "_recebimento_id"
        ])
    return vendas, orcamentos, contas

def processar_api(
    api, inicio, fim, loja_id, vendedor_id=None, vendedor_nome="Todos",
    configuracao=None
):
    vendas_api = api.sales(inicio, fim, loja_id)
    orcamentos_api = api.budgets(inicio, fim, loja_id)
    recebimentos_api = api.open_receivables(loja_id)
    pagamentos_api = api.open_payables(loja_id)
    inicio_mes = fim.replace(day=1)
    recebidos_mes = api.settled_movements(
        "/recebimentos", inicio_mes, fim, loja_id
    )
    pagos_mes = api.settled_movements(
        "/pagamentos", inicio_mes, fim, loja_id
    )
    vendas, orcamentos, contas = api_para_dataframes(
        vendas_api, orcamentos_api, recebimentos_api, vendedor_id
    )
    if vendas.empty:
        raise RuntimeError("Nenhuma venda foi encontrada para os filtros selecionados.")

    dados = processar_dataframes(vendas, orcamentos, contas)
    contas_pagar = preparar_contas_pagar(pagamentos_api)
    recebido_mes = total_movimentos_liquidados(recebidos_mes)
    pago_mes = total_movimentos_liquidados(pagos_mes)
    dados.update({
        "origem": "api",
        "loja_id": str(loja_id),
        "vendedor_id": str(vendedor_id or ""),
        "vendedor_nome": vendedor_nome,
        "atualizado_em": datetime.now(),
        "contas_pagar": contas_pagar,
        "recebimentos_liquidados": recebidos_mes,
        "recebido_mes": recebido_mes,
        "pago_mes": pago_mes,
        "mes_resultado": fim.strftime("%m/%Y"),
        "resultado_financeiro_disponivel": True,
        "configuracao": configuracao or {},
    })
    return dados

def contato_realizado_hoje(cliente_id, cliente):
    hoje = date.today().strftime("%d/%m/%Y")
    return any(
        str(registro.get("data", "")).strip() == hoje
        and cliente_corresponde(registro, cliente_id, cliente)
        for registro in st.session_state.contatos_realizados
    ) or cliente in st.session_state.clientes_ligados

def contato_realizado_periodo(cliente_id, cliente, dias=7):
    limite = pd.Timestamp(date.today() - timedelta(days=dias - 1)).normalize()
    for registro in st.session_state.contatos_realizados:
        if not cliente_corresponde(registro, cliente_id, cliente):
            continue
        data_contato = pd.to_datetime(
            registro.get("data", ""), dayfirst=True, errors="coerce"
        )
        if pd.notna(data_contato) and data_contato.normalize() >= limite:
            return True
    return cliente in st.session_state.clientes_ligados

def retornos_pendentes_cliente(cliente_id, cliente):
    hoje = pd.Timestamp(date.today())
    pendentes = []
    for registro in st.session_state.retornos_programados:
        data_retorno = pd.to_datetime(
            registro.get("data_retorno"), dayfirst=True, errors="coerce"
        )
        if (
            str(registro.get("status", "")).strip().lower() == "pendente"
            and cliente_corresponde(registro, cliente_id, cliente)
            and pd.notna(data_retorno)
            and data_retorno.normalize() <= hoje
        ):
            pendentes.append(registro)
    return pendentes

def enriquecer_regras_prioridade(clientes, orc_aberto):
    base = clientes.copy()
    co_cli = achar_coluna(orc_aberto, ["cliente"])
    co_cli_id = achar_coluna(orc_aberto, ["cliente id"])
    if orc_aberto.empty:
        contagens = {}
    else:
        orcs = orc_aberto.copy()
        if co_cli_id:
            ids = orcs[co_cli_id].astype(str).str.strip()
            ids_invalidos = ids.str.lower().isin({"", "nan", "none"})
            orcs["_cliente_chave_prioridade"] = ids.where(
                ~ids_invalidos, orcs[co_cli].map(norm)
            )
        else:
            orcs["_cliente_chave_prioridade"] = orcs[co_cli].map(norm)
        contagens = {
            chave: {
                "orc_ligar": int((grupo["dias_no_sistema"] == 2).sum()),
                "orc_urgente": int((grupo["dias_no_sistema"] == 3).sum()),
                "orc_risco": int((grupo["dias_no_sistema"] >= 4).sum()),
            }
            for chave, grupo in orcs.groupby("_cliente_chave_prioridade")
        }

    regras = []
    for _, row in base.iterrows():
        cliente_id = str(row.get("Cliente ID", "")).strip()
        chave = (
            cliente_id
            if cliente_id and cliente_id.lower() not in {"nan", "none"}
            else norm(row["Cliente"])
        )
        orc = contagens.get(chave, {})
        retornos = retornos_pendentes_cliente(cliente_id, row["Cliente"])
        tem_orcamento_aberto = (
            orc.get("orc_ligar", 0)
            + orc.get("orc_urgente", 0)
            + orc.get("orc_risco", 0)
            + int(float(row.get("orcamentos_em_aberto", 0) or 0))
        ) > 0
        proximo_recompra = bool(
            row["intervalo"] > 0
            and row["dias_sem_comprar"] >= row["intervalo"] * 0.9
            and not tem_orcamento_aberto
        )
        motivos = []
        if retornos:
            motivos.append("Retorno programado")
        if orc.get("orc_risco", 0):
            motivos.append("OrÃ§amento em risco de perda")
        elif orc.get("orc_urgente", 0):
            motivos.append("OrÃ§amento urgente")
        elif orc.get("orc_ligar", 0):
            motivos.append("OrÃ§amento: ligar hoje")
        if proximo_recompra:
            motivos.append("PrÃ³ximo da recompra")
        if row["intervalo"] > 0 and row["dias_sem_comprar"] > row["intervalo"] * 1.2:
            motivos.append("Ciclo de compra vencido")

        regras.append({
            "orc_ligar": orc.get("orc_ligar", 0),
            "orc_urgente": orc.get("orc_urgente", 0),
            "orc_risco": orc.get("orc_risco", 0),
            "retornos_hoje": len(retornos),
            "proximo_recompra": proximo_recompra,
            "ja_ligou_hoje": contato_realizado_hoje(cliente_id, row["Cliente"]),
            "contato_recente": contato_realizado_periodo(cliente_id, row["Cliente"], 7),
            "motivo_prioridade": " | ".join(motivos),
        })
    regras_df = pd.DataFrame(regras, index=base.index)
    for coluna in regras_df:
        base[coluna] = regras_df[coluna]
    base["score_prioridade_dia"] = (
        base["score_comercial"]
        + base["retornos_hoje"] * 100
        + base["orc_risco"] * 60
        + base["orc_urgente"] * 45
        + base["orc_ligar"] * 30
        + base["proximo_recompra"].astype(int) * 15
    )
    return base

def montar_prioridade(clientes):
    if "contato_recente" not in clientes.columns:
        clientes = clientes.copy()
        clientes["contato_recente"] = False
    elegivel = (
        clientes["retornos_hoje"].gt(0)
        | clientes["orc_ligar"].gt(0)
        | clientes["orc_urgente"].gt(0)
        | clientes["orc_risco"].gt(0)
        | clientes["proximo_recompra"]
    )
    return clientes[
        elegivel & (~clientes["contato_recente"])
    ].sort_values("score_prioridade_dia", ascending=False)

def montar_resumo(clientes):
    if "contato_recente" not in clientes.columns:
        clientes = clientes.copy()
        clientes["contato_recente"] = False
    temperaturas = clientes["temperatura"].isin([
        "ðŸŸ¢ QUENTE", "ðŸŸ¡ ATENÃ‡ÃƒO", "ðŸ”´ ATRASADO NA RECOMPRA", "âš« CLIENTE INATIVO"
    ])
    regras = (
        clientes["retornos_hoje"].gt(0)
        | clientes["orc_ligar"].gt(0)
        | clientes["orc_urgente"].gt(0)
        | clientes["orc_risco"].gt(0)
    )
    return clientes[
        (temperaturas | regras) & (~clientes["contato_recente"])
    ].sort_values("score_prioridade_dia", ascending=False)

def montar_resumo_diario(clientes):
    colunas = [
        "Vendedor", "Clientes para ligar", "Orcamentos sem retorno",
        "Proximos da recompra", "Retornos hoje", "Risco de perda"
    ]
    if clientes.empty:
        return pd.DataFrame(columns=colunas)

    if "contato_recente" not in clientes.columns:
        clientes = clientes.copy()
        clientes["contato_recente"] = False
    base = clientes[~clientes["contato_recente"]].copy()
    if "Vendedor" not in base.columns:
        base["Vendedor"] = "Sem vendedor"
    base["Vendedor"] = (
        base["Vendedor"].fillna("Sem vendedor").astype(str).str.strip()
        .replace({"": "Sem vendedor", "nan": "Sem vendedor"})
    )
    base["_cliente_para_ligar"] = (
        base["retornos_hoje"].gt(0)
        | base["orc_ligar"].gt(0)
        | base["orc_urgente"].gt(0)
        | base["orc_risco"].gt(0)
        | base["proximo_recompra"]
    ).astype(int)
    base["_orcamentos_sem_retorno"] = (
        base["orc_ligar"] + base["orc_urgente"] + base["orc_risco"]
    )
    resumo = base.groupby("Vendedor", as_index=False).agg(
        **{
            "Clientes para ligar": ("_cliente_para_ligar", "sum"),
            "Orcamentos sem retorno": ("_orcamentos_sem_retorno", "sum"),
            "Proximos da recompra": ("proximo_recompra", "sum"),
            "Retornos hoje": ("retornos_hoje", "sum"),
            "Risco de perda": ("orc_risco", "sum"),
        }
    )
    return resumo.sort_values(
        ["Clientes para ligar", "Risco de perda"], ascending=False
    )

def salvar_resumo_diario(resumo):
    if resumo.empty:
        return
    ws = garantir_abas_crm()["ResumoDiario"]
    registros = ws.get_all_records()
    hoje = date.today().strftime("%d/%m/%Y")
    gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    linhas_existentes = {
        (str(r.get("data", "")).strip(), str(r.get("vendedor", "")).strip()): i
        for i, r in enumerate(registros, start=2)
    }
    for _, row in resumo.iterrows():
        valores = [
            hoje, row["Vendedor"], int(row["Clientes para ligar"]),
            int(row["Orcamentos sem retorno"]),
            int(row["Proximos da recompra"]), int(row["Retornos hoje"]),
            int(row["Risco de perda"]), gerado_em
        ]
        linha = linhas_existentes.get((hoje, str(row["Vendedor"]).strip()))
        if linha:
            ws.update(f"A{linha}:H{linha}", [valores])
        else:
            ws.append_row(valores)

def calcular_churn(clientes):
    clientes_com_ciclo = clientes[clientes["intervalo"] > 0]
    if clientes_com_ciclo.empty:
        return 0.0, 0, 0

    filtro_churn = clientes_com_ciclo["dias_sem_comprar"] > clientes_com_ciclo["intervalo"] * 2
    if "contato_recente" in clientes_com_ciclo.columns:
        filtro_churn = filtro_churn & (~clientes_com_ciclo["contato_recente"].fillna(False))
    clientes_churn = clientes_com_ciclo[filtro_churn]
    taxa = len(clientes_churn) / len(clientes_com_ciclo) * 100
    return taxa, len(clientes_churn), len(clientes_com_ciclo)

def listar_clientes_churn(clientes):
    filtro = (
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] > clientes["intervalo"] * 2)
    )
    if "contato_recente" in clientes.columns:
        filtro = filtro & (~clientes["contato_recente"].fillna(False))
    churn = clientes[filtro].copy()
    churn["limite_churn_dias"] = (churn["intervalo"] * 2).round().astype(int)
    churn["dias_alem_limite"] = (
        churn["dias_sem_comprar"] - churn["limite_churn_dias"]
    ).clip(lower=0).astype(int)
    return churn.sort_values(
        ["potencial_mensal", "dias_alem_limite"],
        ascending=[False, False]
    )

def chave_widget(valor):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(valor)).strip("_") or "sem_id"

def identificador_cliente(row, fallback=""):
    cliente_id = str(row.get("Cliente ID", "")).strip()
    if cliente_id and cliente_id.lower() not in {"nan", "none"}:
        return cliente_id
    return f"{norm(row.get('Cliente', 'cliente'))}_{fallback}"

def registros_do_cliente(registros, cliente_id, cliente):
    return [
        registro for registro in registros
        if cliente_corresponde(registro, cliente_id, cliente)
    ]

def ordenar_registros(registros, campo_data="data", campo_hora="hora"):
    def chave(registro):
        texto = str(registro.get(campo_data, ""))
        if campo_hora:
            texto += " " + str(registro.get(campo_hora, ""))
        data_registro = pd.to_datetime(texto, dayfirst=True, errors="coerce")
        return data_registro if pd.notna(data_registro) else pd.Timestamp.min
    return sorted(registros, key=chave, reverse=True)

def renderizar_historico_cliente(row):
    cliente_id = str(row.get("Cliente ID", "")).strip()
    cliente = str(row["Cliente"])
    contatos = ordenar_registros(registros_do_cliente(
        st.session_state.contatos_realizados, cliente_id, cliente
    ))
    observacoes = ordenar_registros(registros_do_cliente(
        st.session_state.observacoes_clientes, cliente_id, cliente
    ))
    retornos = ordenar_registros(
        registros_do_cliente(
            st.session_state.retornos_programados, cliente_id, cliente
        ),
        "data_retorno", None
    )
    ultima_compra = row.get("ultima_compra")
    ultima_compra_txt = (
        ultima_compra.strftime("%d/%m/%Y")
        if pd.notna(ultima_compra) else "NÃ£o informada"
    )

    st.write(f"**Vendedor responsÃ¡vel:** {row.get('Vendedor', 'Sem vendedor')}")
    st.write(f"**Status atual:** {row.get('temperatura', 'NÃ£o informado')}")
    st.write(f"**Ãšltima compra:** {ultima_compra_txt}")

    st.markdown("**OrÃ§amentos em aberto**")
    numeros = row.get("numeros_orcamentos", [])
    if numeros:
        st.write(", ".join(str(numero) for numero in numeros[-5:]))
    else:
        st.caption("Nenhum orÃ§amento em aberto.")

    st.markdown("**Ãšltimos contatos**")
    if contatos:
        for contato in contatos[:5]:
            detalhe = (
                f"{contato.get('data', '')} {contato.get('hora', '')} - "
                f"{contato.get('status', 'contato')}"
            )
            if str(contato.get("observacao", "")).strip():
                detalhe += f": {contato['observacao']}"
            st.write(detalhe)
    else:
        st.caption("Nenhum contato registrado.")

    st.markdown("**Ãšltimas observaÃ§Ãµes**")
    if observacoes:
        for observacao in observacoes[:5]:
            st.write(
                f"{observacao.get('data', '')} {observacao.get('hora', '')} - "
                f"{observacao.get('observacao', '')}"
            )
    else:
        st.caption("Nenhuma observaÃ§Ã£o registrada.")

    st.markdown("**Retornos programados**")
    if retornos:
        for retorno in retornos[:5]:
            st.write(
                f"{retorno.get('data_retorno', '')} - "
                f"{retorno.get('motivo', 'Retorno comercial')} "
                f"({retorno.get('status', 'pendente')})"
            )
    else:
        st.caption("Nenhum retorno programado.")

def renderizar_lista_itens(titulo, itens):
    st.markdown(f"**{titulo}**")
    if not itens:
        st.caption("Nenhum item encontrado para este cliente.")
        return
    for item in itens[:30]:
        st.write(f"- {item}")
    if len(itens) > 30:
        st.caption(f"Mostrando 30 de {len(itens)} itens encontrados.")

def parse_itens_orcamento_colados(texto):
    itens = []
    for linha_num, linha in enumerate(str(texto or "").splitlines(), start=1):
        linha = linha.strip()
        if not linha:
            continue
        separador = ";" if ";" in linha else "\t"
        partes = [parte.strip() for parte in linha.split(separador)]
        if linha_num == 1 and {norm(p) for p in partes} & {"produto", "quantidade", "qtd"}:
            continue
        if len(partes) >= 3:
            produto, quantidade_raw, valor_raw = partes[:3]
        elif len(partes) == 2:
            produto, quantidade_raw = partes
            valor_raw = ""
        else:
            produto = partes[0]
            quantidade_raw = "1"
            valor_raw = ""
        if not produto:
            raise ValueError(f"Linha {linha_num}: informe o nome do produto.")
        quantidade = valor_numerico_simples(quantidade_raw, 0)
        if quantidade <= 0:
            raise ValueError(f"Linha {linha_num}: quantidade deve ser maior que zero.")
        valor = valor_numerico_simples(valor_raw, 0)
        itens.append({
            "produto": produto,
            "quantidade": quantidade,
            "valor": valor,
            "linha": linha_num,
        })
    if not itens:
        raise ValueError("Cole ao menos um produto no formato Produto;Quantidade.")
    return itens

def ultimo_preco_produto_cliente(dados, cliente_id, produto_nome):
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if vendas.empty:
        return 0.0, None
    data_col = achar_coluna(vendas, ["data"])
    if not data_col or "_itens_texto" not in vendas.columns:
        return 0.0, None
    base = vendas[vendas["_cliente_chave"].astype(str) == str(cliente_id)].copy()
    if base.empty:
        return 0.0, None
    produto_norm = norm(produto_nome)
    for _, venda in base.sort_values(data_col, ascending=False).iterrows():
        itens = venda.get("_itens_texto", [])
        if not isinstance(itens, list):
            itens = [itens] if str(itens).strip() else []
        for item in itens:
            dados_item = dados_item_resumo(item)
            if produto_norm in norm(dados_item["nome"]):
                return (
                    float(dados_item["unitario"] or 0),
                    pd.to_datetime(venda.get(data_col), errors="coerce"),
                )
    return 0.0, None

def produto_payload_orcamento(api, loja_id, nome_produto, quantidade, valor, detalhes):
    produtos = []
    try:
        produtos = api.products(loja_id, nome_produto)
    except Exception:
        produtos = []
    produto = produtos[0] if produtos else {}
    product_id = produto.get("id")
    product_name = produto.get("nome") or nome_produto
    variation_id = None
    variacoes = produto.get("variacoes") or []
    if len(variacoes) == 1:
        variation_id = (variacoes[0].get("variacao") or {}).get("id")
    return {
        "produto": {
            "id": product_id,
            "nome_produto": product_name,
            "variacao_id": variation_id,
            "detalhes": detalhes,
            "quantidade": str(quantidade),
            "valor_venda": str(valor),
            "tipo_desconto": "R$",
            "desconto_valor": "0",
            "desconto_porcentagem": "0",
        }
    }

def situacao_inicial_orcamento(api, loja_id):
    situacoes = api.budget_statuses(loja_id)
    return next(
        (
            s for s in situacoes
            if str(s.get("nome", "")).strip().lower() in {"em aberto", "aberto"}
        ),
        situacoes[0] if situacoes else None,
    )

def criar_orcamento_gestaoclick_api(
    dados, cliente_id, vendedor_id, itens, codigo=None, observacao_extra=""
):
    loja_id = dados.get("loja_id")
    if not loja_id:
        raise RuntimeError("Loja nÃ£o identificada. Atualize os dados pela API.")
    api = api_gestaoclick()
    situacao = situacao_inicial_orcamento(api, loja_id)
    if not situacao:
        raise RuntimeError("NÃ£o foi possÃ­vel localizar uma situaÃ§Ã£o inicial para orÃ§amento.")
    produtos = []
    for item in itens:
        data_preco = item.get("data_preco")
        data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data nÃ£o identificada"
        detalhes = (
            item.get("detalhes")
            or f"PreÃ§o sugerido com base na Ãºltima venda em {data_txt}."
        )
        produtos.append(
            produto_payload_orcamento(
                api,
                loja_id,
                item["produto"],
                item["quantidade"],
                item["valor"],
                detalhes,
            )
        )
    payload = {
        "tipo": "produto",
        "cliente_id": int(cliente_id),
        "situacao_id": int(situacao["id"]),
        "data": date.today().isoformat(),
        "validade": "10 dias",
        "condicao_pagamento": "a_vista",
        "valor_frete": 0,
        "produtos": produtos,
        "observacoes_interna": observacao_extra,
    }
    if codigo:
        existente = api.find_budget_by_code(codigo, loja_id)
        if existente:
            raise RuntimeError(f"O orÃ§amento {codigo} jÃ¡ existe no GestÃ£oClick.")
        payload["codigo"] = int(codigo)
    if vendedor_id:
        payload["vendedor_id"] = int(vendedor_id)
    criado = api.create_budget(payload, loja_id)
    if not criado.get("id"):
        raise RuntimeError("O GestÃ£oClick nÃ£o retornou o ID do orÃ§amento criado.")
    return api.budget(criado["id"], loja_id)

def status_aberto_resumo_diario(status):
    return not status_fechado_orcamento(status)

def data_retorno_cliente(cliente_id, cliente):
    retornos = retornos_pendentes_cliente(cliente_id, cliente)
    datas = []
    for retorno in retornos:
        data_retorno = pd.to_datetime(
            retorno.get("data_retorno"), dayfirst=True, errors="coerce"
        )
        if pd.notna(data_retorno):
            datas.append(data_retorno)
    return min(datas) if datas else pd.NaT

def nome_item_resumo(texto):
    texto = texto_valido(texto)
    if not texto:
        return ""
    return texto.split(" | ")[0].strip()

def dados_item_resumo(texto):
    texto = str(texto or "").strip()
    partes = [parte.strip() for parte in texto.split("|")]
    nome = partes[0] if partes else ""
    quantidade = 1.0
    valor = 0.0
    for parte in partes[1:]:
        parte_lower = parte.lower()
        if "qtd" in parte_lower:
            quantidade = valor_numerico_simples(
                re.sub(r"[^0-9,.\-]", "", parte), 1
            )
        elif "r$" in parte_lower:
            valor_limpo = (
                parte_lower.replace("r$", "").strip()
                .replace("unit.", "").replace("unit", "")
                .replace(".", "").replace(",", ".")
            )
            valor = valor_numerico_simples(valor_limpo, 0)
    unitario = valor if any("unit" in parte.lower() for parte in partes[1:]) else (
        valor / quantidade if quantidade and valor else 0.0
    )
    return {
        "nome": nome,
        "quantidade": quantidade,
        "valor": valor,
        "unitario": unitario,
    }

def montar_ofertas_recompra(dados, vendedor="Todas"):
    clientes = dados.get("clientes", pd.DataFrame()).copy()
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if clientes.empty or vendas.empty:
        return pd.DataFrame()

    data_col = achar_coluna(vendas, ["data"])
    vendedor_col = achar_coluna(clientes, ["vendedor"])
    if not data_col or "_itens_texto" not in vendas.columns:
        return pd.DataFrame()

    if vendedor and vendedor != "Todas" and vendedor_col:
        clientes = clientes[clientes[vendedor_col].astype(str).str.strip() == vendedor].copy()
    if clientes.empty:
        return pd.DataFrame()

    mapa_clientes = clientes.set_index("Cliente ID").to_dict("index")
    hoje = pd.Timestamp(date.today())
    linhas = []
    vendas = vendas.dropna(subset=[data_col]).copy()
    for cliente_id, grupo_cliente in vendas.groupby("_cliente_chave"):
        info = mapa_clientes.get(cliente_id)
        if not info:
            continue
        cliente = str(info.get("Cliente", "Cliente sem nome"))
        if contato_realizado_periodo(cliente_id, cliente, 7):
            continue

        intervalo_cliente = int(info.get("intervalo", 0) or 0)
        if intervalo_cliente <= 0:
            continue

        itens = []
        for _, venda in grupo_cliente.sort_values(data_col).iterrows():
            lista_itens = venda.get("_itens_texto", [])
            if not isinstance(lista_itens, list):
                lista_itens = [lista_itens] if str(lista_itens).strip() else []
            for item in lista_itens:
                nome_item = nome_item_resumo(item)
                if nome_item:
                    itens.append({
                        "item": nome_item,
                        "data": pd.to_datetime(venda[data_col], errors="coerce"),
                        "valor": float(venda.get(achar_coluna(vendas, ["valor"]), 0) or 0),
                    })
        if not itens:
            itens_comprados = info.get("itens_comprados", [])
            if isinstance(itens_comprados, list) and itens_comprados:
                itens.append({
                    "item": nome_item_resumo(itens_comprados[0]),
                    "data": pd.to_datetime(info.get("ultima_compra"), errors="coerce"),
                    "valor": float(info.get("ticket_medio", 0) or 0),
                })
        if not itens:
            continue

        itens_df = pd.DataFrame(itens).dropna(subset=["data"])
        if itens_df.empty:
            continue
        melhor = None
        for item, grupo_item in itens_df.groupby("item"):
            datas = grupo_item["data"].sort_values()
            intervalo_item = int(datas.diff().mean().days) if len(datas) > 1 else intervalo_cliente
            if intervalo_item <= 0:
                intervalo_item = intervalo_cliente
            ultima = datas.max()
            dias_sem = int((hoje - ultima.normalize()).days)
            if dias_sem < max(1, int(intervalo_item * 0.8)):
                continue
            prioridade = dias_sem - intervalo_item
            ultimo_valor, ultima_data_preco = ultimo_preco_produto_cliente(
                dados, cliente_id, item
            )
            candidato = {
                "Cliente": cliente,
                "Cliente ID": cliente_id,
                "Documento": info.get("Documento", ""),
                "Vendedor": info.get("Vendedor", "Sem vendedor"),
                "Vendedor ID": info.get("Vendedor ID", ""),
                "Produto": item,
                "Intervalo": intervalo_item,
                "Dias sem comprar": dias_sem,
                "Ãšltima compra": ultima.strftime("%d/%m/%Y"),
                "Ticket mÃ©dio": float(info.get("ticket_medio", 0) or 0),
                "_ultimo_valor_sugerido": ultimo_valor,
                "_ultima_data_preco": ultima_data_preco,
                "_loja_id": dados.get("loja_id", ""),
                "Oferta": (
                    f"{cliente} compra {item} a cada {intervalo_item} dias "
                    f"e estÃ¡ hÃ¡ {dias_sem} dias sem comprar. Ligar oferecendo {item}."
                ),
                "_prioridade": prioridade,
            }
            if melhor is None or candidato["_prioridade"] > melhor["_prioridade"]:
                melhor = candidato
        if melhor:
            linhas.append(melhor)

    ofertas = pd.DataFrame(linhas)
    if not ofertas.empty:
        ofertas = ofertas.sort_values(
            ["_prioridade", "Ticket mÃ©dio"], ascending=[False, False]
        )
    return ofertas

def montar_resumo_diario_oportunidades(dados, vendedor="Todas"):
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame()).copy()
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if orcamentos.empty:
        return pd.DataFrame(), {
            "calls": 0, "hot": 0, "returns": 0, "untouched": 0, "expiring": 0
        }

    co_num = dados.get("co_num") or achar_coluna(orcamentos, ["nÂº", "nÂ°", "numero", "nÃºmero"])
    co_cli = dados.get("co_cli") or achar_coluna(orcamentos, ["cliente"])
    co_data = dados.get("co_data") or achar_coluna(orcamentos, ["data"])
    co_valor = dados.get("co_valor") or achar_coluna(orcamentos, ["valor"])
    co_status = achar_coluna(orcamentos, ["situaÃ§Ã£o", "situacao", "status"])
    co_vendedor = achar_coluna(orcamentos, ["vendedor"])
    co_cli_id = achar_coluna(orcamentos, ["cliente id"])
    co_validade = achar_coluna(orcamentos, ["validade"])

    if not all([co_num, co_cli, co_data, co_status]):
        return pd.DataFrame(), {
            "calls": 0, "hot": 0, "returns": 0, "untouched": 0, "expiring": 0
        }

    hoje = pd.Timestamp(date.today())
    orcamentos[co_data] = pd.to_datetime(orcamentos[co_data], errors="coerce")
    orcamentos = orcamentos.dropna(subset=[co_data]).copy()
    orcamentos = orcamentos[orcamentos[co_status].apply(status_aberto_resumo_diario)].copy()
    orcamentos = orcamentos[
        orcamentos[co_data] >= hoje - pd.Timedelta(days=90)
    ].copy()

    if co_vendedor:
        orcamentos["_vendedor_resumo"] = (
            orcamentos[co_vendedor].fillna("Sem vendedor").astype(str).str.strip()
        )
    else:
        orcamentos["_vendedor_resumo"] = "Sem vendedor"
    orcamentos.loc[orcamentos["_vendedor_resumo"].isin(["", "nan", "None"]), "_vendedor_resumo"] = "Sem vendedor"
    if vendedor and vendedor != "Todas":
        orcamentos = orcamentos[orcamentos["_vendedor_resumo"] == vendedor].copy()

    if orcamentos.empty:
        return pd.DataFrame(), {
            "calls": 0, "hot": 0, "returns": 0, "untouched": 0, "expiring": 0
        }

    if co_cli_id:
        orcamentos["_cliente_chave_resumo"] = orcamentos[co_cli_id].astype(str).str.strip()
        orcamentos.loc[
            orcamentos["_cliente_chave_resumo"].str.lower().isin(["", "nan", "none"]),
            "_cliente_chave_resumo"
        ] = orcamentos.loc[
            orcamentos["_cliente_chave_resumo"].str.lower().isin(["", "nan", "none"]), co_cli
        ].map(norm)
    else:
        orcamentos["_cliente_chave_resumo"] = orcamentos[co_cli].map(norm)

    venda_counts = pd.Series(dtype=int)
    if not vendas.empty:
        chave_venda = "_cliente_chave" if "_cliente_chave" in vendas.columns else achar_coluna(vendas, ["cliente id", "cliente"])
        if chave_venda:
            venda_counts = vendas.groupby(chave_venda).size()
    budget_counts = orcamentos.groupby("_cliente_chave_resumo").size()

    linhas = []
    counters = {"calls": 0, "hot": 0, "returns": 0, "untouched": 0, "expiring": 0}
    for _, row in orcamentos.iterrows():
        cliente = texto_valido(row.get(co_cli, ""), "Cliente sem nome")
        cliente_id = texto_valido(row.get(co_cli_id, "")) if co_cli_id else ""
        chave = row["_cliente_chave_resumo"]
        data_orc = pd.to_datetime(row[co_data], errors="coerce")
        idade = int((hoje - data_orc.normalize()).days) if pd.notna(data_orc) else 0
        total = float(row.get(co_valor, 0) or 0) if co_valor else 0.0
        ja_ligou = contato_realizado_periodo(cliente_id, cliente, 7)
        retorno_data = data_retorno_cliente(cliente_id, cliente)
        tem_retorno = pd.notna(retorno_data) and retorno_data.normalize() <= hoje
        compra_count = int(venda_counts.get(chave, 0)) if not venda_counts.empty else 0
        budget_count = int(budget_counts.get(chave, 0))
        score = 20
        score += min(int(total / 1000) * 2, 30)
        score += min(compra_count * 12, 24)
        score += min(budget_count * 3, 12)
        score += 12 if ja_ligou else 0
        score -= min(max(idade - 7, 0), 20)
        score = max(0, min(score, 100))

        categorias = []
        if tem_retorno:
            categorias.append((110, "RETORNO", "Retorno agendado para hoje ou atrasado", "Retornar"))
            counters["returns"] += 1
        elif not ja_ligou and idade == 2:
            categorias.append((100, "RETORNO", "OrÃ§amento com 2 dias: ligar hoje", "Ligar"))
            counters["returns"] += 1
        if not ja_ligou and idade == 3:
            categorias.append((105, "SEM CONTATO", "Urgente: orÃ§amento com 3 dias", "Ligar urgente"))
            counters["untouched"] += 1
        elif not ja_ligou and idade >= 4:
            categorias.append((108, "SEM CONTATO", f"Risco de perda: orÃ§amento com {idade} dias", "Priorizar"))
            counters["untouched"] += 1

        sinais = []
        if total >= 5000:
            sinais.append("alto valor")
        if compra_count > 0:
            sinais.append("jÃ¡ comprou")
        if budget_count > 1:
            sinais.append("cliente recorrente")
        if ja_ligou:
            sinais.append("contato hoje")
        oportunidade_quente = score >= 65 and len(sinais) >= 2 and idade <= 14
        if oportunidade_quente:
            categorias.append((90, "QUENTE", "Oportunidade: " + ", ".join(sinais[:3]), "Priorizar"))
            counters["hot"] += 1

        validade_txt = str(row.get(co_validade, "")).strip() if co_validade else ""
        validade = pd.to_datetime(validade_txt, dayfirst=True, errors="coerce")
        if pd.notna(validade) and 0 <= (validade.normalize() - hoje).days <= 3:
            dias = int((validade.normalize() - hoje).days)
            categorias.append((85, "VENCENDO", f"Validade termina em {dias} dias", "Renovar"))
            counters["expiring"] += 1
        if not ja_ligou and idade == 1:
            categorias.append((60, "NOVO", "OrÃ§amento com 1 dia: acompanhamento normal", "Acompanhar"))

        if not categorias:
            continue
        prioridade, categoria, motivo, acao = max(categorias, key=lambda valor: valor[0])
        numero_orcamento = texto_valido(row.get(co_num, ""))
        oferta_orcamento = motivo
        if numero_orcamento:
            oferta_orcamento = f"{motivo}. Acompanhar orÃ§amento {numero_orcamento}."
        if categoria in ("RETORNO", "SEM CONTATO", "VENCENDO"):
            counters["calls"] += 1
        linhas.append({
            "Categoria": categoria,
            "Score": score,
            "Cliente": cliente,
            "Vendedor": texto_valido(row["_vendedor_resumo"], "Sem vendedor"),
            "OrÃ§amento": numero_orcamento,
            "Valor": total,
            "Idade": idade,
            "Ãšltimo contato": "Hoje" if ja_ligou else f"{idade} dias sem contato",
            "Motivo": motivo,
            "Oferta": oferta_orcamento,
            "AÃ§Ã£o": acao,
            "_oportunidade_quente": oportunidade_quente,
            "_prioridade": prioridade,
            "_budget_id": str(row.get("_orcamento_id", "") or row.get(co_num, "")),
            "_cliente_id": cliente_id,
        })
    oportunidades = pd.DataFrame(linhas)
    if not oportunidades.empty:
        counters["hot"] = int(oportunidades["_oportunidade_quente"].sum())
        oportunidades = oportunidades.sort_values(
            ["_prioridade", "Score", "Valor", "Cliente"],
            ascending=[False, False, False, True]
        )
    return oportunidades, counters

def renderizar_botao_liguei_resumo(cliente_id, cliente, vendedor, oferta, chave):
    oferta = texto_valido(oferta, "acompanhamento comercial")
    observacao_padrao = (
        f"Contato feito em {date.today():%d/%m/%Y} oferecendo: {oferta}"
        if oferta else ""
    )
    observacao = st.text_area(
        "AnotaÃ§Ã£o para salvar no CRM",
        value=observacao_padrao,
        key=f"resumo_diario_anotacao_{chave}",
    )
    if st.button(
        "JÃ¡ Liguei",
        key=f"resumo_diario_liguei_{chave}",
        type="primary",
        use_container_width=True,
    ):
        try:
            salvar_contato_realizado(
                cliente_id, cliente, vendedor, observacao, "resumo_diario"
            )
            st.success("Contato registrado e anotaÃ§Ã£o salva no CRM.")
            st.rerun()
        except Exception as e:
            st.error(f"NÃ£o foi possÃ­vel registrar o contato: {e}")

def renderizar_agendamento_resumo(cliente_id, cliente, vendedor, chave):
    with st.expander("Agendar retorno"):
        data_retorno = st.date_input(
            "Data do retorno",
            value=date.today() + timedelta(days=1),
            min_value=date.today(),
            key=f"resumo_diario_data_retorno_{chave}"
        )
        motivo = st.text_input(
            "Motivo",
            value="Retorno comercial",
            key=f"resumo_diario_motivo_{chave}"
        )
        observacao_retorno = st.text_area(
            "ObservaÃ§Ã£o do retorno",
            key=f"resumo_diario_obs_retorno_{chave}"
        )
        if st.button(
            "Salvar retorno",
            key=f"resumo_diario_agendar_{chave}",
            use_container_width=True,
        ):
            try:
                agendar_retorno_cliente(
                    cliente_id, cliente, vendedor, data_retorno,
                    motivo, observacao_retorno,
                )
                st.success(f"Retorno agendado para {data_retorno:%d/%m/%Y}.")
                st.rerun()
            except Exception as e:
                st.error(f"NÃ£o foi possÃ­vel agendar o retorno: {e}")

def texto_email_resumo(cliente, vendedor, oferta, row=None):
    row = row if row is not None else {}
    oferta = texto_valido(oferta, "acompanhamento comercial")
    produto = texto_valido(row.get("Produto", ""))
    orcamento = texto_valido(row.get("OrÃ§amento", ""))
    categoria = texto_valido(row.get("Categoria", ""))
    motivo = texto_valido(row.get("Motivo", oferta), oferta)
    valor = row.get("Valor", row.get("Ticket mÃ©dio", 0))
    intervalo = row.get("Intervalo", "")
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    valor_txt = fmt(valor) if valor else ""
    intervalo_num = valor_numerico_simples(intervalo, 0)
    dias_num = valor_numerico_simples(dias, 0)

    if orcamento:
        assunto = f"Sobre o orÃ§amento {orcamento}"
        detalhe_valor = f" no valor de {valor_txt}" if valor_txt else ""
        urgencia = (
            f"Vi que ele jÃ¡ estÃ¡ hÃ¡ {dias} dias em aberto, entÃ£o quis te chamar antes de perdermos o timing."
            if valor_informado(dias) else
            "Quis te chamar para ver se ficou alguma dÃºvida ou se posso te ajudar a seguir com ele."
        )
        corpo = (
            f"OlÃ¡, tudo bem?\n\n"
            f"Passando rapidinho para saber se conseguimos avanÃ§ar com o orÃ§amento {orcamento}{detalhe_valor}.\n\n"
            f"{urgencia}\n\n"
            f"Se fizer sentido para vocÃª, posso revisar algum detalhe, ajustar quantidade ou ver uma condiÃ§Ã£o para fecharmos.\n\n"
            f"Posso dar sequÃªncia por aqui?\n\n"
            f"AbraÃ§o,\n"
            f"{vendedor}\n"
            f"Novaprint"
        )
        return assunto, corpo

    if produto:
        assunto = f"ReposiÃ§Ã£o de {produto}"
        ciclo = (
            f"Vi aqui que vocÃªs costumam comprar {produto} a cada {int(intervalo_num)} dias"
            if intervalo_num > 0
            else f"Vi aqui uma oportunidade para reposiÃ§Ã£o de {produto}"
        )
        tempo = (
            f" e jÃ¡ faz {int(dias_num)} dias desde a Ãºltima compra."
            if dias_num > 0
            else "."
        )
        corpo = (
            f"OlÃ¡, tudo bem?\n\n"
            f"{ciclo}{tempo}\n\n"
            f"Quer que eu jÃ¡ separe uma condiÃ§Ã£o para reposiÃ§Ã£o? "
            f"Se quiser, tambÃ©m posso revisar a quantidade ideal para evitar falta ou compra maior que o necessÃ¡rio.\n\n"
            f"Posso te mandar uma proposta atualizada de {produto}?\n\n"
            f"AbraÃ§o,\n"
            f"{vendedor}\n"
            f"Novaprint"
        )
        return assunto, corpo

    assunto = f"Seguimos com essa demanda?"
    corpo = (
        f"OlÃ¡, tudo bem?\n\n"
        f"Passei para retomar com vocÃª esse ponto que ficou em aberto:\n\n"
        f"{motivo}\n\n"
        f"Se ainda fizer sentido, posso te ajudar a avanÃ§ar com isso hoje ou ajustar o que for necessÃ¡rio.\n\n"
        f"Como prefere seguir?\n\n"
        f"AbraÃ§o,\n"
        f"{vendedor}\n"
        f"Novaprint"
    )
    return assunto, corpo

def renderizar_email_resumo(cliente, vendedor, oferta, chave, row=None):
    with st.expander("Preparar e-mail"):
        conta_saida = conta_email_para_vendedor(vendedor)
        if conta_saida:
            st.caption(
                f"SaÃ­da configurada: {conta_saida.get('name', conta_saida.get('email'))} "
                f"<{conta_saida.get('email')}>"
            )
        else:
            st.caption("Nenhuma caixa SMTP configurada. O CRM vai manter o rascunho por mailto.")
        destinatario = st.text_input(
            "E-mail do cliente",
            key=f"email_destino_resumo_{chave}"
        )
        assunto_padrao, corpo_padrao = texto_email_resumo(cliente, vendedor, oferta, row)
        assunto = st.text_input(
            "Assunto",
            value=assunto_padrao,
            key=f"email_assunto_resumo_{chave}"
        )
        corpo = st.text_area(
            "Mensagem",
            value=corpo_padrao,
            height=220,
            key=f"email_corpo_resumo_{chave}"
        )
        if destinatario.strip():
            if conta_saida:
                confirmado_email = st.checkbox(
                    "Revisei e autorizo enviar este e-mail pelo CRM.",
                    key=f"email_confirmar_envio_{chave}",
                )
                if st.button(
                    "Enviar e-mail pelo CRM",
                    key=f"email_enviar_crm_{chave}",
                    type="primary",
                    disabled=not confirmado_email,
                    use_container_width=True,
                ):
                    try:
                        origem = enviar_email_crm(
                            conta_saida,
                            destinatario.strip(),
                            assunto,
                            corpo,
                        )
                        cliente_id = str(
                            (row or {}).get("Cliente ID", (row or {}).get("_cliente_id", ""))
                            or ""
                        )
                        salvar_contato_realizado(
                            cliente_id,
                            cliente,
                            vendedor,
                            f"E-mail enviado pelo CRM. Assunto: {assunto}. Oferta/aÃ§Ã£o: {oferta}",
                            "email",
                            "email enviado",
                        )
                        st.success(f"E-mail enviado por {origem}.")
                    except Exception as e:
                        st.error(f"NÃ£o foi possÃ­vel enviar pelo CRM: {e}")
            link = (
                "mailto:"
                + urllib.parse.quote(destinatario.strip())
                + "?subject="
                + urllib.parse.quote(assunto)
                + "&body="
                + urllib.parse.quote(corpo)
            )
            st.markdown(f"[Abrir rascunho no e-mail]({link})")
        else:
            st.caption("Informe o e-mail do cliente para gerar o rascunho.")

def texto_whatsapp_resumo(cliente, vendedor, oferta, row=None):
    row = row if row is not None else {}
    oferta = texto_valido(oferta, "acompanhamento comercial")
    produto = texto_valido(row.get("Produto", ""))
    orcamento = texto_valido(row.get("OrÃ§amento", ""))
    valor = row.get("Valor", row.get("Ticket mÃ©dio", 0))
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    intervalo = row.get("Intervalo", "")
    valor_txt = fmt(valor) if valor else ""

    if orcamento:
        trecho_valor = f" ({valor_txt})" if valor_txt else ""
        trecho_tempo = f" Vi que ele estÃ¡ hÃ¡ {dias} dias em aberto." if valor_informado(dias) else ""
        return (
            f"Oi, tudo bem? Aqui Ã© {vendedor}, da Novaprint.\n\n"
            f"Passando para ver se conseguimos avanÃ§ar com o orÃ§amento {orcamento}{trecho_valor}."
            f"{trecho_tempo}\n\n"
            "Ficou alguma dÃºvida ou quer que eu ajuste alguma condiÃ§Ã£o para fecharmos?"
        )

    if produto:
        trecho_ciclo = (
            f"Vi aqui que vocÃªs costumam comprar {produto} a cada {intervalo} dias. "
            if valor_informado(intervalo) else
            f"Vi aqui uma oportunidade de reposiÃ§Ã£o de {produto}. "
        )
        trecho_tempo = f"JÃ¡ faz {dias} dias desde a Ãºltima compra. " if valor_informado(dias) else ""
        return (
            f"Oi, tudo bem? Aqui Ã© {vendedor}, da Novaprint.\n\n"
            f"{trecho_ciclo}{trecho_tempo}"
            f"Quer que eu prepare uma condiÃ§Ã£o atualizada de {produto} para vocÃª?"
        )

    return (
        f"Oi, tudo bem? Aqui Ã© {vendedor}, da Novaprint.\n\n"
        f"Passando para retomar este ponto: {oferta}\n\n"
        "Quer que eu te ajude a dar sequÃªncia?"
    )

def renderizar_whatsapp_resumo(cliente, vendedor, oferta, chave, row=None):
    with st.expander("Preparar WhatsApp"):
        row = row if row is not None else {}
        telefone_padrao = texto_valido(
            row.get("Telefone", row.get("Celular", row.get("WhatsApp", "")))
        )
        telefone = st.text_input(
            "WhatsApp do cliente",
            value=telefone_padrao,
            key=f"whatsapp_destino_resumo_{chave}",
            placeholder="Exemplo: 11999999999",
        )
        mensagem = st.text_area(
            "Mensagem",
            value=texto_whatsapp_resumo(cliente, vendedor, oferta, row),
            height=170,
            key=f"whatsapp_msg_resumo_{chave}",
        )
        numero = somente_digitos(telefone)
        if numero:
            if not numero.startswith("55"):
                numero = "55" + numero
            link = "https://wa.me/" + numero + "?text=" + urllib.parse.quote(mensagem)
            if watidy_configurado():
                if st.button(
                    "Enviar pelo Watidy",
                    key=f"whatsapp_watidy_enviar_{chave}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        status, resposta = enviar_whatsapp_watidy(numero, mensagem)
                        cliente_id = str(
                            (row or {}).get("Cliente ID", (row or {}).get("_cliente_id", ""))
                            or ""
                        )
                        salvar_contato_realizado(
                            cliente_id,
                            cliente,
                            vendedor,
                            f"WhatsApp enviado pelo Watidy. Oferta/aÃ§Ã£o: {oferta}",
                            "whatsapp",
                            "whatsapp enviado",
                        )
                        st.success(f"Mensagem enviada pelo Watidy. Status {status}.")
                        if resposta:
                            st.caption(resposta[:300])
                    except Exception as e:
                        st.error(f"NÃ£o foi possÃ­vel enviar pelo Watidy: {e}")
                        st.markdown(f"[Abrir conversa no WhatsApp]({link})")
            else:
                st.caption("Watidy nÃ£o configurado nos secrets. Usando rascunho manual.")
                st.markdown(f"[Abrir conversa no WhatsApp]({link})")
        else:
            st.caption("Informe o WhatsApp do cliente para gerar a conversa.")

def renderizar_criar_orcamento_sugerido(row, chave):
    produto = texto_valido(row.get("Produto", ""))
    cliente_id = texto_valido(row.get("Cliente ID", row.get("_cliente_id", "")))
    if not produto or not cliente_id:
        return
    with st.expander("Criar orÃ§amento"):
        valor_sugerido = numero_seguro(row.get("_ultimo_valor_sugerido", 0), 0.0)
        data_preco = row.get("_ultima_data_preco")
        qtd = st.number_input(
            "Quantidade",
            min_value=1.0,
            value=1.0,
            step=1.0,
            key=f"orc_sug_qtd_{chave}",
        )
        valor = st.number_input(
            "Valor sugerido",
            min_value=0.0,
            value=valor_sugerido,
            step=10.0,
            key=f"orc_sug_valor_{chave}",
        )
        data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data nÃ£o identificada"
        st.caption(f"PreÃ§o sugerido da Ãºltima venda em {data_txt}.")
        codigo = st.text_input(
            "NÃºmero do orÃ§amento (opcional)",
            key=f"orc_sug_codigo_{chave}",
        )
        confirmado = st.checkbox(
            "Revisei e autorizo criar este orÃ§amento no GestÃ£oClick.",
            key=f"orc_sug_confirmar_{chave}",
        )
        if st.button(
            "Criar orÃ§amento no GestÃ£oClick",
            disabled=not confirmado,
            type="primary",
            use_container_width=True,
            key=f"orc_sug_criar_{chave}",
        ):
            try:
                item = {
                    "produto": produto,
                    "quantidade": qtd,
                    "valor": valor,
                    "data_preco": data_preco,
                    "detalhes": f"PreÃ§o sugerido da Ãºltima venda em {data_txt}.",
                }
                criado = criar_orcamento_gestaoclick_api(
                    st.session_state.dados_processados,
                    cliente_id,
                    row.get("Vendedor ID", ""),
                    [item],
                    codigo.strip() or None,
                    f"Criado pelo CRM Inteligente. Produto sugerido: {produto}.",
                )
                numero = criado.get("codigo") or criado.get("id")
                st.success(f"OrÃ§amento {numero} criado no GestÃ£oClick.")
            except Exception as e:
                st.error(f"NÃ£o foi possÃ­vel criar o orÃ§amento: {e}")

def renderizar_card_resumo(row, indice, modo="prioridade"):
    cliente = texto_valido(row.get("Cliente", ""), "Cliente sem nome")
    vendedor = texto_valido(row.get("Vendedor", ""), "Sem vendedor")
    cliente_id = texto_valido(row.get("_cliente_id", row.get("Cliente ID", "")))
    valor = row.get("Valor", row.get("Ticket mÃ©dio", 0))
    oferta = texto_valido(row.get("Oferta", row.get("Motivo", "")))
    categoria = texto_valido(row.get("Categoria", ""), "Recompra")
    score = row.get("Score", "")
    orcamento = texto_valido(row.get("OrÃ§amento", ""))
    acao = texto_valido(row.get("AÃ§Ã£o", ""))
    produto = texto_valido(row.get("Produto", ""))
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    intervalo = row.get("Intervalo", "")
    if not oferta:
        if orcamento:
            oferta = f"Acompanhar retorno do orÃ§amento {orcamento}"
        elif produto:
            oferta = f"Oferecer reposiÃ§Ã£o de {produto}"
        else:
            oferta = "Acompanhamento comercial do cliente"
    chave = chave_widget(
        f"resumo_{modo}_{row.get('_budget_id', '')}_{cliente_id}_{cliente}_{indice}"
    )
    detalhes = []
    if produto:
        detalhes.append(f"Produto sugerido: <b>{html_seguro(produto)}</b>")
    if orcamento:
        detalhes.append(f"OrÃ§amento: <b>{html_seguro(orcamento)}</b>")
    if valor_informado(dias):
        detalhes.append(f"Dias em atenÃ§Ã£o: <b>{html_seguro(dias)}</b>")
    if valor_informado(intervalo):
        detalhes.append(f"Ciclo mÃ©dio: <b>{html_seguro(intervalo)} dias</b>")
    if valor_informado(score):
        detalhes.append(f"Score: <b>{html_seguro(score)}</b>")
    if acao:
        detalhes.append(f"AÃ§Ã£o sugerida: <b>{html_seguro(acao)}</b>")
    detalhes_html = "<br>".join(detalhes)

    st.markdown(
        f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:8px;">
<b>{html_seguro(cliente)}</b><br>
Vendedor: <b>{html_seguro(vendedor)}</b><br>
Valor/ticket: <b>{fmt_html(valor)}</b><br>
Tipo de prioridade: <b>{html_seguro(categoria)}</b><br>
<br>
<b>Por que estÃ¡ na fila?</b><br>
{html_seguro(oferta)}<br>
{detalhes_html}
</div>
""",
        unsafe_allow_html=True,
    )
    renderizar_botao_liguei_resumo(cliente_id, cliente, vendedor, oferta, chave)
    renderizar_email_resumo(cliente, vendedor, oferta, chave, row)
    renderizar_whatsapp_resumo(cliente, vendedor, oferta, chave, row)
    renderizar_criar_orcamento_sugerido(row, chave)
    renderizar_agendamento_resumo(cliente_id, cliente, vendedor, chave)

def renderizar_grid_resumo(df, modo):
    if df.empty:
        st.info("Nenhum cliente encontrado para esta visÃ£o.")
        return
    linhas = list(df.head(30).iterrows())
    for i in range(0, len(linhas), 3):
        cols = st.columns(3)
        for j, (indice, row) in enumerate(linhas[i:i+3]):
            with cols[j]:
                renderizar_card_resumo(row, indice, modo)

def prioridade_crm_para_resumo(prioridade):
    if prioridade.empty:
        return pd.DataFrame()
    linhas = []
    for indice, row in prioridade.iterrows():
        cliente = texto_valido(row.get("Cliente", ""), "Cliente sem nome")
        motivo = texto_valido(row.get("motivo_prioridade", ""))
        acao = texto_valido(row.get("acao_ia", ""), "Entrar em contato")
        produto = ""
        itens = row.get("itens_comprados", [])
        if isinstance(itens, list) and itens:
            produto = texto_valido(itens[0])
        oferta = motivo or acao or "Prioridade comercial do CRM"
        linhas.append({
            "Categoria": "CRM PRIORIDADE",
            "Score": row.get("score_comercial", 0),
            "Cliente": cliente,
            "Cliente ID": row.get("Cliente ID", ""),
            "_cliente_id": row.get("Cliente ID", ""),
            "Telefone": row.get("Telefone", ""),
            "Vendedor": row.get("Vendedor", "Sem vendedor"),
            "Vendedor ID": row.get("Vendedor ID", ""),
            "OrÃ§amento": "",
            "Valor": row.get("ticket_medio", 0),
            "Ticket mÃ©dio": row.get("ticket_medio", 0),
            "Produto": produto,
            "Intervalo": row.get("intervalo", ""),
            "Dias sem comprar": row.get("dias_sem_comprar", ""),
            "Ãšltimo contato": "Hoje" if row.get("ja_ligou_hoje", False) else "",
            "Motivo": motivo,
            "Oferta": oferta,
            "AÃ§Ã£o": acao,
            "_oportunidade_quente": str(row.get("temperatura", "")).find("QUENTE") >= 0,
            "_prioridade": row.get("score_prioridade_dia", row.get("score_comercial", 0)),
            "_budget_id": "",
            "_origem_prioridade": "crm",
            "_linha_origem": indice,
        })
    resultado = pd.DataFrame(linhas)
    return resultado.sort_values(["_prioridade", "Valor"], ascending=[False, False])

def renderizar_busca_cliente_produtos(dados, vendedor="Todas"):
    clientes = dados.get("clientes", pd.DataFrame()).copy()
    vendedor_col = achar_coluna(clientes, ["vendedor"])
    if not clientes.empty and vendedor and vendedor != "Todas" and vendedor_col:
        clientes = clientes[clientes[vendedor_col].astype(str).str.strip() == vendedor].copy()

    termo = st.text_input(
        "Buscar cliente por nome, CNPJ/documento ou ID",
        key="resumo_diario_busca_cliente"
    )
    if not termo.strip():
        st.caption("Digite parte do nome, CNPJ/documento ou ID do cliente.")
        return

    encontrados = pd.DataFrame()
    termo_norm = norm(termo)
    if not clientes.empty:
        documento_col = achar_coluna(clientes, ["documento", "cnpj", "cpf"])
        mascara = (
            clientes["Cliente"].astype(str).map(norm).str.contains(termo_norm, na=False, regex=False) |
            clientes["Cliente ID"].astype(str).map(norm).str.contains(termo_norm, na=False, regex=False)
        )
        if documento_col:
            mascara = mascara | clientes[documento_col].astype(str).map(norm).str.contains(
                termo_norm, na=False, regex=False
            )
        encontrados = clientes[mascara].copy()

    encontrados_api = []
    if dados.get("origem") == "api" and len(termo.strip()) >= 2:
        loja_id = dados.get("loja_id")
        cache_key = f"{loja_id}|{termo.strip().lower()}"
        if "clientes_api_cache" not in st.session_state:
            st.session_state.clientes_api_cache = {}
        if cache_key not in st.session_state.clientes_api_cache:
            try:
                with st.spinner("Buscando cliente no GestÃ£oClick..."):
                    st.session_state.clientes_api_cache[cache_key] = api_gestaoclick().clients(
                        loja_id, termo.strip()
                    )
            except Exception as e:
                st.warning(f"NÃ£o foi possÃ­vel buscar clientes na API: {e}")
                st.session_state.clientes_api_cache[cache_key] = []
        encontrados_api = st.session_state.clientes_api_cache.get(cache_key, [])

    if encontrados_api:
        linhas_api = []
        for item in encontrados_api:
            cliente_id = str(item.get("id") or item.get("cliente_id") or "").strip()
            nome = (
                item.get("nome")
                or item.get("nome_fantasia")
                or item.get("razao_social")
                or item.get("nome_cliente")
                or "Cliente sem nome"
            )
            linhas_api.append({
                "Cliente": nome,
                "Cliente ID": cliente_id,
                "Documento": documento_cliente_registro(item),
                "Vendedor": item.get("nome_vendedor") or "GestÃ£oClick",
                "intervalo": 0,
                "dias_sem_comprar": 0,
                "itens_comprados": [],
                "itens_orcados": [],
                "_origem_busca": "API GestÃ£oClick",
            })
        clientes_api_df = pd.DataFrame(linhas_api)
        if not encontrados.empty:
            ids_locais = set(encontrados["Cliente ID"].astype(str))
            clientes_api_df = clientes_api_df[
                ~clientes_api_df["Cliente ID"].astype(str).isin(ids_locais)
            ]
        encontrados = pd.concat([encontrados, clientes_api_df], ignore_index=True, sort=False)

    if encontrados.empty:
        st.warning("Nenhum cliente encontrado.")
        return

    st.caption(
        "A busca considera a base carregada do CRM e consulta o GestÃ£oClick pela API quando conectado."
    )
    encontrados = encontrados.head(18)
    for i in range(0, len(encontrados), 3):
        cols = st.columns(3)
        for j, (_, r) in enumerate(encontrados.iloc[i:i+3].iterrows()):
            with cols[j]:
                origem = r.get("_origem_busca", "CRM")
                st.markdown(
                    f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:8px;">
<b>{html_seguro(r['Cliente'])}</b><br>
Origem: <b>{html_seguro(origem)}</b><br>
Documento: <b>{html_seguro(r.get('Documento', ''))}</b><br>
Vendedor: <b>{html_seguro(r.get('Vendedor', 'Sem vendedor'))}</b><br>
Compra a cada <b>{int(r.get('intervalo', 0) or 0)} dias</b><br>
Dias sem comprar: <b>{int(r.get('dias_sem_comprar', 0) or 0)}</b>
</div>
""",
                    unsafe_allow_html=True,
                )
                with st.expander("Produtos comprados e orÃ§ados"):
                    renderizar_lista_itens("Itens comprados", r.get("itens_comprados", []))
                    renderizar_lista_itens("Itens orçados", r.get("itens_orcados", []))

def renderizar_geracao_orcamentos():
    st.subheader("Geração de Orçamentos")
    st.caption("Crie orçamentos no GestãoClick usando o formato: Nome do produto; quantidade.")
    dados = st.session_state.dados_processados or {}
    if dados.get("origem") != "api" or not dados.get("loja_id"):
        st.info("Atualize os dados pela API do GestãoClick antes de criar orçamentos.")
        return

    loja_id = dados.get("loja_id")
    col_cliente, col_vendedor = st.columns(2)
    termo_cliente = col_cliente.text_input(
        "Buscar cliente por nome, CNPJ/documento ou ID",
        key="gerar_orc_busca_cliente",
    )
    usuarios = [
        usuario for usuario in st.session_state.get("gestaoclick_usuarios", [])
        if str(usuario.get("id") or "").strip()
    ]
    vendedor = col_vendedor.selectbox(
        "Vendedor",
        [{"id": "", "nome": "Sem vendedor"}, *usuarios],
        format_func=lambda item: item.get("nome") or "Sem vendedor",
        key="gerar_orc_vendedor",
    )

    clientes_api = []
    if termo_cliente.strip():
        cache_key = f"{loja_id}|orc|{termo_cliente.strip().lower()}"
        if "clientes_api_cache" not in st.session_state:
            st.session_state.clientes_api_cache = {}
        if cache_key not in st.session_state.clientes_api_cache:
            try:
                with st.spinner("Buscando cliente no GestÃ£oClick..."):
                    st.session_state.clientes_api_cache[cache_key] = api_gestaoclick().clients(
                        loja_id, termo_cliente.strip()
                    )
            except Exception as e:
                st.warning(f"NÃ£o foi possÃ­vel buscar clientes na API: {e}")
                st.session_state.clientes_api_cache[cache_key] = []
        clientes_api = st.session_state.clientes_api_cache.get(cache_key, [])
        if not clientes_api:
            clientes_base = dados.get("clientes", pd.DataFrame()).copy()
            termo_doc = somente_digitos(termo_cliente)
            if not clientes_base.empty:
                documento_col = achar_coluna(clientes_base, ["documento", "cnpj", "cpf"])
                mascara_local = (
                    clientes_base["Cliente"].astype(str).map(norm).str.contains(
                        norm(termo_cliente), na=False, regex=False
                    )
                    | clientes_base["Cliente ID"].astype(str).map(norm).str.contains(
                        norm(termo_cliente), na=False, regex=False
                    )
                )
                if documento_col and termo_doc:
                    mascara_local = mascara_local | clientes_base[documento_col].astype(str).map(
                        somente_digitos
                    ).str.contains(termo_doc, na=False, regex=False)
                clientes_api = [
                    {
                        "id": row.get("Cliente ID", ""),
                        "nome": row.get("Cliente", ""),
                        "documento": row.get("Documento", ""),
                    }
                    for _, row in clientes_base[mascara_local].head(20).iterrows()
                ]

    cliente_escolhido = None
    if clientes_api:
        cliente_escolhido = st.selectbox(
            "Cliente encontrado",
            clientes_api,
            format_func=lambda c: (
                (c.get("nome") or c.get("nome_fantasia") or c.get("razao_social") or f"Cliente {c.get('id')}")
                + (f" - {c.get('documento')}" if c.get("documento") else "")
            ),
            key="gerar_orc_cliente",
        )
    elif termo_cliente.strip():
        st.warning("Nenhum cliente encontrado para esse termo.")

    texto_itens = st.text_area(
        "Produtos",
        placeholder="Exemplo:\nAdesivo vinil brilho; 10\nBanner 90x120; 2",
        height=180,
        key="gerar_orc_itens",
    )
    st.caption("Use uma linha por item: Nome do produto; quantidade. Opcionalmente: Nome; quantidade; valor.")

    itens = []
    if texto_itens.strip():
        try:
            itens = parse_itens_orcamento_colados(texto_itens)
            st.success(f"{len(itens)} item(ns) identificado(s).")
            preview = pd.DataFrame(itens)[["produto", "quantidade", "valor"]]
            st.dataframe(preview, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(str(e))

    codigo = st.text_input("NÃºmero do orÃ§amento (opcional)", key="gerar_orc_codigo")
    confirmado = st.checkbox(
        "Revisei cliente, vendedor e produtos. Autorizo criar o orÃ§amento no GestÃ£oClick.",
        key="gerar_orc_confirmar",
    )
    if st.button(
        "Criar orÃ§amento no GestÃ£oClick",
        type="primary",
        disabled=not confirmado,
        key="gerar_orc_criar",
    ):
        if not cliente_escolhido:
            st.error("Selecione um cliente.")
            return
        if not itens:
            st.error("Informe ao menos um produto.")
            return
        try:
            itens_final = []
            cliente_id = str(cliente_escolhido.get("id") or cliente_escolhido.get("cliente_id") or "")
            for item in itens:
                valor = item["valor"]
                data_preco = None
                if valor <= 0:
                    valor, data_preco = ultimo_preco_produto_cliente(
                        dados, cliente_id, item["produto"]
                    )
                data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data nÃ£o identificada"
                itens_final.append({
                    **item,
                    "valor": valor,
                    "data_preco": data_preco,
                    "detalhes": f"PreÃ§o sugerido da Ãºltima venda em {data_txt}.",
                })
            criado = criar_orcamento_gestaoclick_api(
                dados,
                cliente_id,
                vendedor.get("id") or "",
                itens_final,
                codigo.strip() or None,
                "Criado pelo módulo Geração de Orçamentos do CRM Inteligente.",
            )
            numero = criado.get("codigo") or criado.get("id")
            st.success(f"Orçamento {numero} criado no GestãoClick.")
        except Exception as e:
            st.error(f"Não foi possível criar o orçamento: {e}")

def renderizar_resumo_diario(dados):
    st.subheader("Comercial")
    st.caption("Prioridades, churn, orçamentos, ofertas de recompra, busca de clientes e ações rápidas por vendedor.")
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame())
    clientes = dados.get("clientes", pd.DataFrame())
    if orcamentos.empty and clientes.empty:
        st.info("Carregue os dados da API para montar o painel comercial.")
        return

    base_vendedores = orcamentos if not orcamentos.empty else clientes
    vendedor_col = achar_coluna(base_vendedores, ["vendedor"])
    vendedores = ["Todas"]
    if vendedor_col:
        vendedores += sorted(
            nome for nome in base_vendedores[vendedor_col].dropna().astype(str).str.strip().unique()
            if nome and nome.lower() not in {"nan", "none"}
        )

    vendedor = st.selectbox("Vendedor", vendedores, key="resumo_diario_vendedor")
    oportunidades, counters = montar_resumo_diario_oportunidades(dados, vendedor)
    ofertas = montar_ofertas_recompra(dados, vendedor)
    prioridade_crm = montar_prioridade(dados.get("clientes", pd.DataFrame()))
    if vendedor and vendedor != "Todas" and not prioridade_crm.empty:
        prioridade_crm = prioridade_crm[
            prioridade_crm["Vendedor"].astype(str).str.strip() == vendedor
        ].copy()
    prioridade_resumo = prioridade_crm_para_resumo(prioridade_crm)
    if not prioridade_resumo.empty:
        oportunidades = pd.concat(
            [prioridade_resumo, oportunidades],
            ignore_index=True,
            sort=False,
        ).sort_values(["_prioridade", "Valor"], ascending=[False, False])
        counters["calls"] = counters.get("calls", 0) + len(prioridade_resumo)
        counters["hot"] = counters.get("hot", 0) + int(
            prioridade_resumo.get("_oportunidade_quente", pd.Series(dtype=bool)).sum()
        )

    cols = st.columns(5)
    cols[0].metric("Ligações hoje", counters["calls"] + len(ofertas))
    cols[1].metric("Oportunidades quentes", counters["hot"])
    cols[2].metric("Retornos hoje", counters["returns"])
    cols[3].metric("Sem contato", counters["untouched"])
    cols[4].metric("Vencendo", counters["expiring"])

    if "resumo_diario_secao" not in st.session_state:
        st.session_state.resumo_diario_secao = "Início"

    secao = st.session_state.resumo_diario_secao

    if secao == "Início":
        st.markdown("#### Prioridades e ofertas para hoje")
        st.caption(
            "A tela inicial reúne a prioridade do CRM com ofertas de recompra calculadas pelo ciclo real de compra."
        )
        inicio = pd.concat(
            [prioridade_resumo.head(12), ofertas.head(18)],
            ignore_index=True,
            sort=False,
        )
        renderizar_grid_resumo(inicio, "inicio")

    if secao == "Fila de prioridades":
        st.markdown("#### Fila de prioridades")
        filtro = st.radio(
            "Mostrar",
            ["Todas", "Oportunidades quentes", "Retornos hoje"],
            horizontal=True,
            key="resumo_diario_filtro"
        )
        exibicao = oportunidades.copy()
        if not exibicao.empty and filtro == "Oportunidades quentes":
            exibicao = exibicao[exibicao["_oportunidade_quente"]]
        elif not exibicao.empty and filtro == "Retornos hoje":
            exibicao = exibicao[exibicao["Categoria"] == "RETORNO"]
        renderizar_grid_resumo(exibicao, "fila")

    if secao == "Ofertas de recompra":
        st.markdown("#### Ofertas de recompra")
        st.caption(
            "Sugestões geradas a partir do ciclo real de compra: produto, intervalo e dias sem comprar."
        )
        renderizar_grid_resumo(ofertas, "oferta")

    if secao == "Buscar cliente/produtos":
        st.markdown("#### Buscar cliente e produtos")
        renderizar_busca_cliente_produtos(dados, vendedor)

    if secao == "Ações rápidas":
        st.markdown("#### Ações rápidas")
        combinada = pd.concat(
            [oportunidades.head(15), ofertas.head(15)],
            ignore_index=True,
            sort=False,
        )
        renderizar_grid_resumo(combinada, "acoes")

    if secao == "Visão de gestão":
        st.markdown("#### Desempenho por vendedor")
        if oportunidades.empty and ofertas.empty:
            st.info("Nenhuma prioridade encontrada para gestão.")
            return
        if oportunidades.empty:
            gestao = pd.DataFrame(columns=[
                "Vendedor", "Prioridades", "Ligacoes", "Quentes", "Retornos", "Valor"
            ])
        else:
            gestao = oportunidades.groupby("Vendedor").agg(
                Prioridades=("Cliente", "count"),
                Ligacoes=("Categoria", lambda s: int(s.isin(["RETORNO", "SEM CONTATO", "VENCENDO"]).sum())),
                Quentes=("_oportunidade_quente", "sum"),
                Retornos=("Categoria", lambda s: int((s == "RETORNO").sum())),
                Valor=("Valor", "sum"),
            ).reset_index()
        if not ofertas.empty:
            ofertas_gestao = ofertas.groupby("Vendedor").agg(
                Ofertas=("Cliente", "count"),
                Ticket=("Ticket médio", "sum"),
            ).reset_index()
            gestao = gestao.merge(ofertas_gestao, on="Vendedor", how="outer").fillna(0)
        if "Ofertas" not in gestao.columns:
            gestao["Ofertas"] = 0
        if "Ticket" not in gestao.columns:
            gestao["Ticket"] = 0
        if "Valor" not in gestao.columns:
            gestao["Valor"] = 0
        gestao["Valor"] = gestao["Valor"] + gestao["Ticket"]
        gestao["Valor"] = gestao["Valor"].map(fmt)
        st.dataframe(
            gestao.drop(columns=["Ticket"], errors="ignore"),
            use_container_width=True,
            hide_index=True,
        )
    if secao == "Churn e retenção":
        st.markdown("#### Churn e ações de retenção")
        clientes_base = dados.get("clientes", pd.DataFrame()).copy()
        if vendedor and vendedor != "Todas" and not clientes_base.empty:
            clientes_base = clientes_base[
                clientes_base["Vendedor"].astype(str).str.strip() == vendedor
            ].copy()
        churn = listar_clientes_churn(clientes_base)
        taxa, qtd, base = calcular_churn(clientes_base)
        c1, c2, c3 = st.columns(3)
        c1.metric("Taxa de churn", f"{taxa:.1f}%")
        c2.metric("Clientes em churn", qtd)
        c3.metric("Potencial mensal em risco", fmt(churn["potencial_mensal"].sum() if not churn.empty else 0))
        st.caption(
            "Ações recomendadas: contato consultivo, sugestão de recompra pelo item recorrente, "
            "orçamento com último preço unitário e retorno agendado se o cliente não decidir agora."
        )
        if churn.empty:
            st.success("Nenhum cliente em churn para este filtro.")
        else:
            churn_resumo = prioridade_crm_para_resumo(churn.head(30))
            churn_resumo["Categoria"] = "CHURN"
            churn_resumo["Oferta"] = churn_resumo.apply(
                lambda r: (
                    "Cliente em churn. Retomar relacionamento"
                    + (f" oferecendo {r['Produto']}" if texto_valido(r.get("Produto", "")) else "")
                    + "."
                ),
                axis=1,
            )
            renderizar_grid_resumo(churn_resumo, "churn")

    if secao == "Orçamentos":
        st.markdown("#### Orçamentos para retorno")
        orc_aberto_secao = dados.get("orc_aberto", pd.DataFrame()).copy()
        co_num_secao = dados.get("co_num") or achar_coluna(orc_aberto_secao, ["nº", "n°", "numero", "número"])
        co_cli_secao = dados.get("co_cli") or achar_coluna(orc_aberto_secao, ["cliente"])
        co_valor_secao = dados.get("co_valor") or achar_coluna(orc_aberto_secao, ["valor"])
        if vendedor and vendedor != "Todas" and not orc_aberto_secao.empty:
            co_vendedor_secao = achar_coluna(orc_aberto_secao, ["vendedor"])
            if co_vendedor_secao:
                orc_aberto_secao = orc_aberto_secao[
                    orc_aberto_secao[co_vendedor_secao].astype(str).str.strip() == vendedor
                ].copy()
        if orc_aberto_secao.empty:
            st.info("Nenhum orçamento aberto para retorno neste filtro.")
        else:
            renderizar_cards_orcamentos_simples(
                orc_aberto_secao.sort_values("dias_no_sistema", ascending=False),
                co_num_secao,
                co_cli_secao,
                co_valor_secao,
                incluir_acao=True,
            )

    return

def card_cliente(row, tipo, posicao):
    atraso = int(row["dias_sem_comprar"] - row["intervalo"])
    estrela = "â­ Cliente estratÃ©gico<br>" if row["cliente_estrategico"] else ""
    cliente_html = html_seguro(row["Cliente"])
    temperatura_html = html_seguro(row["temperatura"])
    risco_html = html_seguro(row["risco_inadimplencia"])
    acao_html = html_seguro(row["acao_ia"])
    vendedor_html = html_seguro(row.get("Vendedor", "Sem vendedor"))
    motivo_html = html_seguro(row.get("motivo_prioridade", ""))

    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{cliente_html}</b><br>
{estrela}
Temperatura: <b>{temperatura_html}</b><br>
Score comercial: <b>{int(row['score_comercial'])}/100</b><br><br>
Compra a cada <b>{int(row['intervalo'])} dias</b><br>
EstÃ¡ hÃ¡ <b>{int(row['dias_sem_comprar'])} dias</b> sem comprar<br>
JÃ¡ era para ter comprado hÃ¡ <b>{max(atraso, 0)} dias</b><br><br>
Ticket mÃ©dio: <b>{fmt_html(row['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(row['potencial_mensal'])}</b><br>
Potencial recuperÃ¡vel: <b>{fmt_html(row['potencial_recuperavel'])}</b><br>
OrÃ§amentos em aberto: <b>{int(row['orcamentos_em_aberto'])}</b><br>
Vendedor responsÃ¡vel: <b>{vendedor_html}</b><br>
InadimplÃªncia: <b>{fmt_html(row['inadimplencia'])}</b><br>
Score de risco: <b>{int(row['score_risco'])}/100 â€” {risco_html}</b><br><br>
Prioridade de hoje: <b>{motivo_html or 'Acompanhamento comercial'}</b><br>
RecomendaÃ§Ã£o: <b>{acao_html}</b>
</div>
""", unsafe_allow_html=True)

    cliente_uid = chave_widget(identificador_cliente(row, posicao))
    chave_base = f"{tipo}_{cliente_uid}_{chave_widget(posicao)}"
    sufixo_uid = cliente_uid[-6:]

    with st.expander(f"Ver HistÃ³rico - {row['Cliente']} #{sufixo_uid}"):
        renderizar_historico_cliente(row)

    observacao_contato = st.text_input(
        "ObservaÃ§Ã£o do contato (opcional)",
        key=f"obs_contato_{chave_base}"
    )
    if st.button(
        f"JÃ¡ Liguei - {row['Cliente']}",
        key=f"liguei_{chave_base}",
        type="primary"
    ):
        try:
            salvar_contato_realizado(
                row.get("Cliente ID", ""),
                row["Cliente"],
                row.get("Vendedor", "Sem vendedor"),
                observacao_contato,
                tipo
            )
            st.session_state.clientes_ligados.add(row["Cliente"])
            st.success("Contato registrado. O cliente saiu das prioridades de hoje.")
            st.rerun()
        except Exception as e:
            st.error(f"NÃ£o foi possÃ­vel registrar o contato: {e}")

    with st.expander(f"Agendar Retorno - {row['Cliente']} #{sufixo_uid}"):
        data_retorno = st.date_input(
            "Data do retorno",
            value=date.today() + timedelta(days=1),
            min_value=date.today(),
            key=f"data_retorno_{chave_base}"
        )
        motivo_retorno = st.text_input(
            "Motivo do retorno",
            value="Retorno comercial",
            key=f"motivo_retorno_{chave_base}"
        )
        observacao_retorno = st.text_area(
            "ObservaÃ§Ã£o",
            key=f"obs_retorno_{chave_base}"
        )
        if st.button(
            "Salvar Retorno",
            key=f"salvar_retorno_{chave_base}"
        ):
            try:
                agendar_retorno_cliente(
                    row.get("Cliente ID", ""),
                    row["Cliente"],
                    row.get("Vendedor", "Sem vendedor"),
                    data_retorno,
                    motivo_retorno,
                    observacao_retorno
                )
                st.success(f"Retorno agendado para {data_retorno:%d/%m/%Y}.")
                st.rerun()
            except Exception as e:
                st.error(f"NÃ£o foi possÃ­vel agendar o retorno: {e}")

def gerar_texto_email(
    prioridade, orc_aberto, clientes, clientes_churn,
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
    financeiro=None, contas_pagar=None, recebido_mes=0, pago_mes=0
):
    hoje_txt = datetime.now().strftime("%d/%m/%Y")
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )
    temperaturas = clientes["temperatura"].value_counts()
    metricas_fin = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
    )

    linhas = [
        f"RESUMO COMERCIAL DIÃRIO - {hoje_txt}",
        f"PerÃ­odo das vendas analisadas: {periodo}",
        "",
        "VISÃƒO EXECUTIVA",
        f"- Faturamento histÃ³rico importado: {fmt(clientes['faturamento'].sum())}",
        f"- Potencial mensal da carteira: {fmt(clientes['potencial_mensal'].sum())}",
        f"- Capacidade estimada das prioridades de hoje: {fmt(prioridade['ticket_medio'].sum())}",
        f"- Potencial recuperÃ¡vel: {fmt(clientes['potencial_recuperavel'].sum())}",
        f"- InadimplÃªncia identificada: {fmt(clientes['inadimplencia'].sum())}",
        f"- Churn estimado: {taxa_churn:.1f}% ({qtd_churn} de {base_churn} clientes com ciclo conhecido)",
        "",
        "FINANCEIRO",
        f"- Carteira a receber: {fmt(metricas_fin['total_aberto'])}",
        f"- Total vencido: {fmt(metricas_fin['total_vencido'])} ({metricas_fin['percentual_vencido']:.1f}%)",
        f"- Entradas previstas em atÃ© 7 dias: {fmt(metricas_fin['vence_7'])}",
        f"- Entradas previstas de 8 a 15 dias: {fmt(metricas_fin['vence_15'])}",
        f"- Entradas previstas de 16 a 30 dias: {fmt(metricas_fin['vence_30'])}",
        f"- ConcentraÃ§Ã£o nos 5 maiores clientes: {metricas_fin['concentracao_top5']:.1f}%",
        f"- Contas a pagar: {fmt(metricas_fin['total_pagar'])}",
        f"- Saldo total projetado: {fmt(metricas_fin['saldo_carteira'])}",
        f"- Sobra projetada em 30 dias: {fmt(metricas_fin['saldo_30_dias'])}",
        f"- Resultado financeiro do mÃªs: {fmt(metricas_fin['resultado_mes'])}",
        "",
        "CARTEIRA",
        f"- Quentes: {int(temperaturas.get('ðŸŸ¢ QUENTE', 0))}",
        f"- Em atenÃ§Ã£o: {int(temperaturas.get('ðŸŸ¡ ATENÃ‡ÃƒO', 0))}",
        f"- Atrasados na recompra: {int(temperaturas.get('ðŸ”´ ATRASADO NA RECOMPRA', 0))}",
        f"- Inativos: {int(temperaturas.get('âš« CLIENTE INATIVO', 0))}",
        "",
        f"PRIORIDADES DE HOJE ({len(prioridade)})"
    ]

    if prioridade.empty:
        linhas.append("- Nenhum cliente no timing ideal.")
    else:
        for i, (_, r) in enumerate(prioridade.head(10).iterrows(), 1):
            linhas.append(
                f"{i}. {r['Cliente']} | Ticket {fmt(r['ticket_medio'])} | "
                f"Potencial {fmt(r['potencial_mensal'])} | {r['acao_ia']}"
            )

    linhas.extend(["", f"ORÃ‡AMENTOS URGENTES ({len(orc_urgentes)})"])
    if orc_urgentes.empty:
        linhas.append("- Nenhum orÃ§amento com dois dias ou mais sem retorno.")
    else:
        for i, (_, r) in enumerate(orc_urgentes.head(10).iterrows(), 1):
            valor = fmt(r[co_valor]) if co_valor else "valor nÃ£o informado"
            linhas.append(
                f"{i}. NÂº {r[co_num]} | {r[co_cli]} | {int(r['dias_no_sistema'])} dias | {valor}"
            )

    linhas.extend(["", f"CHURN PARA RECUPERAÃ‡ÃƒO ({len(clientes_churn)})"])
    if clientes_churn.empty:
        linhas.append("- Nenhum cliente classificado em churn.")
    else:
        for i, (_, r) in enumerate(clientes_churn.head(10).iterrows(), 1):
            linhas.append(
                f"{i}. {r['Cliente']} | {int(r['dias_sem_comprar'])} dias sem comprar | "
                f"Potencial em risco {fmt(r['potencial_mensal'])}"
            )

    linhas.extend([
        "",
        "PLANO DO DIA",
        f"- Realizar {len(prioridade)} contatos prioritÃ¡rios.",
        f"- Retornar {len(orc_urgentes)} orÃ§amentos urgentes.",
        f"- Iniciar recuperaÃ§Ã£o dos {min(len(clientes_churn), 10)} clientes de churn com maior potencial.",
        "- Tratar inadimplÃªncia antes de oferecer nova venda aos clientes com pendÃªncias.",
        "",
        "ObservaÃ§Ã£o: capacidade estimada nÃ£o Ã© previsÃ£o garantida; representa a soma dos tickets mÃ©dios das prioridades."
    ])
    return "\n".join(linhas)

def gerar_pdf(
    prioridade, orc_aberto, clientes, clientes_churn,
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
    financeiro=None, contas_pagar=None, recebido_mes=0, pago_mes=0
):
    if not REPORTLAB_OK:
        return None

    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )
    metricas_fin = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=16 * mm,
        title="RelatÃ³rio Comercial Executivo"
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TituloCEO", parent=styles["Title"], fontSize=20, leading=24,
        textColor=colors.HexColor("#17324D"), alignment=TA_CENTER, spaceAfter=6
    ))
    styles.add(ParagraphStyle(
        name="SecaoCEO", parent=styles["Heading1"], fontSize=13, leading=16,
        textColor=colors.HexColor("#17324D"), spaceBefore=10, spaceAfter=7
    ))
    styles.add(ParagraphStyle(
        name="Pequeno", parent=styles["BodyText"], fontSize=8, leading=10
    ))
    styles.add(ParagraphStyle(
        name="CabecalhoTabela", parent=styles["Pequeno"],
        textColor=colors.white, fontName="Helvetica-Bold"
    ))
    elementos = []

    def p(valor, estilo="Pequeno"):
        texto = re.sub(r"[\U00010000-\U0010ffff]", "", str(valor)).strip()
        return Paragraph(escape(texto), styles[estilo])

    def tabela(dados, larguras=None):
        cabecalho = [
            Paragraph(escape(celula.getPlainText()), styles["CabecalhoTabela"])
            if isinstance(celula, Paragraph)
            else Paragraph(escape(str(celula)), styles["CabecalhoTabela"])
            for celula in dados[0]
        ]
        dados = [cabecalho] + dados[1:]
        tabela_pdf = Table(dados, colWidths=larguras, repeatRows=1, hAlign="LEFT")
        tabela_pdf.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C2CC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return tabela_pdf

    elementos.append(Paragraph("RELATÃ“RIO COMERCIAL EXECUTIVO", styles["TituloCEO"]))
    elementos.append(Paragraph(
        f"Emitido em {datetime.now():%d/%m/%Y} | Vendas analisadas: {periodo}",
        styles["Normal"]
    ))
    elementos.append(Spacer(1, 8))

    indicadores = [
        [p("Indicador"), p("Resultado"), p("Leitura")],
        [p("Faturamento histÃ³rico"), p(fmt(clientes["faturamento"].sum())), p("Total existente no arquivo importado.")],
        [p("Potencial mensal"), p(fmt(clientes["potencial_mensal"].sum())), p("MÃ©dia mensal das compras dos Ãºltimos trÃªs meses.")],
        [p("Capacidade das prioridades"), p(fmt(prioridade["ticket_medio"].sum())), p("Soma dos tickets mÃ©dios; nÃ£o Ã© previsÃ£o garantida.")],
        [p("Potencial recuperÃ¡vel"), p(fmt(clientes["potencial_recuperavel"].sum())), p("Potencial de atrasados e inativos.")],
        [p("InadimplÃªncia"), p(fmt(clientes["inadimplencia"].sum())), p("PendÃªncias identificadas no contas a receber.")],
        [p("Carteira a receber"), p(fmt(metricas_fin["total_aberto"])), p("Total de recebimentos ainda em aberto.")],
        [p("Percentual vencido"), p(f"{metricas_fin['percentual_vencido']:.1f}%"), p("ParticipaÃ§Ã£o dos tÃ­tulos vencidos na carteira aberta.")],
        [p("Receber em atÃ© 7 dias"), p(fmt(metricas_fin["vence_7"])), p("Entradas previstas no curto prazo.")],
        [p("Contas a pagar"), p(fmt(metricas_fin["total_pagar"])), p("ObrigaÃ§Ãµes ainda em aberto.")],
        [p("Sobra em 30 dias"), p(fmt(metricas_fin["saldo_30_dias"])), p("Entradas previstas menos saÃ­das previstas.")],
        [p("Resultado financeiro mensal"), p(fmt(metricas_fin["resultado_mes"])), p("Recebimentos liquidados menos pagamentos liquidados.")],
        [p("Churn estimado"), p(f"{taxa_churn:.1f}%"), p(f"{qtd_churn} de {base_churn} clientes com ciclo conhecido.")],
    ]
    elementos.append(Paragraph("1. Painel executivo", styles["SecaoCEO"]))
    elementos.append(tabela(indicadores, [45 * mm, 35 * mm, 80 * mm]))

    temperaturas = clientes["temperatura"].value_counts()
    carteira = [
        [p("SituaÃ§Ã£o"), p("Clientes")],
        [p("Quentes"), p(int(temperaturas.get("ðŸŸ¢ QUENTE", 0)))],
        [p("Em atenÃ§Ã£o"), p(int(temperaturas.get("ðŸŸ¡ ATENÃ‡ÃƒO", 0)))],
        [p("Atrasados na recompra"), p(int(temperaturas.get("ðŸ”´ ATRASADO NA RECOMPRA", 0)))],
        [p("Inativos"), p(int(temperaturas.get("âš« CLIENTE INATIVO", 0)))],
        [p("Novos"), p(int(temperaturas.get("ðŸŸ£ NOVO", 0)))],
    ]
    elementos.append(Paragraph("2. SituaÃ§Ã£o da carteira", styles["SecaoCEO"]))
    elementos.append(tabela(carteira, [80 * mm, 35 * mm]))

    elementos.append(Paragraph("3. Prioridades comerciais", styles["SecaoCEO"]))
    prioridades_pdf = [[p("Cliente"), p("Dias"), p("Ticket"), p("Potencial"), p("RecomendaÃ§Ã£o")]]
    for _, r in prioridade.head(20).iterrows():
        prioridades_pdf.append([
            p(r["Cliente"]), p(int(r["dias_sem_comprar"])), p(fmt(r["ticket_medio"])),
            p(fmt(r["potencial_mensal"])), p(r["acao_ia"])
        ])
    if len(prioridades_pdf) == 1:
        elementos.append(Paragraph("Nenhum cliente no timing ideal hoje.", styles["Normal"]))
    else:
        elementos.append(tabela(prioridades_pdf, [38 * mm, 14 * mm, 25 * mm, 27 * mm, 56 * mm]))

    elementos.append(PageBreak())
    elementos.append(Paragraph("4. Churn e receita em risco", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        f"Taxa estimada: <b>{taxa_churn:.1f}%</b>. Um cliente entra em churn quando "
        "possui ciclo de recompra conhecido e ultrapassa duas vezes seu intervalo mÃ©dio sem comprar.",
        styles["BodyText"]
    ))
    elementos.append(Spacer(1, 6))
    churn_pdf = [[p("Cliente"), p("Sem comprar"), p("Ciclo"), p("AlÃ©m do limite"), p("Potencial em risco")]]
    for _, r in clientes_churn.head(25).iterrows():
        churn_pdf.append([
            p(r["Cliente"]), p(f"{int(r['dias_sem_comprar'])} dias"),
            p(f"{int(r['intervalo'])} dias"), p(f"{int(r['dias_alem_limite'])} dias"),
            p(fmt(r["potencial_mensal"]))
        ])
    if len(churn_pdf) == 1:
        elementos.append(Paragraph("Nenhum cliente classificado em churn.", styles["Normal"]))
    else:
        elementos.append(tabela(churn_pdf, [48 * mm, 27 * mm, 24 * mm, 30 * mm, 31 * mm]))

    elementos.append(Paragraph("5. OrÃ§amentos que exigem retorno", styles["SecaoCEO"]))
    orc_pdf = [[p("OrÃ§amento"), p("Cliente"), p("Dias"), p("Valor"), p("Prioridade")]]
    for _, r in orc_urgentes.head(25).iterrows():
        orc_pdf.append([
            p(r[co_num]), p(r[co_cli]), p(int(r["dias_no_sistema"])),
            p(fmt(r[co_valor]) if co_valor else "NÃ£o informado"), p(r["acao_recomendada_orcamento"])
        ])
    if len(orc_pdf) == 1:
        elementos.append(Paragraph("Nenhum orÃ§amento urgente.", styles["Normal"]))
    else:
        elementos.append(tabela(orc_pdf, [25 * mm, 50 * mm, 15 * mm, 28 * mm, 42 * mm]))

    inadimplentes = clientes[clientes["inadimplencia"] > 0].sort_values(
        "inadimplencia", ascending=False
    )
    elementos.append(Paragraph("6. InadimplÃªncia por cliente", styles["SecaoCEO"]))
    inad_pdf = [[p("Cliente"), p("Valor"), p("MÃ©dia de atraso"), p("Risco")]]
    for _, r in inadimplentes.head(25).iterrows():
        inad_pdf.append([
            p(r["Cliente"]), p(fmt(r["inadimplencia"])),
            p(f"{int(r['media_dias_atraso'])} dias"), p(r["risco_inadimplencia"])
        ])
    if len(inad_pdf) == 1:
        elementos.append(Paragraph("Nenhuma inadimplÃªncia identificada.", styles["Normal"]))
    else:
        elementos.append(tabela(inad_pdf, [55 * mm, 32 * mm, 32 * mm, 41 * mm]))

    elementos.append(Paragraph("7. Carteira financeira", styles["SecaoCEO"]))
    if financeiro is None or financeiro.empty:
        elementos.append(Paragraph("Nenhum recebimento em aberto identificado.", styles["Normal"]))
    else:
        fin_clientes = (
            financeiro.groupby("Cliente")["Valor"].sum()
            .sort_values(ascending=False)
            .head(15)
        )
        fin_pdf = [[p("Cliente"), p("Total a receber"), p("% da carteira")]]
        for cliente, valor in fin_clientes.items():
            participacao = (
                float(valor) / metricas_fin["total_aberto"] * 100
                if metricas_fin["total_aberto"] else 0
            )
            fin_pdf.append([
                p(cliente), p(fmt(valor)), p(f"{participacao:.1f}%")
            ])
        elementos.append(tabela(fin_pdf, [80 * mm, 42 * mm, 38 * mm]))

    elementos.append(Paragraph("8. Contas a pagar por fornecedor", styles["SecaoCEO"]))
    if contas_pagar is None or contas_pagar.empty:
        elementos.append(Paragraph("Nenhuma conta a pagar em aberto identificada.", styles["Normal"]))
    else:
        pagar_fornecedor = (
            contas_pagar.groupby("Fornecedor")["Valor"].sum()
            .sort_values(ascending=False)
            .head(15)
        )
        fornecedores_pdf = [[p("Fornecedor"), p("Total a pagar"), p("% das obrigaÃ§Ãµes")]]
        for fornecedor, valor in pagar_fornecedor.items():
            participacao = (
                float(valor) / metricas_fin["total_pagar"] * 100
                if metricas_fin["total_pagar"] else 0
            )
            fornecedores_pdf.append([
                p(fornecedor), p(fmt(valor)), p(f"{participacao:.1f}%")
            ])
        elementos.append(tabela(fornecedores_pdf, [80 * mm, 42 * mm, 38 * mm]))

    elementos.append(Paragraph("9. AnÃ¡lise e plano de aÃ§Ã£o", styles["SecaoCEO"]))
    dicas_financeiras = estrategia_financeira(metricas_fin)
    elementos.append(Paragraph(
        f"<b>Hoje:</b> realizar {len(prioridade)} contatos prioritÃ¡rios e retornar "
        f"{len(orc_urgentes)} orÃ§amentos urgentes.<br/>"
        f"<b>PrÃ³ximos 7 dias:</b> acompanhar clientes em atenÃ§Ã£o e propostas ainda abertas.<br/>"
        f"<b>RecuperaÃ§Ã£o:</b> abordar primeiro os {min(len(clientes_churn), 10)} clientes "
        "em churn com maior potencial mensal e tratar pendÃªncias financeiras antes de uma nova oferta.<br/>"
        f"<b>Financeiro:</b> {' '.join(dicas_financeiras)}",
        styles["BodyText"]
    ))

    elementos.append(Paragraph("10. Metodologia", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        "<b>Churn estimado:</b> clientes com ciclo conhecido e mais de duas vezes o intervalo "
        "mÃ©dio sem comprar, dividido pela quantidade de clientes com ciclo conhecido.<br/>"
        "<b>Potencial mensal:</b> compras dos Ãºltimos trÃªs meses divididas por trÃªs.<br/>"
        "<b>Capacidade das prioridades:</b> soma dos tickets mÃ©dios dos clientes quentes; "
        "nÃ£o representa promessa de venda.<br/>"
        "<b>Percentual vencido:</b> valor vencido dividido pela carteira total ainda em aberto.<br/>"
        "<b>ConcentraÃ§Ã£o:</b> participaÃ§Ã£o dos cinco maiores clientes no total a receber.<br/>"
        "<b>Resultado financeiro mensal:</b> recebimentos liquidados menos pagamentos liquidados; "
        "nÃ£o equivale necessariamente ao lucro contÃ¡bil.<br/>"
        "<b>Cliente estratÃ©gico:</b> cliente situado entre os 10% de maior faturamento histÃ³rico.",
        styles["BodyText"]
    ))

    def rodape(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#667788"))
        canvas.drawString(15 * mm, 9 * mm, "CRM Inteligente - RelatÃ³rio Comercial")
        canvas.drawRightString(195 * mm, 9 * mm, f"PÃ¡gina {documento.page}")
        canvas.restoreState()

    doc.build(elementos, onFirstPage=rodape, onLaterPages=rodape)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf

def renderizar_financeiro_ceo(
    financeiro, contas_pagar, recebido_mes, pago_mes,
    mes_resultado, resultado_disponivel, clientes, clientes_churn
):
    st.subheader("Financeiro CEO")
    st.caption(
        "VisÃ£o estratÃ©gica da carteira de recebimentos em aberto. "
        "Os valores representam entradas previstas, nÃ£o saldo bancÃ¡rio disponÃ­vel."
    )
    metricas = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
    )

    linha1 = st.columns(4)
    linha1[0].metric("Carteira a receber", fmt(metricas["total_aberto"]))
    linha1[1].metric("Contas a pagar", fmt(metricas["total_pagar"]))
    linha1[2].metric(
        "Saldo total projetado",
        fmt(metricas["saldo_carteira"]),
        "Receber menos pagar"
    )
    if resultado_disponivel:
        linha1[3].metric(
            f"Resultado financeiro {mes_resultado}",
            fmt(metricas["resultado_mes"]),
            "Lucro" if metricas["resultado_mes"] >= 0 else "PrejuÃ­zo",
            delta_color="normal"
        )
    else:
        linha1[3].metric("Resultado financeiro mensal", "IndisponÃ­vel")

    linha2 = st.columns(4)
    linha2[0].metric(
        "Total vencido",
        fmt(metricas["total_vencido"]),
        f"{metricas['percentual_vencido']:.1f}% da carteira"
    )
    linha2[1].metric("Contas a pagar vencidas", fmt(metricas["pagar_vencido"]))
    linha2[2].metric("Sobra projetada em 30 dias", fmt(metricas["saldo_30_dias"]))
    linha2[3].metric(
        "Margem financeira do mÃªs",
        f"{metricas['margem_caixa']:.1f}%"
    )

    with st.expander("Como o resultado e a sobra sÃ£o calculados?"):
        st.markdown(
            f"""
            **Resultado financeiro de {mes_resultado}**

            `Recebimentos liquidados - pagamentos liquidados`

            {fmt(metricas['recebido_mes'])} - {fmt(metricas['pago_mes'])}
            = **{fmt(metricas['resultado_mes']) if resultado_disponivel else 'IndisponÃ­vel no modo Excel'}**

            **Sobra projetada em 30 dias**

            `Contas a receber nos prÃ³ximos 30 dias - contas a pagar nos prÃ³ximos 30 dias`

            Este resultado Ã© uma visÃ£o de caixa. NÃ£o inclui automaticamente estoque,
            depreciaÃ§Ã£o, impostos provisionados ou despesas que ainda nÃ£o foram lanÃ§adas.
            """
        )

    linha3 = st.columns(4)
    linha3[0].metric("Receber em atÃ© 7 dias", fmt(metricas["vence_7"]))
    linha3[1].metric("Pagar em atÃ© 7 dias", fmt(metricas["pagar_7"]))
    linha3[2].metric("Prazo mÃ©dio a receber", f"{metricas['prazo_medio']:.0f} dias")
    linha3[3].metric("ConcentraÃ§Ã£o nos 5 maiores", f"{metricas['concentracao_top5']:.1f}%")

    if financeiro is None or financeiro.empty:
        st.info("Nenhuma conta em aberto foi encontrada para montar a visÃ£o financeira.")
        if contas_pagar is None or contas_pagar.empty:
            return

    potencial_churn = float(clientes_churn["potencial_mensal"].sum())
    receita_em_risco = metricas["total_vencido"] + potencial_churn
    st.metric(
        "ExposiÃ§Ã£o estratÃ©gica estimada",
        fmt(receita_em_risco),
        help=(
            "Soma do valor vencido com o potencial mensal dos clientes em churn. "
            "Ã‰ um indicador de exposiÃ§Ã£o, nÃ£o uma perda contÃ¡bil confirmada."
        )
    )

    st.markdown("#### Alertas estratÃ©gicos")
    alertas = []
    if metricas["percentual_vencido"] >= 25:
        alertas.append(
            f"CRÃTICO: {metricas['percentual_vencido']:.1f}% da carteira estÃ¡ vencida."
        )
    elif metricas["percentual_vencido"] >= 10:
        alertas.append(
            f"ATENÃ‡ÃƒO: {metricas['percentual_vencido']:.1f}% da carteira estÃ¡ vencida."
        )
    if metricas["concentracao_top5"] >= 50:
        alertas.append(
            "A carteira estÃ¡ concentrada: os cinco maiores clientes representam "
            f"{metricas['concentracao_top5']:.1f}% do total a receber."
        )
    vencido_60 = float(financeiro.loc[
        financeiro["Dias_atraso"] > 60, "Valor"
    ].sum())
    if vencido_60 > 0:
        alertas.append(
            f"Existem {fmt(vencido_60)} vencidos hÃ¡ mais de 60 dias."
        )
    if metricas["vence_7"] > 0:
        alertas.append(
            f"HÃ¡ {fmt(metricas['vence_7'])} previstos para entrar nos prÃ³ximos 7 dias."
        )
    if metricas["saldo_30_dias"] < 0:
        alertas.append(
            f"DÃ©ficit projetado de {fmt(abs(metricas['saldo_30_dias']))} "
            "para os prÃ³ximos 30 dias."
        )
    if not alertas:
        st.success("Nenhum alerta financeiro relevante pelos critÃ©rios atuais.")
    else:
        for alerta in alertas:
            st.warning(alerta)

    col_aging, col_fluxo = st.columns(2)
    ordem_faixas = [
        "Vencido acima de 60 dias",
        "Vencido de 31 a 60 dias",
        "Vencido de 16 a 30 dias",
        "Vencido de 8 a 15 dias",
        "Vencido atÃ© 7 dias",
        "A vencer em atÃ© 7 dias",
        "A vencer de 8 a 15 dias",
        "A vencer de 16 a 30 dias",
        "A vencer de 31 a 60 dias",
        "A vencer acima de 60 dias",
    ]
    with col_aging:
        st.markdown("#### Carteira por faixa de vencimento")
        aging = (
            financeiro.groupby("Faixa")["Valor"].sum()
            .reindex(ordem_faixas, fill_value=0)
        )
        st.bar_chart(aging)

    with col_fluxo:
        st.markdown("#### Entradas previstas por mÃªs")
        futuro = financeiro[~financeiro["Vencida"]].copy()
        if futuro.empty:
            st.info("NÃ£o hÃ¡ recebimentos futuros na carteira consultada.")
        else:
            futuro["MÃªs"] = futuro["Vencimento"].dt.strftime("%m/%Y")
            fluxo = futuro.groupby("MÃªs", sort=False)["Valor"].sum()
            st.bar_chart(fluxo)

    st.markdown("#### Maiores clientes na carteira")
    ranking = (
        financeiro.groupby("Cliente")
        .agg(
            Total=("Valor", "sum"),
            Vencido=("Valor", lambda s: s[financeiro.loc[s.index, "Vencida"]].sum()),
            Titulos=("Valor", "count"),
            Maior_atraso=("Dias_atraso", "max"),
        )
        .sort_values("Total", ascending=False)
        .head(20)
        .reset_index()
    )
    ranking["Total"] = ranking["Total"].map(fmt)
    ranking["Vencido"] = ranking["Vencido"].map(fmt)
    ranking = ranking.rename(columns={
        "Titulos": "TÃ­tulos",
        "Maior_atraso": "Maior atraso (dias)"
    })
    st.dataframe(ranking, use_container_width=True, hide_index=True)

    st.markdown("#### Contas a pagar por fornecedor")
    if contas_pagar is None or contas_pagar.empty:
        st.info(
            "Contas a pagar nÃ£o estÃ£o disponÃ­veis. No modo API, atualize os dados; "
            "no modo Excel, seria necessÃ¡rio um quarto arquivo de contas a pagar."
        )
    else:
        fornecedores = (
            contas_pagar.groupby("Fornecedor")
            .agg(
                Total=("Valor", "sum"),
                Vencido=("Valor", lambda s: s[contas_pagar.loc[s.index, "Vencida"]].sum()),
                Titulos=("Valor", "count"),
                Proximo_vencimento=("Vencimento", "min"),
            )
            .sort_values("Total", ascending=False)
            .reset_index()
        )
        fornecedores["Total"] = fornecedores["Total"].map(fmt)
        fornecedores["Vencido"] = fornecedores["Vencido"].map(fmt)
        fornecedores["Proximo_vencimento"] = fornecedores[
            "Proximo_vencimento"
        ].dt.strftime("%d/%m/%Y")
        fornecedores = fornecedores.rename(columns={
            "Titulos": "TÃ­tulos",
            "Proximo_vencimento": "PrÃ³ximo vencimento"
        })
        st.dataframe(
            fornecedores.head(25), use_container_width=True, hide_index=True
        )

        st.markdown("#### Agenda de pagamentos")
        agenda = contas_pagar[[
            "Fornecedor", "Descricao", "Vencimento", "Valor",
            "Situacao", "Dias_para_vencer"
        ]].head(30).copy()
        agenda["Vencimento"] = agenda["Vencimento"].dt.strftime("%d/%m/%Y")
        agenda["Valor"] = agenda["Valor"].map(fmt)
        agenda = agenda.rename(columns={
            "Descricao": "DescriÃ§Ã£o",
            "Situacao": "SituaÃ§Ã£o",
            "Dias_para_vencer": "Dias para vencer"
        })
        st.dataframe(agenda, use_container_width=True, hide_index=True)

    st.markdown("#### AnÃ¡lise e estratÃ©gia financeira")
    if not resultado_disponivel:
        st.info(
            "O lucro ou prejuÃ­zo mensal exige os movimentos liquidados de recebimentos "
            "e pagamentos. Esse cÃ¡lculo fica disponÃ­vel automaticamente pelo modo API."
        )
    elif metricas["resultado_mes"] > 0:
        st.success(
            f"HÃ¡ lucro financeiro de {fmt(metricas['resultado_mes'])} em "
            f"{mes_resultado}."
        )
    elif metricas["resultado_mes"] < 0:
        st.error(
            f"HÃ¡ prejuÃ­zo financeiro de {fmt(abs(metricas['resultado_mes']))} em "
            f"{mes_resultado}."
        )
    else:
        st.warning(f"O resultado financeiro de {mes_resultado} estÃ¡ equilibrado.")
    if resultado_disponivel:
        for dica in estrategia_financeira(metricas):
            st.write(f"- {dica}")

def renderizar_financeiro_real(dados):
    configuracao = dados.get("configuracao", {})
    real = calcular_financeiro_real(dados, configuracao)
    st.markdown("---")
    st.subheader("Resultado econÃ´mico e cenÃ¡rios")
    if not real:
        st.info(
            "Os dados desta sessÃ£o foram carregados por uma versÃ£o anterior. "
            "Clique em 'Atualizar dados do GestÃ£oClick' para calcular custos, "
            "margens e resultado econÃ´mico."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Receita do mÃªs", fmt(real["receita_mes"]))
    cols[1].metric("Custo das vendas", fmt(real["custo_mes"]))
    cols[2].metric("Lucro bruto", fmt(real["lucro_bruto"]))
    cols[3].metric("Margem bruta", f"{real['margem_bruta']:.1f}%")
    cols2 = st.columns(4)
    cols2[0].metric("Impostos estimados", fmt(real["impostos_estimados"]))
    cols2[1].metric("Folha + despesas", fmt(
        real["folha"] + real["despesas_fixas"] + real["outras_despesas"]
    ))
    cols2[2].metric(
        "Lucro operacional estimado",
        fmt(real["lucro_operacional"]),
        "Lucro" if real["lucro_operacional"] >= 0 else "PrejuÃ­zo"
    )
    cols2[3].metric("Margem operacional", f"{real['margem_operacional']:.1f}%")
    if not real["custos_disponiveis"]:
        st.warning(
            "Os custos das vendas nÃ£o estÃ£o preenchidos na API. O lucro bruto e "
            "operacional podem estar superestimados."
        )
    st.markdown("#### CenÃ¡rios de caixa")
    cenarios = pd.DataFrame([
        {"CenÃ¡rio": nome, "Caixa projetado": valor}
        for nome, valor in real["cenarios"].items()
    ])
    cenarios["Caixa projetado"] = cenarios["Caixa projetado"].map(fmt)
    st.dataframe(cenarios, use_container_width=True, hide_index=True)
    st.caption(
        "Os cenÃ¡rios consideram 70%, 90% ou 100% da carteira a receber, "
        "menos todas as contas a pagar registradas."
    )

def renderizar_gestao_comercial(dados):
    indicadores, vendedores = calcular_gestao_comercial(
        dados, dados.get("configuracao", {})
    )
    st.subheader("GestÃ£o Comercial")
    if not indicadores:
        st.info(
            "Os dados desta sessÃ£o foram carregados por uma versÃ£o anterior. "
            "Clique em 'Atualizar dados do GestÃ£oClick' para calcular metas, "
            "margens e desempenho por vendedor."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Meta geral", fmt(indicadores["meta_geral"]))
    cols[1].metric("Realizado no mÃªs", fmt(indicadores["realizado"]))
    cols[2].metric("ProjeÃ§Ã£o de fechamento", fmt(indicadores["projecao"]))
    cols[3].metric("DistÃ¢ncia da meta", fmt(indicadores["distancia_meta"]))
    if indicadores.get("ciclo_meta_inicio") is not None:
        st.caption(
            f"Meta calculada no ciclo comercial de "
            f"{indicadores['ciclo_meta_inicio']:%d/%m/%Y} a "
            f"{indicadores['ciclo_meta_fim']:%d/%m/%Y}."
        )
    cols2 = st.columns(3)
    cols2[0].metric(
        "ConversÃ£o de orÃ§amentos",
        f"{indicadores['conversao_orcamentos']:.1f}%"
    )
    cols2[1].metric("OrÃ§amentos analisados", indicadores["orcamentos_total"])
    cols2[2].metric(
        "Idade mÃ©dia dos abertos",
        f"{indicadores['idade_media_abertos']:.0f} dias"
    )
    st.caption(
        "A conversÃ£o usa as situaÃ§Ãµes dos orÃ§amentos. Sem vÃ­nculo direto entre "
        "orÃ§amento e venda, o tempo exato atÃ© fechamento nÃ£o pode ser afirmado."
    )
    if not vendedores.empty:
        exibir = vendedores.copy()
        for col in ("Faturamento", "Custo", "Margem", "Meta", "Distancia_meta"):
            exibir[col] = exibir[col].map(fmt)
        exibir["Ticket_medio"] = exibir["Ticket_medio"].map(fmt)
        exibir["Margem_pct"] = exibir["Margem_pct"].map(lambda v: f"{v:.1f}%")
        exibir["Atingimento_pct"] = exibir["Atingimento_pct"].map(
            lambda v: f"{v:.1f}%"
        )
        exibir = exibir.rename(columns={
            "Ticket_medio": "Ticket mÃ©dio",
            "Margem_pct": "Margem %",
            "Atingimento_pct": "Atingimento %",
            "Distancia_meta": "DistÃ¢ncia da meta",
        })
        st.dataframe(exibir, use_container_width=True, hide_index=True)
    st.markdown("#### Motivos de perda")
    if indicadores["motivos_perda"].empty:
        st.info(
            "Nenhum motivo de perda foi encontrado nas observaÃ§Ãµes dos orÃ§amentos."
        )
    else:
        st.dataframe(
            indicadores["motivos_perda"],
            use_container_width=True,
            hide_index=True
        )

def renderizar_qualidade_dados(dados):
    qualidade = dados.get("qualidade_dados", {})
    clientes = dados.get("clientes", pd.DataFrame()).copy()
    st.subheader("Qualidade dos Dados")
    cols = st.columns(5)
    cols[0].metric("Vendas excluÃ­das", qualidade.get("vendas_canceladas", 0))
    cols[1].metric("Sem cliente ID", qualidade.get("vendas_sem_cliente_id", 0))
    cols[2].metric("Nomes duplicados", qualidade.get("clientes_nomes_duplicados", 0))
    cols[3].metric("Sem custo", qualidade.get("vendas_sem_custo", 0))
    cols[4].metric("Sem vendedor", qualidade.get("vendas_sem_vendedor", 0))
    problemas = sum(int(v) for v in qualidade.values())
    if problemas:
        st.warning(
            "HÃ¡ registros que podem reduzir a precisÃ£o dos indicadores. "
            "Vendas canceladas e devolvidas foram excluÃ­das automaticamente."
        )
    else:
        st.success("Nenhum problema relevante foi detectado na base consultada.")
    st.markdown(
        """
        **Regras aplicadas**

        - clientes sÃ£o consolidados por `cliente_id`; o nome Ã© apenas para exibiÃ§Ã£o;
        - vendas canceladas, devolvidas, estornadas, reprovadas ou perdidas sÃ£o excluÃ­das;
        - registros duplicados da API sÃ£o removidos pelo ID;
        - custos, vendedor e identificaÃ§Ã£o ausentes sÃ£o sinalizados;
        - contas futuras nÃ£o entram na inadimplÃªncia antes do vencimento.
        """
    )

    st.markdown("#### Clientes ativos com pendÃªncias")
    if clientes.empty or "inadimplencia" not in clientes.columns:
        st.info(
            "Atualize os dados do GestÃ£oClick para analisar clientes com pendÃªncias."
        )
        return
    ativos_inadimplentes = clientes[
        clientes["inadimplencia"] > 0
    ].sort_values("inadimplencia", ascending=False).copy()
    if ativos_inadimplentes.empty:
        st.success("Nenhum cliente da carteira comercial possui pendÃªncia identificada.")
    else:
        tabela_ativos = ativos_inadimplentes[[
            "Cliente", "ultima_compra", "inadimplencia",
            "media_dias_atraso", "temperatura"
        ]].head(20).copy()
        tabela_ativos["ultima_compra"] = tabela_ativos["ultima_compra"].dt.strftime("%d/%m/%Y")
        tabela_ativos["inadimplencia"] = tabela_ativos["inadimplencia"].map(fmt)
        tabela_ativos = tabela_ativos.rename(columns={
            "ultima_compra": "Ãšltima compra",
            "inadimplencia": "Valor vencido",
            "media_dias_atraso": "MÃ©dia de atraso",
            "temperatura": "SituaÃ§Ã£o comercial",
        })
        st.dataframe(tabela_ativos, use_container_width=True, hide_index=True)

def renderizar_card_metric(coluna, titulo, valor, detalhe="", ajuda=None):
    coluna.metric(titulo, valor, detalhe, help=ajuda)

def renderizar_cards_orcamentos_simples(orcamentos, co_num, co_cli, co_valor, incluir_acao=False):
    cards = list(orcamentos.head(24).iterrows())
    dados_app = st.session_state.get("dados_processados") or {}
    for i in range(0, len(cards), 3):
        cols = st.columns(3)
        for j, (indice, r) in enumerate(cards[i:i+3]):
            with cols[j]:
                numero = html_seguro(r.get(co_num, ""))
                cliente = html_seguro(r.get(co_cli, "Cliente sem nome"))
                valor = fmt_html(r.get(co_valor, 0)) if co_valor else "Sem valor"
                dias = int(r.get("dias_no_sistema", 0) or 0)
                acao = html_seguro(r.get("acao_recomendada_orcamento", "")) if incluir_acao else ""
                linha_acao = f"<br>AÃ§Ã£o: <b>{acao}</b>" if acao else ""
                orcamento_id = texto_valido(r.get("_orcamento_id", ""))
                uid = chave_widget(orcamento_id or f"{r.get(co_num, '')}_{indice}_{i}_{j}")
                st.markdown(f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:10px;">
<b>OrÃ§amento #{numero}</b><br>
Cliente: <b>{cliente}</b><br>
Valor: <b>{valor}</b><br>
Tempo no sistema: <b>{dias} dia(s)</b>{linha_acao}
</div>
""", unsafe_allow_html=True)
                if dados_app.get("origem") == "api" and orcamento_id:
                    with st.expander("Registrar / concretizar"):
                        obs = st.text_area(
                            "Observação no orçamento",
                            key=f"orc_obs_simples_{uid}",
                            height=90,
                        )
                        if st.button("Salvar observação no GestãoClick", key=f"orc_obs_btn_{uid}", use_container_width=True):
                            try:
                                if not obs.strip():
                                    raise RuntimeError("Digite a observação antes de salvar.")
                                api_gestaoclick().append_budget_note(
                                    orcamento_id,
                                    dados_app["loja_id"],
                                    obs,
                                    st.session_state.get("gc_usuario_nome", USUARIO_PADRAO),
                                )
                                st.success("Observação gravada no orçamento.")
                            except Exception as e:
                                st.error(f"Não foi possível gravar no GestãoClick: {e}")
                        try:
                            status = api_gestaoclick().budget_statuses(dados_app["loja_id"])
                        except Exception:
                            status = []
                        candidatos = [
                            s for s in status
                            if re.search("CONCRET|APROV|VEND|FECH|FATUR", str(s.get("nome") or s.get("descricao") or "").upper())
                        ]
                        if candidatos:
                            escolhido = st.selectbox(
                                "Status para concretizar",
                                candidatos,
                                format_func=lambda s: str(s.get("nome") or s.get("descricao") or s.get("id")),
                                key=f"orc_status_{uid}",
                            )
                            ok = st.checkbox("Confirmo concretizar este orçamento no GestãoClick.", key=f"orc_conf_{uid}")
                            if st.button("Concretizar orçamento", key=f"orc_conc_{uid}", disabled=not ok, type="primary", use_container_width=True):
                                try:
                                    api_gestaoclick().update_budget_status(
                                        orcamento_id,
                                        dados_app["loja_id"],
                                        escolhido.get("id"),
                                    )
                                    st.success("Orçamento concretizado no GestãoClick.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Não foi possível concretizar: {e}")
                        else:
                            st.caption("Não encontrei status de concretização na API para esta loja.")

def renderizar_cards_clientes_simples(clientes_df):
    cards = list(clientes_df.head(24).iterrows())
    for i in range(0, len(cards), 3):
        cols = st.columns(3)
        for j, (_, r) in enumerate(cards[i:i+3]):
            with cols[j]:
                cliente = html_seguro(r.get("Cliente", "Cliente sem nome"))
                vendedor = html_seguro(r.get("Vendedor", "Sem vendedor"))
                motivo = html_seguro(r.get("motivo_prioridade", r.get("temperatura", "Acompanhamento")))
                potencial = fmt_html(r.get("potencial_mensal", r.get("ticket_medio", 0)))
                st.markdown(f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:10px;">
<b>{cliente}</b><br>
Vendedor: <b>{vendedor}</b><br>
Potencial/ticket: <b>{potencial}</b><br>
Motivo: <b>{motivo}</b>
</div>
""", unsafe_allow_html=True)

def renderizar_comissao(dados):
    st.subheader("Comissão")
    st.caption(
        "Apuração baseada no ciclo de vendas fechado de 21 a 20. "
        "A comissão só entra como a pagar quando há pagamento identificado até o fim do mês."
    )
    apuracao = calcular_comissoes(dados)
    inicio = apuracao["inicio"]
    fim = apuracao["fim"]
    prazo = apuracao["prazo"]
    pagamento = apuracao["pagamento"]
    itens = apuracao["itens"]
    pendentes = apuracao["pendentes"]
    resumo = apuracao["resumo"]

    st.info(
        f"Ciclo de venda: {inicio:%d/%m/%Y} a {fim:%d/%m/%Y} | "
        f"Cliente precisa pagar até {prazo:%d/%m/%Y} | "
        f"Pagamento da comissão em {pagamento:%d/%m/%Y}"
    )
    c1, c2, c3, c4 = st.columns(4)
    comissao_paga = float(itens.loc[itens["Pago no prazo"], "ComissÃ£o"].sum()) if not itens.empty else 0.0
    comissao_total = float(itens["ComissÃ£o"].sum()) if not itens.empty else 0.0
    aguardando = int((~itens["Pago no prazo"]).sum()) if not itens.empty else 0
    c1.metric("Comissão a pagar", fmt(comissao_paga))
    c2.metric("Comissão potencial", fmt(comissao_total))
    c3.metric("Itens aguardando pagamento", aguardando)
    c4.metric("Itens sem percentual", len(pendentes))

    st.markdown("#### Resumo por vendedora")
    if resumo.empty:
        st.info("Nenhuma comissão paga no prazo foi identificada para este ciclo.")
    else:
        tabela = resumo.copy()
        tabela["Vendas"] = tabela["Vendas"].map(fmt)
        tabela["Comissao"] = tabela["Comissao"].map(fmt)
        tabela = tabela.rename(columns={"Comissao": "Comissão"})
        st.dataframe(tabela, use_container_width=True, hide_index=True)

    st.markdown("#### Potencial por vendedora")
    if itens.empty:
        st.info("Nenhuma comissão potencial encontrada.")
    else:
        potencial = itens.groupby("Vendedor").agg(
            Vendas=("Valor", "sum"),
            Comissao_potencial=("ComissÃ£o", "sum"),
            Itens=("Produto", "count"),
        ).reset_index()
        potencial["Vendas"] = potencial["Vendas"].map(fmt)
        potencial["Comissao_potencial"] = potencial["Comissao_potencial"].map(fmt)
        potencial = potencial.rename(columns={"Comissao_potencial": "Comissão potencial"})
        st.dataframe(potencial, use_container_width=True, hide_index=True)

    with st.expander("Itens com percentual de comissão", expanded=True):
        if itens.empty:
            st.info("Nenhum item com percentual de comissão foi encontrado no campo Tipo.")
        else:
            tabela = itens.copy()
            tabela["Data venda"] = pd.to_datetime(tabela["Data venda"], errors="coerce").dt.strftime("%d/%m/%Y")
            tabela["Valor"] = tabela["Valor"].map(fmt)
            tabela["ComissÃ£o"] = tabela["ComissÃ£o"].map(fmt)
            tabela["Percentual"] = tabela["Percentual"].map(lambda x: f"{x:.2f}%".replace(".", ","))
            tabela = tabela.rename(columns={"ComissÃ£o": "Comissão"})
            st.dataframe(tabela, use_container_width=True, hide_index=True)

    with st.expander("Itens ignorados por falta de percentual"):
        if pendentes.empty:
            st.success("Nenhum item pendente de percentual.")
        else:
            tabela = pendentes.copy()
            tabela["Data venda"] = pd.to_datetime(tabela["Data venda"], errors="coerce").dt.strftime("%d/%m/%Y")
            tabela["Valor"] = tabela["Valor"].map(fmt)
            st.dataframe(tabela, use_container_width=True, hide_index=True)

def renderizar_retencao_crescimento_ceo(
    indicadores, clientes, prioridade
):
    st.markdown("---")
    st.subheader("RETENÃ‡ÃƒO E CRESCIMENTO")
    contagem = indicadores["contagem_status"]
    historico = indicadores["historico"]
    receita_prevista = float(clientes["faturamento"].sum())
    venda_possivel = float(prioridade["ticket_medio"].sum())

    linha1 = st.columns(4)
    renderizar_card_metric(
        linha1[0], "ðŸ’° Receita Prevista", fmt(receita_prevista),
        ajuda="Faturamento total do perÃ­odo carregado no sistema."
    )
    renderizar_card_metric(
        linha1[1], "ðŸŽ¯ Venda PossÃ­vel Hoje", fmt(venda_possivel),
        ajuda="Soma do ticket mÃ©dio dos clientes na prioridade comercial de hoje."
    )
    renderizar_card_metric(
        linha1[2], "âš ï¸ Carteira em Risco",
        fmt(indicadores["carteira_risco_mensal"]),
        f"{indicadores['qtd_risco']} clientes",
        "Receita que pode ser perdida caso clientes em risco nÃ£o sejam trabalhados."
    )
    renderizar_card_metric(
        linha1[3], "ðŸ”„ Potencial RecuperÃ¡vel",
        fmt(indicadores["potencial_recuperavel_mensal"]),
        f"{indicadores['qtd_recuperaveis']} clientes",
        "Receita que pode voltar para a empresa atravÃ©s da recuperaÃ§Ã£o da carteira."
    )

    linha2 = st.columns(4)
    renderizar_card_metric(
        linha2[0], "ðŸš¨ Churn Financeiro",
        fmt(indicadores["churn_financeiro_mensal"]),
        f"Anual: {fmt(indicadores['churn_financeiro_anual'])}",
        "Receita potencial perdida por clientes que deixaram de comprar."
    )
    renderizar_card_metric(
        linha2[1], "ðŸ‘¥ Clientes em Risco",
        int(contagem.get("EM RISCO", 0)),
        ajuda="Clientes que passaram do ciclo mÃ©dio de compra, mas ainda nÃ£o chegaram a 2x o ciclo."
    )
    renderizar_card_metric(
        linha2[2], "âŒ Clientes Perdidos",
        int(contagem.get("CHURN", 0)),
        ajuda="Clientes hÃ¡ mais de duas vezes o ciclo mÃ©dio de recompra sem comprar."
    )
    cac_valor = (
        fmt(indicadores["cac_atual"])
        if indicadores["novos_clientes_atual"] else "Sem novos clientes"
    )
    renderizar_card_metric(
        linha2[3], "ðŸ’µ CAC Atual", cac_valor,
        texto_variacao(indicadores["cac_variacao"]),
        "Quanto custa adquirir um novo cliente."
    )

    st.caption(
        f"CAC anterior: {fmt(indicadores['cac_anterior'])} | "
        f"Novos clientes no mÃªs atual: {indicadores['novos_clientes_atual']} | "
        f"Novos clientes no mÃªs anterior: {indicadores['novos_clientes_anterior']}"
    )

    st.markdown("#### Clientes Perdidos")
    col_status = st.columns(3)
    col_status[0].metric("SaudÃ¡veis", int(contagem.get("SAUDÃVEL", 0)))
    col_status[1].metric("Em risco", int(contagem.get("EM RISCO", 0)))
    col_status[2].metric("Churn", int(contagem.get("CHURN", 0)))

    st.markdown("#### Taxa de RecuperaÃ§Ã£o")
    st.metric(
        "Clientes recuperados / clientes marcados como em risco",
        f"{indicadores['taxa_recuperacao']:.1f}%",
        help="Clientes que estavam em risco no mÃªs anterior e voltaram a comprar no mÃªs atual."
    )

    if historico.empty:
        st.info("Ainda nÃ£o hÃ¡ histÃ³rico mensal suficiente para os grÃ¡ficos executivos.")
        return

    grafico = historico.set_index("MÃªs")
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.markdown("#### EvoluÃ§Ã£o do Churn Financeiro")
        st.line_chart(grafico[["Churn financeiro"]])
    with col_g2:
        st.markdown("#### EvoluÃ§Ã£o da Carteira em Risco")
        st.line_chart(grafico[["Carteira em risco"]])

    col_g3, col_g4 = st.columns(2)
    with col_g3:
        st.markdown("#### EvoluÃ§Ã£o do CAC")
        st.line_chart(grafico[["CAC"]])
    with col_g4:
        st.markdown("#### Clientes por Status")
        status_df = pd.DataFrame({
            "Status": ["SaudÃ¡veis", "Em risco", "Churn"],
            "Clientes": [
                int(contagem.get("SAUDÃVEL", 0)),
                int(contagem.get("EM RISCO", 0)),
                int(contagem.get("CHURN", 0)),
            ]
        }).set_index("Status")
        st.bar_chart(status_df)

    st.markdown("#### HistÃ³rico da Taxa de RecuperaÃ§Ã£o")
    st.line_chart(grafico[["Taxa de recuperaÃ§Ã£o"]])

def supabase_select(tabela, query=""):
    try:
        return supabase_request(tabela, method="GET", query=query, timeout=20) or []
    except Exception as e:
        st.warning(f"Supabase indisponível para {tabela}: {e}")
        return []

def supabase_insert(tabela, payload):
    return supabase_request(
        tabela, method="POST", body=payload, timeout=20, prefer="return=representation"
    )

def supabase_patch(tabela, filtro, payload):
    return supabase_request(
        tabela, method="PATCH", query=filtro, body=payload, timeout=20, prefer="return=representation"
    )

def renderizar_gestao_executiva_unificada(
    dados, clientes, prioridade, financeiro, contas_pagar,
    recebido_mes, pago_mes, mes_resultado, resultado_disponivel,
    indicadores_retencao
):
    st.subheader("Gestão Executiva")
    aba_ceo, aba_fin, aba_comercial = st.tabs(["CEO", "Financeiro", "Gestão comercial"])
    with aba_ceo:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Faturamento no período", fmt(clientes["faturamento"].sum()))
        col2.metric("Venda possível hoje", fmt(prioridade["ticket_medio"].sum()))
        col3.metric("Inadimplência", fmt(clientes["inadimplencia"].sum()))
        col4.metric("Clientes em prioridade", len(prioridade))
        renderizar_retencao_crescimento_ceo(indicadores_retencao, clientes, prioridade)
    with aba_fin:
        renderizar_financeiro_ceo(
            financeiro, contas_pagar, recebido_mes, pago_mes,
            mes_resultado, resultado_disponivel, clientes, listar_clientes_churn(clientes)
        )
        renderizar_financeiro_real(dados)
    with aba_comercial:
        renderizar_gestao_comercial(dados)

def renderizar_base_qualidade_unificada(dados, clientes):
    st.subheader("Base e Qualidade")
    aba_base, aba_qualidade = st.tabs(["Base de clientes", "Qualidade dos dados"])
    with aba_base:
        vendedor_opts = ["Todos"] + sorted(
            v for v in clientes["Vendedor"].dropna().astype(str).str.strip().unique()
            if v and v.lower() not in {"nan", "none"}
        )
        vendedor = st.selectbox("Vendedor", vendedor_opts, key="base_unificada_vendedor")
        base = clientes.copy()
        if vendedor != "Todos":
            base = base[base["Vendedor"].astype(str).str.strip() == vendedor].copy()
        colunas = [
            "Cliente", "Vendedor", "ultima_compra", "dias_sem_comprar",
            "intervalo", "ticket_medio", "potencial_mensal",
            "orcamentos_em_aberto", "inadimplencia", "temperatura"
        ]
        tabela = base[[c for c in colunas if c in base.columns]].copy()
        if "ultima_compra" in tabela:
            tabela["ultima_compra"] = pd.to_datetime(tabela["ultima_compra"], errors="coerce").dt.strftime("%d/%m/%Y")
        for col in ["ticket_medio", "potencial_mensal", "inadimplencia"]:
            if col in tabela:
                tabela[col] = tabela[col].map(fmt)
        st.dataframe(tabela, use_container_width=True, hide_index=True)
    with aba_qualidade:
        renderizar_qualidade_dados(dados)

def renderizar_entregas_crm(dados):
    st.subheader("Entregas")
    st.caption("Módulo leve integrado ao CRM. Dados salvos no Supabase.")
    with st.expander("SQL necessário no Supabase"):
        st.code(
            """CREATE TABLE IF NOT EXISTS entregadores (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  nome TEXT NOT NULL UNIQUE,
  codigo_acesso TEXT
);
CREATE TABLE IF NOT EXISTS rotas (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  data_rota DATE NOT NULL,
  entregador TEXT NOT NULL,
  entregador_id BIGINT,
  veiculo TEXT,
  observacao TEXT,
  criado_em TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS entregas (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  rota_id BIGINT,
  venda_id TEXT,
  numero_venda TEXT,
  cliente TEXT NOT NULL,
  telefone TEXT,
  endereco TEXT NOT NULL,
  cidade TEXT,
  estado TEXT,
  cep TEXT,
  status TEXT NOT NULL DEFAULT 'PENDENTE',
  recebido_por TEXT,
  observacao TEXT,
  data_entrega TIMESTAMPTZ,
  atualizado_por TEXT,
  api_retorno TEXT,
  origem_pedido TEXT,
  loja_id TEXT,
  criado_em TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ocorrencias (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  entrega_id BIGINT,
  tipo TEXT NOT NULL,
  descricao TEXT,
  data_ocorrencia TIMESTAMPTZ DEFAULT NOW(),
  usuario TEXT
);""",
            language="sql",
        )
    rotas = supabase_select("rotas", "?select=*&order=data_rota.desc,criado_em.desc&limit=100")
    entregas = supabase_select("entregas", "?select=*&order=criado_em.desc&limit=200")
    c1, c2, c3 = st.columns(3)
    c1.metric("Rotas", len(rotas))
    c2.metric("Pendentes", sum(1 for e in entregas if str(e.get("status", "")).upper() == "PENDENTE"))
    c3.metric("Entregues", sum(1 for e in entregas if str(e.get("status", "")).upper() in {"ENTREGUE", "CONCLUIDA", "CONCLUÍDA"}))

    with st.expander("Nova rota"):
        with st.form("nova_rota_entrega"):
            data_rota = st.date_input("Data da rota", value=date.today())
            entregador = st.text_input("Entregador")
            veiculo = st.text_input("Veículo")
            observacao = st.text_area("Observação")
            if st.form_submit_button("Criar rota"):
                try:
                    supabase_insert("rotas", {
                        "data_rota": data_rota.isoformat(),
                        "entregador": entregador.strip(),
                        "veiculo": veiculo.strip(),
                        "observacao": observacao.strip(),
                    })
                    st.success("Rota criada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Não foi possível criar rota: {e}")

    with st.expander("Nova entrega"):
        with st.form("nova_entrega"):
            rota = st.selectbox(
                "Rota",
                rotas,
                format_func=lambda r: f"{r.get('data_rota')} | {r.get('entregador')} | #{r.get('id')}",
            ) if rotas else None
            cliente = st.text_input("Cliente")
            telefone = st.text_input("Telefone")
            endereco = st.text_input("Endereço")
            cidade = st.text_input("Cidade")
            numero_venda = st.text_input("Número da venda/orçamento")
            observacao = st.text_area("Observação da entrega")
            if st.form_submit_button("Criar entrega"):
                try:
                    if not rota:
                        raise RuntimeError("Crie uma rota antes da entrega.")
                    supabase_insert("entregas", {
                        "rota_id": rota.get("id"),
                        "numero_venda": numero_venda.strip(),
                        "cliente": cliente.strip(),
                        "telefone": telefone.strip(),
                        "endereco": endereco.strip(),
                        "cidade": cidade.strip(),
                        "status": "PENDENTE",
                        "observacao": observacao.strip(),
                        "origem_pedido": "CRM",
                        "loja_id": dados.get("loja_id", ""),
                    })
                    st.success("Entrega criada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Não foi possível criar entrega: {e}")

    st.markdown("#### Entregas recentes")
    if not entregas:
        st.info("Nenhuma entrega cadastrada.")
    for entrega in entregas[:30]:
        with st.container(border=True):
            st.markdown(f"**{entrega.get('cliente')}** | {entrega.get('status')} | #{entrega.get('numero_venda', '')}")
            st.caption(f"{entrega.get('endereco', '')} - {entrega.get('cidade', '')}")
            if st.button("Marcar entregue", key=f"entrega_ok_{entrega.get('id')}"):
                try:
                    supabase_patch(
                        "entregas",
                        f"?id=eq.{entrega.get('id')}",
                        {
                            "status": "ENTREGUE",
                            "data_entrega": datetime.now().isoformat(),
                            "atualizado_por": st.session_state.get("gc_usuario_nome", USUARIO_PADRAO),
                        },
                    )
                    st.success("Entrega atualizada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro ao atualizar entrega: {e}")

def renderizar_followup_crm():
    st.subheader("Follow-up")
    st.caption("Envio semi-automático pelo CRM e acompanhamento da rotina automática externa das 10h.")
    contas = contas_email_saida()
    if not contas:
        st.warning("Nenhuma caixa SMTP configurada em [email_smtp].accounts nos secrets.")
    else:
        conta = st.selectbox(
            "Caixa de saída",
            contas,
            format_func=lambda c: f"{c.get('name', c.get('email'))} <{c.get('email')}>",
        )
        with st.form("followup_manual"):
            destino = st.text_input("E-mail do cliente")
            assunto = st.text_input("Assunto", value="Novaprint | Retomando nosso contato")
            corpo = st.text_area(
                "Mensagem",
                value="Oi, tudo bem?\n\nPassando para retomar nosso contato e ver se consigo te ajudar com a próxima compra ou orçamento.\n\nFico à disposição.",
                height=220,
            )
            confirmado = st.checkbox("Revisei e autorizo enviar este follow-up.")
            if st.form_submit_button("Enviar follow-up", disabled=not confirmado):
                try:
                    origem = enviar_email_crm(conta, destino.strip(), assunto, corpo)
                    supabase_insert("followup_historico", {
                        "destinatario": destino.strip(),
                        "assunto": assunto,
                        "mensagem": corpo,
                        "conta_saida": conta.get("email"),
                        "enviado_em": datetime.now().isoformat(),
                        "origem": origem,
                    })
                    st.success("Follow-up enviado e registrado.")
                except Exception as e:
                    st.error(f"Não foi possível enviar: {e}")
    st.markdown("#### Automação das 10h")
    st.info(
        "A rotina automática das 10h pode ser mantida pelo script externo do sistema de follow-up. "
        "No Streamlit Cloud, tarefas agendadas contínuas não são garantidas; o ideal é manter o agendamento "
        "fora do app ou migrar depois para Supabase/Edge Function/Cron."
    )

def renderizar():
    dados = st.session_state.dados_processados
    orc_aberto = dados["orc_aberto"]
    clientes = enriquecer_regras_prioridade(dados["clientes"], orc_aberto)
    dados["clientes"] = clientes
    co_num = dados["co_num"]
    co_cli = dados["co_cli"]
    co_valor = dados["co_valor"]
    periodo_inicio = dados.get("periodo_inicio", clientes["ultima_compra"].min())
    periodo_fim = dados.get("periodo_fim", clientes["ultima_compra"].max())
    financeiro = dados.get("financeiro", pd.DataFrame())
    contas_pagar = dados.get("contas_pagar", pd.DataFrame())
    recebido_mes = float(dados.get("recebido_mes", 0))
    pago_mes = float(dados.get("pago_mes", 0))
    mes_resultado = dados.get("mes_resultado", datetime.now().strftime("%m/%Y"))
    resultado_disponivel = bool(
        dados.get("resultado_financeiro_disponivel", False)
    )

    prioridade = montar_prioridade(clientes)
    resumo = montar_resumo(clientes)
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    clientes_churn = listar_clientes_churn(clientes)
    churn_avancado = calcular_churn_avancado(dados)
    configuracao = dados.get("configuracao", {})
    indicadores_retencao = calcular_indicadores_retencao_ceo(
        clientes,
        dados.get("vendas_validas", pd.DataFrame()),
        dados.get("periodo_inicio"),
        dados.get("periodo_fim"),
        float(configuracao.get("custo_comercial_mensal", 0)),
        float(configuracao.get("custo_marketing_mensal", 0)),
        float(configuracao.get("custo_ferramentas_mensal", 0)),
    )

    if dados.get("origem") == "api":
        atualizado = dados.get("atualizado_em")
        texto_atualizacao = atualizado.strftime("%d/%m/%Y %H:%M") if atualizado else "agora"
        st.success(
            f"Dados carregados pela API do GestãoClick | "
            f"Vendedor: {dados.get('vendedor_nome', 'Todos')} | "
            f"Período: {periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y} | "
            f"Atualizado em {texto_atualizacao}"
        )
    else:
        st.info("Dados carregados por arquivos Excel.")

    if pagina == "Gestão Executiva":
        renderizar_gestao_executiva_unificada(
            dados, clientes, prioridade, financeiro, contas_pagar,
            recebido_mes, pago_mes, mes_resultado, resultado_disponivel,
            indicadores_retencao
        )
        return

    if pagina == "Comercial":
        renderizar_resumo_diario(dados)
        return

    if pagina == "Base e Qualidade":
        renderizar_base_qualidade_unificada(dados, clientes)
        return

    if pagina == "Entregas":
        renderizar_entregas_crm(dados)
        return

    if pagina == "Follow-up":
        renderizar_followup_crm()
        return

    if pagina == "Resumo E-mail":
        st.subheader("Resumo para E-mail")
        texto_email = gerar_texto_email(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
        )
        st.text_area("Texto pronto para enviar:", texto_email, height=650)
        st.download_button(
            "Baixar resumo em .txt",
            texto_email,
            f"Resumo_Comercial_{datetime.now():%d_%m_%Y}.txt",
            "text/plain"
        )
        return

    if pagina == "Relatório Comercial":
        st.subheader("Relatório Comercial")
        pdf = gerar_pdf(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
        )
        if pdf:
            st.download_button(
                "Baixar relatório comercial em PDF",
                pdf,
                f"Relatorio_Comercial_{datetime.now():%d_%m_%Y}.pdf",
                "application/pdf"
            )
        else:
            st.warning("PDF indisponível. Verifique se reportlab está no requirements.txt.")
        return

    if pagina == "Resumo Diário":
        renderizar_resumo_diario(dados)

    if pagina == "Geração de Orçamentos":
        renderizar_geracao_orcamentos()

    if pagina == "Comissão":
        renderizar_comissao(dados)

    if pagina == "AÃ§Ãµes de Hoje":
        st.subheader("AÃ§Ãµes de Hoje")
        st.caption("Fila operacional do dia com clientes, orÃ§amentos e retornos que precisam de aÃ§Ã£o.")
        orc_operacional = orc_aberto.copy()
        if not orc_operacional.empty:
            cli_orc_col = co_cli or achar_coluna(orc_operacional, ["cliente"])
            cli_id_orc_col = achar_coluna(orc_operacional, ["cliente id"])
            contato_orc_recente = orc_operacional.apply(
                lambda r: contato_realizado_periodo(
                    texto_valido(r.get(cli_id_orc_col, "")) if cli_id_orc_col else "",
                    texto_valido(r.get(cli_orc_col, "")) if cli_orc_col else "",
                    7,
                ),
                axis=1,
            )
            orc_operacional = orc_operacional[~contato_orc_recente].copy()
        orc_2_dias = orc_operacional[orc_operacional["dias_no_sistema"] == 2].copy() if not orc_operacional.empty else pd.DataFrame()
        orc_urgentes = orc_operacional[orc_operacional["dias_no_sistema"] >= 3].copy() if not orc_operacional.empty else pd.DataFrame()
        atraso_recompra = clientes[
            clientes["temperatura"].isin(["ðŸ”´ ATRASADO NA RECOMPRA", "âš« CLIENTE INATIVO"])
            & (~clientes["contato_recente"])
        ].copy()
        retornos_hoje = clientes[
            clientes.get("retornos_hoje", pd.Series(0, index=clientes.index)).gt(0)
            & (~clientes["contato_recente"])
        ].copy()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Clientes para ligar", len(prioridade))
        c2.metric("OrÃ§amentos com 2 dias", len(orc_2_dias))
        c3.metric("OrÃ§amentos urgentes 3+ dias", len(orc_urgentes))
        c4.metric("Atraso de recompra", len(atraso_recompra))
        c5.metric("Retornos hoje", len(retornos_hoje))

        with st.expander("Clientes para ligar hoje", expanded=True):
            cards = list(prioridade.head(15).iterrows())
            for i in range(0, len(cards), 3):
                cols_cards = st.columns(3)
                for j, (indice, row) in enumerate(cards[i:i+3]):
                    with cols_cards[j]:
                        card_cliente(row, "acoes_hoje", f"{indice}_{i}_{j}")
            if not cards:
                st.info("Nenhum cliente prioritÃ¡rio para ligar agora.")

        with st.expander("OrÃ§amentos com 2 dias"):
            if orc_2_dias.empty:
                st.info("Nenhum orÃ§amento com 2 dias.")
            else:
                renderizar_cards_orcamentos_simples(orc_2_dias, co_num, co_cli, co_valor)

        with st.expander("OrÃ§amentos urgentes com 3+ dias"):
            if orc_urgentes.empty:
                st.info("Nenhum orÃ§amento urgente.")
            else:
                renderizar_cards_orcamentos_simples(
                    orc_urgentes, co_num, co_cli, co_valor, incluir_acao=True
                )

        with st.expander("Clientes em atraso de recompra"):
            if atraso_recompra.empty:
                st.info("Nenhum cliente em atraso de recompra.")
            else:
                renderizar_cards_clientes_simples(atraso_recompra)

        with st.expander("Retornos programados para hoje"):
            if retornos_hoje.empty:
                st.info("Nenhum retorno programado para hoje.")
            else:
                renderizar_cards_clientes_simples(retornos_hoje)

    if pagina == "ðŸ‘‘ CEO":
        st.subheader("ðŸ‘‘ Painel CEO")

        col_churn, col_perdidos, col_base = st.columns(3)
        with col_churn:
            st.metric("Taxa de churn estimada", f"{taxa_churn:.1f}%")
        with col_perdidos:
            st.metric("Clientes em churn", qtd_churn)
        with col_base:
            st.metric("Base analisada", base_churn)

        with st.expander("Como a taxa de churn foi calculada?"):
            st.markdown(
                """
                **FÃ³rmula**

                `Taxa de churn = clientes em churn Ã· clientes com ciclo conhecido Ã— 100`

                Um cliente entra em **churn estimado** quando:

                - possui pelo menos duas compras, permitindo calcular seu intervalo mÃ©dio;
                - estÃ¡ sem comprar hÃ¡ mais de duas vezes o seu intervalo mÃ©dio de recompra.

                **Exemplo:** se um cliente costuma comprar a cada 30 dias e estÃ¡ hÃ¡ mais
                de 60 dias sem comprar, ele Ã© considerado em churn. Clientes com apenas
                uma compra nÃ£o entram na base, pois ainda nÃ£o possuem ciclo conhecido.
                """
            )
            st.write(
                f"CÃ¡lculo atual: {qtd_churn} Ã· {base_churn} Ã— 100 = {taxa_churn:.1f}%"
                if base_churn
                else "Ainda nÃ£o hÃ¡ clientes com histÃ³rico suficiente para calcular o churn."
            )

        st.markdown(f"**Receita prevista:** **{fmt(clientes['faturamento'].sum())}**")
        st.caption("Soma do faturamento total existente no relatÃ³rio de vendas importado. O perÃ­odo depende do arquivo enviado.")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.caption("MÃ©dia mensal de compras dos Ãºltimos 3 meses.")
        st.markdown(f"**Venda possÃ­vel hoje:** **{fmt(prioridade['ticket_medio'].sum())}**")
        st.caption("Soma do ticket mÃ©dio dos clientes classificados como QUENTE na aba Prioridade.")
        st.markdown(f"**Potencial recuperÃ¡vel:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.caption("Soma do potencial mensal dos clientes classificados como ATRASADO NA RECOMPRA ou CLIENTE INATIVO.")
        st.markdown(f"**InadimplÃªncia real:** **{fmt(clientes['inadimplencia'].sum())}**")
        renderizar_retencao_crescimento_ceo(
            indicadores_retencao, clientes, prioridade
        )

    if pagina == "ðŸ’° Financeiro CEO":
        renderizar_financeiro_ceo(
            financeiro, contas_pagar, recebido_mes, pago_mes,
            mes_resultado, resultado_disponivel, clientes, clientes_churn
        )
        renderizar_financeiro_real(dados)

    if pagina == "ðŸŽ¯ GestÃ£o Comercial":
        renderizar_gestao_comercial(dados)

    if pagina == "ðŸ“‰ Churn":
        st.subheader("ðŸ“‰ Clientes em churn")
        st.caption(
            "Clientes com ciclo de recompra conhecido que estÃ£o hÃ¡ mais de duas vezes "
            "o intervalo mÃ©dio sem comprar."
        )

        col1, col2, col3 = st.columns(3)
        col1.metric("Taxa de churn", f"{taxa_churn:.1f}%")
        col2.metric("Clientes em churn", qtd_churn)
        col3.metric(
            "Potencial mensal em risco",
            fmt(clientes_churn["potencial_mensal"].sum())
        )
        avancado = st.columns(4)
        avancado[0].metric(
            "Churn ponderado por valor",
            f"{churn_avancado['churn_ponderado']:.1f}%"
        )
        avancado[1].metric(
            "Migrando para churn",
            len(churn_avancado["migrando"])
        )
        avancado[2].metric(
            "RecuperaÃ§Ãµes histÃ³ricas",
            churn_avancado["recuperados_historicos"],
            f"{churn_avancado['taxa_recuperacao_historica']:.1f}% da base recorrente"
        )
        avancado[3].metric(
            "Clientes sazonais",
            churn_avancado["sazonais"]
        )
        st.caption(
            "O churn ponderado considera o faturamento dos clientes perdidos. "
            "Clientes sazonais sÃ£o sinalizados separadamente por apresentarem ciclos irregulares."
        )
        if not churn_avancado["tendencia_mensal"].empty:
            st.markdown("#### EvoluÃ§Ã£o mensal do churn")
            tendencia = churn_avancado["tendencia_mensal"].set_index("MÃªs")
            st.line_chart(tendencia)
        if not churn_avancado["migrando"].empty:
            st.markdown("#### Clientes migrando para churn")
            migrando = churn_avancado["migrando"][[
                "Cliente", "dias_sem_comprar", "intervalo",
                "potencial_mensal", "temperatura"
            ]].copy()
            migrando["potencial_mensal"] = migrando["potencial_mensal"].map(fmt)
            migrando = migrando.rename(columns={
                "dias_sem_comprar": "Dias sem comprar",
                "intervalo": "Ciclo mÃ©dio",
                "potencial_mensal": "Potencial mensal",
                "temperatura": "SituaÃ§Ã£o",
            })
            st.dataframe(migrando, use_container_width=True, hide_index=True)

        if clientes_churn.empty:
            st.success("Nenhum cliente estÃ¡ classificado em churn.")
        else:
            for _, r in clientes_churn.iterrows():
                cliente_html = html_seguro(r["Cliente"])
                ultima_compra = r["ultima_compra"].strftime("%d/%m/%Y")
                st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border-left:6px solid #d62728;border-top:1px solid #ddd;border-right:1px solid #ddd;border-bottom:1px solid #ddd;">
<b>{cliente_html}</b><br>
Ãšltima compra: <b>{ultima_compra}</b><br>
EstÃ¡ hÃ¡ <b>{int(r['dias_sem_comprar'])} dias</b> sem comprar<br>
Ciclo mÃ©dio: <b>{int(r['intervalo'])} dias</b><br>
Limite para churn: <b>{int(r['limite_churn_dias'])} dias</b><br>
Passou do limite hÃ¡: <b>{int(r['dias_alem_limite'])} dias</b><br><br>
Faturamento histÃ³rico: <b>{fmt_html(r['faturamento'])}</b><br>
Ticket mÃ©dio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal em risco: <b>{fmt_html(r['potencial_mensal'])}</b><br>
InadimplÃªncia: <b>{fmt_html(r['inadimplencia'])}</b>
</div>
""", unsafe_allow_html=True)
                itens_churn = r.get("itens_comprados", [])
                if not isinstance(itens_churn, list):
                    itens_churn = [itens_churn] if str(itens_churn).strip() else []
                produto_churn = nome_item_resumo(itens_churn[0]) if itens_churn else ""
                valor_unitario_churn, data_preco_churn = (
                    ultimo_preco_produto_cliente(
                        dados,
                        str(r.get("Cliente ID", "")),
                        produto_churn,
                    )
                    if produto_churn else (0.0, pd.NaT)
                )
                oferta_churn = (
                    f"Cliente em churn. Sugerir recompra de {produto_churn}."
                    if produto_churn else
                    "Cliente em churn. Retomar relacionamento e identificar demanda atual."
                )
                row_churn = r.to_dict()
                row_churn.update({
                    "Categoria": "CHURN",
                    "Produto": produto_churn,
                    "Oferta": oferta_churn,
                    "Valor": r.get("potencial_mensal", 0),
                    "_cliente_id": str(r.get("Cliente ID", "")),
                    "_ultimo_valor_sugerido": valor_unitario_churn,
                    "_ultima_data_preco": data_preco_churn,
                })
                chave_churn = chave_widget(
                    f"churn_{r.get('Cliente ID', '')}_{r.get('Cliente', '')}"
                )
                if produto_churn:
                    st.caption(f"Produto sugerido para retomada: {produto_churn}")
                renderizar_botao_liguei_resumo(
                    str(r.get("Cliente ID", "")),
                    str(r.get("Cliente", "")),
                    str(r.get("Vendedor", "Sem vendedor")),
                    oferta_churn,
                    chave_churn,
                )
                renderizar_email_resumo(
                    str(r.get("Cliente", "")),
                    str(r.get("Vendedor", "Sem vendedor")),
                    oferta_churn,
                    chave_churn,
                    row_churn,
                )
                renderizar_whatsapp_resumo(
                    str(r.get("Cliente", "")),
                    str(r.get("Vendedor", "Sem vendedor")),
                    oferta_churn,
                    chave_churn,
                    row_churn,
                )
                renderizar_criar_orcamento_sugerido(row_churn, chave_churn)

    if pagina == "ðŸ”¥ Prioridade":
        st.subheader("ðŸ”¥ Prioridade")
        if prioridade.empty:
            st.info("Nenhum cliente no timing ideal hoje.")
        cards = list(prioridade.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "prioridade", f"{indice}_{i}_{j}")

    if pagina == "ðŸ“‹ Resumo":
        st.subheader("ðŸ“‹ Resumo Comercial")
        st.markdown(f"**Clientes para aÃ§Ã£o:** **{len(resumo)}**")
        st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")
        st.markdown(f"**Potencial recuperÃ¡vel:** **{fmt(resumo['potencial_recuperavel'].sum())}**")
        cards = list(resumo.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "resumo", f"{indice}_{i}_{j}")

    if pagina == "ðŸ“„ OrÃ§amentos":
        st.subheader("ðŸ“„ OrÃ§amentos em aberto para retorno")
        if orc_aberto.empty:
            st.info("Nenhum orÃ§amento em aberto nos Ãºltimos 30 dias.")
        else:
            cards = list(orc_aberto.iterrows())
            for i in range(0, len(cards), 3):
                cols = st.columns(3)
                for j, (indice, r) in enumerate(cards[i:i+3]):
                    with cols[j]:
                        valor_txt = fmt_html(r[co_valor]) if co_valor else "Sem valor"
                        num_orc = str(r[co_num])
                        orcamento_id = str(r.get("_orcamento_id", "")).strip()
                        orcamento_uid = chave_widget(
                            orcamento_id or f"{num_orc}_{indice}_{i}_{j}"
                        )
                        chave_obs = f"obs_orc_{orcamento_uid}"
                        num_orc_html = html_seguro(r[co_num])
                        cliente_orc_html = html_seguro(r[co_cli])
                        status_orc_html = html_seguro(r["acao_recomendada_orcamento"])

                        st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>OrÃ§amento NÂº {num_orc_html}</b><br>
Cliente: <b>{cliente_orc_html}</b><br>
Tempo no sistema: <b>{int(r['dias_no_sistema'])} dia(s)</b><br>
Status: <b>{status_orc_html}</b><br>
Valor: <b>{valor_txt}</b>
</div>
""", unsafe_allow_html=True)

                        if dados.get("origem") == "api" and str(r.get("_observacoes_interna", "")).strip():
                            with st.expander("Ver histÃ³rico do GestÃ£oClick"):
                                st.text(str(r.get("_observacoes_interna", "")))

                        obs = st.text_area(
                            "Nova observaÃ§Ã£o" if dados.get("origem") == "api" else "ObservaÃ§Ã£o",
                            value=st.session_state.observacoes_orc.get(num_orc, ""),
                            key=chave_obs
                        )

                        if st.button(
                            f"ðŸ’¾ Salvar observaÃ§Ã£o {num_orc}",
                            key=f"salvar_obs_{orcamento_uid}"
                        ):
                            try:
                                if not obs.strip():
                                    raise RuntimeError("Digite uma observaÃ§Ã£o antes de salvar.")
                                if dados.get("origem") == "api":
                                    orcamento_id = str(r.get("_orcamento_id") or "").strip()
                                    if not orcamento_id:
                                        raise RuntimeError("ID interno do orÃ§amento nÃ£o encontrado.")
                                    st.session_state.alteracao_gestaoclick_pendente = {
                                        "tipo": "observacao_orcamento",
                                        "numero": num_orc,
                                        "orcamento_id": orcamento_id,
                                        "loja_id": dados["loja_id"],
                                        "cliente": str(r[co_cli]),
                                        "observacao": obs,
                                    }
                                    st.rerun()
                                else:
                                    st.session_state.observacoes_orc[num_orc] = obs
                                    salvar_observacao_orcamento(num_orc, r[co_cli], obs)
                                    st.success("ObservaÃ§Ã£o salva no Google Sheets.")
                            except Exception as e:
                                st.error(f"NÃ£o foi possÃ­vel salvar a observaÃ§Ã£o: {e}")

                        pendente = st.session_state.alteracao_gestaoclick_pendente
                        if (
                            dados.get("origem") == "api" and pendente and
                            pendente.get("numero") == num_orc and
                            pendente.get("orcamento_id") == orcamento_id
                        ):
                            st.warning(
                                "Confirme a alteraÃ§Ã£o no GestÃ£oClick.\n\n"
                                f"OrÃ§amento: {num_orc}\n\n"
                                f"Cliente: {pendente['cliente']}\n\n"
                                f"Nova observaÃ§Ã£o: {pendente['observacao']}"
                            )
                            confirmado = st.checkbox(
                                "Revisei os dados e autorizo a gravaÃ§Ã£o no GestÃ£oClick.",
                                key=f"confirmar_gc_{orcamento_uid}"
                            )
                            col_confirmar, col_cancelar = st.columns(2)
                            if col_confirmar.button(
                                "Confirmar gravaÃ§Ã£o",
                                key=f"executar_gc_{orcamento_uid}",
                                disabled=not confirmado,
                                type="primary"
                            ):
                                try:
                                    api_gestaoclick().append_budget_note(
                                        pendente["orcamento_id"],
                                        pendente["loja_id"],
                                        pendente["observacao"],
                                        st.session_state.get(
                                            "gc_usuario_nome", USUARIO_PADRAO
                                        )
                                    )
                                    st.session_state.observacoes_orc[num_orc] = ""
                                    st.session_state.alteracao_gestaoclick_pendente = None
                                    st.success(
                                        "AlteraÃ§Ã£o confirmada e gravada no GestÃ£oClick."
                                    )
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Falha ao gravar no GestÃ£oClick: {e}")
                            if col_cancelar.button(
                                "Cancelar",
                                key=f"cancelar_gc_{orcamento_uid}"
                            ):
                                st.session_state.alteracao_gestaoclick_pendente = None
                                st.rerun()

    if pagina == "ðŸ§  GestÃ£o":
        st.subheader("ðŸ§  GestÃ£o")
        st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
        st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
        st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.markdown(f"**Potencial recuperÃ¡vel:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.markdown(f"**InadimplÃªncia total:** **{fmt(clientes['inadimplencia'].sum())}**")
        st.markdown(f"**Taxa de churn estimada:** **{taxa_churn:.1f}%**")
        st.caption(f"Clientes em churn: {qtd_churn} | Base analisada: {base_churn}")

    if pagina == "âœ… Qualidade":
        renderizar_qualidade_dados(dados)

    if pagina == "ðŸ“Š Base":
        st.subheader("ðŸ“Š Base completa")
        acoes = ["Todas"] + sorted(clientes["acao_ia"].unique().tolist())
        temperaturas = ["Todas"] + sorted(clientes["temperatura"].unique().tolist())
        col1, col2 = st.columns(2)
        with col1:
            filtro_acao = st.selectbox("Filtrar por aÃ§Ã£o sugerida", acoes, key="filtro_base_acao")
        with col2:
            filtro_temp = st.selectbox("Filtrar por temperatura", temperaturas, key="filtro_base_temp")

        base = clientes.copy()
        if filtro_acao != "Todas":
            base = base[base["acao_ia"] == filtro_acao]
        if filtro_temp != "Todas":
            base = base[base["temperatura"] == filtro_temp]

        cards = list(base.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, r) in enumerate(cards[i:i+3]):
                with cols[j]:
                    estrela = "â­ Cliente estratÃ©gico<br>" if r["cliente_estrategico"] else ""
                    cliente_html = html_seguro(r["Cliente"])
                    temperatura_html = html_seguro(r["temperatura"])
                    risco_html = html_seguro(r["risco_inadimplencia"])
                    acao_html = html_seguro(r["acao_ia"])
                    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{cliente_html}</b><br>
{estrela}
Temperatura: <b>{temperatura_html}</b><br>
Score comercial: <b>{int(r['score_comercial'])}/100</b><br>
Faturamento: <b>{fmt_html(r['faturamento'])}</b><br>
Ticket mÃ©dio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(r['potencial_mensal'])}</b><br>
Potencial recuperÃ¡vel: <b>{fmt_html(r['potencial_recuperavel'])}</b><br>
Compras: <b>{int(r['qtd_compras'])}</b><br>
Intervalo mÃ©dio: <b>{int(r['intervalo'])} dias</b><br>
Ãšltima compra: <b>{r['ultima_compra'].strftime('%d/%m/%Y')}</b><br>
Dias sem comprar: <b>{int(r['dias_sem_comprar'])}</b><br>
OrÃ§amentos em aberto: <b>{int(r['orcamentos_em_aberto'])}</b><br>
InadimplÃªncia: <b>{fmt_html(r['inadimplencia'])}</b><br>
Score de risco: <b>{int(r['score_risco'])}/100 â€” {risco_html}</b><br>
RecomendaÃ§Ã£o: <b>{acao_html}</b>
</div>
""", unsafe_allow_html=True)
                    cliente_uid = chave_widget(identificador_cliente(r, f"base_{i}_{j}"))
                    with st.expander(f"Ver itens comprados e orÃ§ados - {r['Cliente']} #{cliente_uid[-6:]}"):
                        renderizar_lista_itens(
                            "Itens comprados",
                            r.get("itens_comprados", [])
                        )
                        renderizar_lista_itens(
                            "Itens orÃ§ados",
                            r.get("itens_orcados", [])
                        )

    if pagina == "âœ‰ï¸ Resumo E-mail":
        st.subheader("âœ‰ï¸ Resumo para E-mail")
        st.caption(
            "Resumo diÃ¡rio e acionÃ¡vel para a equipe: indicadores, prioridades, "
            "orÃ§amentos urgentes, churn e plano do dia."
        )
        texto_email = gerar_texto_email(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
        )
        st.text_area("Texto pronto para enviar:", texto_email, height=650)
        st.download_button(
            "Baixar resumo em .txt",
            texto_email,
            f"Resumo_Comercial_{datetime.now():%d_%m_%Y}.txt",
            "text/plain"
        )

    if pagina == "ðŸ“§ RelatÃ³rio Comercial":
        st.subheader("ðŸ“§ RelatÃ³rio Comercial")
        st.caption(
            "RelatÃ³rio executivo completo com perÃ­odo analisado, indicadores, carteira, "
            "prioridades, churn, orÃ§amentos, inadimplÃªncia, plano de aÃ§Ã£o e metodologia."
        )
        pdf = gerar_pdf(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
        )
        if pdf:
            nome_pdf = f"Relatorio_Comercial_Executivo_{datetime.now():%d_%m_%Y}.pdf"
            st.markdown(
                link_download_bytes(
                    "ðŸ“„ Baixar RelatÃ³rio Executivo em PDF",
                    pdf,
                    nome_pdf,
                    "application/pdf",
                ),
                unsafe_allow_html=True
            )
        else:
            st.warning("PDF indisponÃ­vel. Verifique se 'reportlab' estÃ¡ no requirements.txt.")

opcoes_menu_crm = [
    "Gestão Executiva",
    "Comercial",
    "Base e Qualidade",
    "Entregas",
    "Follow-up",
    "Resumo E-mail",
    "Relatório Comercial",
]
if "pagina_atual_crm" not in st.session_state:
    st.session_state.pagina_atual_crm = "Gestão Executiva"
if "abrir_resumo_diario" not in st.session_state:
    st.session_state.abrir_resumo_diario = False
if "abrir_geracao_orcamentos" not in st.session_state:
    st.session_state.abrir_geracao_orcamentos = False
if "abrir_comissao" not in st.session_state:
    st.session_state.abrir_comissao = False
if "resumo_diario_secao" not in st.session_state:
    st.session_state.resumo_diario_secao = "Início"
opcoes_paginas_crm = opcoes_menu_crm + ["Resumo Diário", "Geração de Orçamentos", "Comissão"]
if st.session_state.pagina_atual_crm not in opcoes_paginas_crm:
    st.session_state.pagina_atual_crm = "Gestão Executiva"

with st.sidebar.expander("CRM Inteligente", expanded=True):
    pagina_radio_atual = st.session_state.get("menu_lateral_crm", "Gestão Executiva")
    if pagina_radio_atual not in opcoes_menu_crm:
        pagina_radio_atual = "Gestão Executiva"
    pagina_selecionada = st.radio(
        "Abas",
        opcoes_menu_crm,
        index=opcoes_menu_crm.index(pagina_radio_atual),
        key="menu_lateral_crm",
        label_visibility="collapsed",
    )

    radio_anterior = st.session_state.get("menu_lateral_crm_anterior")
    if radio_anterior is None:
        st.session_state.menu_lateral_crm_anterior = pagina_selecionada
    elif pagina_selecionada != radio_anterior:
        st.session_state.abrir_resumo_diario = False
        st.session_state.abrir_geracao_orcamentos = False
        st.session_state.abrir_comissao = False
        st.session_state.pagina_atual_crm = pagina_selecionada
        st.session_state.menu_lateral_crm_anterior = pagina_selecionada
    elif not st.session_state.abrir_resumo_diario and not st.session_state.abrir_geracao_orcamentos and not st.session_state.abrir_comissao:
        st.session_state.pagina_atual_crm = pagina_selecionada

with st.sidebar.expander("Comercial", expanded=False):
    secoes_resumo = [
        "Início",
        "Fila de prioridades",
        "Churn e retenção",
        "Orçamentos",
        "Buscar cliente/produtos",
        "Ações rápidas",
        "Visão de gestão",
    ]
    secao_resumo_lateral = st.radio(
        "Seções",
        secoes_resumo,
        index=secoes_resumo.index(st.session_state.resumo_diario_secao)
        if st.session_state.resumo_diario_secao in secoes_resumo else 0,
        key="menu_lateral_resumo_diario",
        label_visibility="collapsed",
    )
    secao_anterior_resumo = st.session_state.get("menu_lateral_resumo_anterior")
    if secao_anterior_resumo is None:
        st.session_state.menu_lateral_resumo_anterior = secao_resumo_lateral
    elif secao_resumo_lateral != secao_anterior_resumo:
        st.session_state.resumo_diario_secao = secao_resumo_lateral
        st.session_state.abrir_resumo_diario = True
        st.session_state.pagina_atual_crm = "Comercial"
        st.session_state.menu_lateral_resumo_anterior = secao_resumo_lateral
        st.rerun()
    if st.button("Abrir Comercial", use_container_width=True):
        st.session_state.resumo_diario_secao = secao_resumo_lateral
        st.session_state.abrir_resumo_diario = True
        st.session_state.abrir_geracao_orcamentos = False
        st.session_state.abrir_comissao = False
        st.session_state.pagina_atual_crm = "Comercial"
        st.rerun()

with st.sidebar.expander("Geração de Orçamentos", expanded=False):
    st.caption("Criar orçamento via API do GestãoClick.")
    if st.button("Abrir Geração de Orçamentos", use_container_width=True):
        st.session_state.abrir_geracao_orcamentos = True
        st.session_state.abrir_resumo_diario = False
        st.session_state.abrir_comissao = False
        st.session_state.pagina_atual_crm = "Geração de Orçamentos"
        st.rerun()

with st.sidebar.expander("Comissão", expanded=False):
    st.caption("Apuração de comissão por ciclo 21 a 20.")
    if st.button("Abrir Comissão", use_container_width=True):
        st.session_state.abrir_comissao = True
        st.session_state.abrir_resumo_diario = False
        st.session_state.abrir_geracao_orcamentos = False
        st.session_state.pagina_atual_crm = "Comissão"
        st.rerun()

pagina = st.session_state.pagina_atual_crm

with st.sidebar.expander("Configurações", expanded=False):
    st.info("Fonte automática: API GestãoClick")
    st.markdown("**Base estável Supabase/local**")
    ultima_base = idade_snapshot_estavel()
    if ultima_base:
        st.success(f"Última base estável salva: {ultima_base}")
        if st.button("Usar última base estável", use_container_width=True):
            snapshot = carregar_snapshot_estavel()
            if snapshot is not None:
                st.session_state.dados_processados = snapshot
                st.success("Base estável carregada.")
                st.rerun()
            else:
                st.warning("Não encontrei uma base estável válida.")
    else:
        st.caption("Ainda não há base estável salva. Atualize uma vez pelo GestãoClick.")
    st.markdown("**Supabase**")
    st.caption(
        "Planejado para observações, já liguei, retornos programados, histórico do cliente "
        "e usuários/vendedoras."
    )
    if st.button("Testar conexão Supabase", use_container_width=True):
        try:
            resultados_supabase = testar_conexao_supabase()
            ok = [r for r in resultados_supabase if str(r[1]) in {"200", "206"}]
            if len(ok) == len(resultados_supabase):
                st.success("Supabase conectado e tabelas acessÃ­veis.")
            else:
                st.warning("Supabase respondeu, mas há tabelas pendentes ou sem permissão.")
            st.dataframe(
                pd.DataFrame(resultados_supabase, columns=["Tabela", "Status", "Detalhe"]),
                use_container_width=True,
                hide_index=True,
            )
        except Exception as e:
            st.error(f"Não foi possível conectar ao Supabase: {e}")
    st.markdown("**Watidy / WhatsApp**")
    if watidy_configurado():
        cfg_watidy = credenciais_watidy()
        st.success(f"Watidy configurado: {cfg_watidy['base_url']}{cfg_watidy['send_path']}")
    else:
        st.warning("Watidy não configurado. O CRM abrirá rascunho no WhatsApp.")
modo_dados = "API GestãoClick"

if modo_dados == "API GestãoClick":
    with st.sidebar.expander("Conexão GestãoClick", expanded=False):
        access_padrao, secret_padrao = credenciais_gestaoclick()
        tokens_no_secrets = credenciais_gestaoclick_no_secrets()
        if "gc_access_token" not in st.session_state:
            st.session_state.gc_access_token = ""
        if "gc_secret_token" not in st.session_state:
            st.session_state.gc_secret_token = ""
        if "gc_usuario_nome" not in st.session_state:
            st.session_state.gc_usuario_nome = USUARIO_PADRAO

        if tokens_no_secrets:
            st.success("Tokens do GestãoClick carregados pelo secrets.")
        else:
            st.error(
                "Tokens do GestãoClick não encontrados no secrets. "
                "Configure st.secrets['gestaoclick'] para conectar."
            )
        st.text_input(
            "Nome de quem registra as observações",
            key="gc_usuario_nome"
        )

        if st.button("Conectar e carregar lojas"):
            try:
                with st.spinner("Conectando ao GestãoClick..."):
                    st.session_state.gestaoclick_lojas = api_gestaoclick().stores()
                    st.session_state.gestaoclick_usuarios = []
                st.success("Conexão realizada.")
            except Exception as e:
                st.error(f"Erro de conexão: {e}")

    lojas = st.session_state.gestaoclick_lojas
    if lojas:
        lojas_validas = [
            loja for loja in lojas
            if str(loja.get("id") or "").strip()
        ]
        loja_escolhida = st.sidebar.selectbox(
            "Loja",
            lojas_validas,
            format_func=lambda loja: (
                loja.get("nome") or loja.get("nome_fantasia") or f"Loja {loja.get('id')}"
            )
        )
        loja_id = str(loja_escolhida.get("id"))

        if st.sidebar.button("Carregar vendedores"):
            try:
                with st.spinner("Carregando vendedores..."):
                    st.session_state.gestaoclick_usuarios = api_gestaoclick().users(loja_id)
                st.sidebar.success("Vendedores carregados.")
            except Exception as e:
                st.sidebar.error(f"Erro ao carregar vendedores: {e}")

        usuarios = [
            usuario for usuario in st.session_state.gestaoclick_usuarios
            if str(usuario.get("id") or "").strip()
            and str(usuario.get("nome") or "").strip()
        ]
        opcoes_vendedor = [{"id": "", "nome": "Todos"}, *usuarios]
        vendedor = st.sidebar.selectbox(
            "Vendedor",
            opcoes_vendedor,
            format_func=lambda item: item.get("nome") or "Sem nome"
        )

        fim_padrao = date.today()
        inicio_padrao = fim_padrao - timedelta(days=90)
        inicio_api = st.sidebar.date_input(
            "Vendas e orçamentos desde",
            value=inicio_padrao,
            max_value=fim_padrao
        )
        fim_api = st.sidebar.date_input(
            "Até",
            value=fim_padrao,
            min_value=inicio_api,
            max_value=fim_padrao
        )
        st.sidebar.caption(
            "Padrão comercial: últimos 90 dias para ganhar velocidade. "
            "A visão financeira permanece separada e considera os dados financeiros disponíveis."
        )

        with st.sidebar.expander("Metas e premissas financeiras"):
            meta_geral = st.number_input(
                "Meta geral mensal",
                min_value=0.0,
                value=float(st.session_state.get("meta_geral", 0.0)),
                step=1000.0
            )
            st.session_state.meta_geral = meta_geral
            custo_comercial_mensal = st.number_input(
                "Custo mensal da equipe comercial",
                min_value=0.0,
                value=float(st.session_state.get("custo_comercial_mensal", 0.0)),
                step=500.0
            )
            custo_marketing_mensal = st.number_input(
                "Custo mensal de marketing",
                min_value=0.0,
                value=float(st.session_state.get("custo_marketing_mensal", 0.0)),
                step=500.0
            )
            custo_ferramentas_mensal = st.number_input(
                "Custo mensal de ferramentas comerciais",
                min_value=0.0,
                value=float(st.session_state.get("custo_ferramentas_mensal", 0.0)),
                step=100.0
            )
            st.session_state.custo_comercial_mensal = custo_comercial_mensal
            st.session_state.custo_marketing_mensal = custo_marketing_mensal
            st.session_state.custo_ferramentas_mensal = custo_ferramentas_mensal
            vendedor_nome_config = vendedor.get("nome") or "Todos"
            if vendedor_nome_config != "Todos":
                meta_vendedor = st.number_input(
                    f"Meta de {vendedor_nome_config}",
                    min_value=0.0,
                    value=float(
                        st.session_state.metas_vendedor.get(
                            vendedor_nome_config, 0.0
                        )
                    ),
                    step=500.0
                )
                st.session_state.metas_vendedor[vendedor_nome_config] = meta_vendedor
            saldo_inicial = st.number_input(
                "Saldo bancário inicial",
                value=float(st.session_state.get("saldo_inicial", 0.0)),
                step=1000.0
            )
            impostos_pct = st.number_input(
                "Impostos estimados sobre vendas (%)",
                min_value=0.0,
                max_value=100.0,
                value=float(st.session_state.get("impostos_pct", 0.0)),
                step=0.5
            )
            folha_mensal = st.number_input(
                "Folha mensal",
                min_value=0.0,
                value=float(st.session_state.get("folha_mensal", 0.0)),
                step=1000.0
            )
            despesas_fixas = st.number_input(
                "Despesas fixas mensais não lançadas",
                min_value=0.0,
                value=float(st.session_state.get("despesas_fixas", 0.0)),
                step=1000.0
            )
            outras_despesas = st.number_input(
                "Outras despesas mensais não lançadas",
                min_value=0.0,
                value=float(st.session_state.get("outras_despesas", 0.0)),
                step=500.0
            )
            st.session_state.saldo_inicial = saldo_inicial
            st.session_state.impostos_pct = impostos_pct
            st.session_state.folha_mensal = folha_mensal
            st.session_state.despesas_fixas = despesas_fixas
            st.session_state.outras_despesas = outras_despesas

        if st.sidebar.button("Atualizar dados do GestãoClick", type="primary"):
            try:
                with st.spinner(
                    "Buscando vendas, orçamentos, contas a receber, contas a pagar e movimentos do mês..."
                ):
                    st.session_state.clientes_ligados = carregar_clientes_ligados_hoje()
                    carregar_persistencia_crm()
                    st.session_state.observacoes_orc = {}
                    st.session_state.dados_processados = processar_api(
                        api_gestaoclick(),
                        inicio_api,
                        fim_api,
                        loja_id,
                        vendedor.get("id") or None,
                        vendedor.get("nome") or "Todos",
                        {
                            "meta_geral": meta_geral,
                            "metas_vendedor": dict(st.session_state.metas_vendedor),
                            "saldo_inicial": saldo_inicial,
                            "impostos_pct": impostos_pct,
                            "folha_mensal": folha_mensal,
                            "despesas_fixas": despesas_fixas,
                            "outras_despesas": outras_despesas,
                            "custo_comercial_mensal": custo_comercial_mensal,
                            "custo_marketing_mensal": custo_marketing_mensal,
                            "custo_ferramentas_mensal": custo_ferramentas_mensal,
                        }
                    )
                salvar_snapshot_estavel(st.session_state.dados_processados)
                st.success("Dados atualizados pelo GestãoClick.")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao buscar dados do GestãoClick: {e}")
    else:
        st.sidebar.info("Conecte a API para selecionar uma loja.")

else:
    st.sidebar.header("Importar arquivos")
    vendas_file = st.sidebar.file_uploader("Relatório de Vendas", type=["xlsx"])
    orc_file = st.sidebar.file_uploader("Relatório de Orçamentos", type=["xlsx"])
    contas_file = st.sidebar.file_uploader("Contas a Receber", type=["xlsx"])

    with st.sidebar.expander("Premissas CAC"):
        custo_comercial_excel = st.number_input(
            "Custo mensal da equipe comercial",
            min_value=0.0,
            value=float(st.session_state.get("custo_comercial_mensal", 0.0)),
            step=500.0,
            key="custo_comercial_excel"
        )
        custo_marketing_excel = st.number_input(
            "Custo mensal de marketing",
            min_value=0.0,
            value=float(st.session_state.get("custo_marketing_mensal", 0.0)),
            step=500.0,
            key="custo_marketing_excel"
        )
        custo_ferramentas_excel = st.number_input(
            "Custo mensal de ferramentas comerciais",
            min_value=0.0,
            value=float(st.session_state.get("custo_ferramentas_mensal", 0.0)),
            step=100.0,
            key="custo_ferramentas_excel"
        )
        st.session_state.custo_comercial_mensal = custo_comercial_excel
        st.session_state.custo_marketing_mensal = custo_marketing_excel
        st.session_state.custo_ferramentas_mensal = custo_ferramentas_excel

    if st.sidebar.button("Analisar arquivos", type="primary"):
        if not vendas_file or not orc_file or not contas_file:
            st.error("Envie os três arquivos.")
            st.stop()

        try:
            st.session_state.clientes_ligados = carregar_clientes_ligados_hoje()
            carregar_persistencia_crm()
            st.session_state.observacoes_orc = carregar_observacoes_orcamentos()
            st.session_state.dados_processados = processar_dados(
                vendas_file, orc_file, contas_file
            )
            st.session_state.dados_processados["configuracao"] = {
                "custo_comercial_mensal": custo_comercial_excel,
                "custo_marketing_mensal": custo_marketing_excel,
                "custo_ferramentas_mensal": custo_ferramentas_excel,
            }
            salvar_snapshot_estavel(st.session_state.dados_processados)
            st.success("Arquivos analisados e base local estÃ¡vel salva.")
            st.rerun()
        except Exception as e:
            st.error(f"Erro ao processar: {e}")

if (
    st.session_state.dados_processados is None
    and not st.session_state.snapshot_local_tentado
):
    snapshot = carregar_snapshot_estavel()
    if snapshot is not None:
        st.session_state.dados_processados = snapshot

if st.session_state.dados_processados is not None:
    if not st.session_state.persistencia_crm_tentada:
        carregar_persistencia_crm()
    renderizar()
else:
    st.info(
        "Conecte o GestÃ£oClick ou use os arquivos Excel na barra lateral."
    )
