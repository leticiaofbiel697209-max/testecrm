import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from io import BytesIO
from html import escape
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import smtplib
from email.message import EmailMessage
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

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

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
                f"GestãoClick retornou erro {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Não foi possível acessar o GestãoClick: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(
                "O GestãoClick demorou para responder. Tente novamente em alguns "
                "segundos ou confira os tokens."
            ) from exc
        finally:
            self.last_request = time.monotonic()

        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("message") or "Resposta inesperada do GestãoClick."
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
            raise RuntimeError("O orçamento não foi encontrado no GestãoClick.")

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

@st.cache_data(ttl=900, show_spinner=False)
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
                f"GestãoClick retornou erro {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Não foi possível acessar o GestãoClick: {exc.reason}"
            ) from exc
        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("message") or "Resposta inesperada do GestãoClick."
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
            raise RuntimeError("A consulta excedeu 200 páginas.")
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
                "nome", "descricao", "descrição", "nome_produto",
                "produto_nome", "descricao_produto", "descrição_produto",
                "nome_servico", "nome_serviço", "servico_nome",
                "serviço_nome", "descricao_servico", "descricao_serviço",
                "referencia", "referência", "codigo", "código", "sku"
            ]
            nome = (
                primeiro_valor_campos(wrapper, detalhe, campos=campos_nome)
                or buscar_valor_recursivo(wrapper, campos_nome)
            )
            if not nome:
                id_item = primeiro_valor_campos(
                    wrapper, detalhe,
                    campos=[
                        "produto_id", "servico_id", "serviço_id",
                        "id_produto", "id_servico", "id_serviço", "id"
                    ]
                )
                tipo = "Produto" if campo == "produtos" else "Serviço"
                nome = f"{tipo} ID {id_item}" if id_item else "Item sem identificação"
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
        "tipo", "Tipo", "comissao", "comissão", "percentual_comissao",
        "percentual comissão", "percentual", "perc_comissao", "comissao_percentual"
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
                        "nome", "descricao", "descrição", "nome_produto",
                        "produto_nome", "descricao_produto", "nome_servico",
                        "nome_serviço", "servico_nome", "serviço_nome",
                    ],
                )
                or buscar_valor_recursivo(
                    wrapper,
                    ["nome", "descricao", "descrição", "nome_produto", "produto_nome"]
                )
                or "Item sem identificação"
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
                    campos=["tipo", "Tipo", "comissao", "comissão", "percentual_comissao"]
                )
                or buscar_valor_recursivo(
                    wrapper,
                    ["tipo", "Tipo", "comissao", "comissão", "percentual_comissao"]
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
                if texto and chave not in vistos:
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
        raise RuntimeError("Informe os dois tokens da API do GestãoClick.")
    return GestaoClickAPI(access, secret)

SUPABASE_TABELAS_CRM = [
    "observacoes",
    "ja_liguei",
    "retornos_programados",
    "historico_cliente",
    "usuarios_vendedoras",
]

def credenciais_supabase():
    try:
        config = st.secrets.get("supabase", {})
        url = str(config.get("url", "")).strip().rstrip("/")
        key = str(
            config.get("anon_key", "")
            or config.get("service_role_key", "")
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
        "Confira o send_path da documentação da sua conta. Tentativas: "
        + " | ".join(erros[:4])
    )

def fmt(v):
    try:
        return f"R${float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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
    return str(x).strip().lower().replace("º", "o").replace("°", "o")

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
            ws.append_row(cabecalhos)
        elif not ws.row_values(1):
            ws.append_row(cabecalhos)
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
            f"Não foi possível carregar a persistência do Google Sheets: {e}"
        )
        return False

def cliente_corresponde(registro, cliente_id, cliente):
    reg_id = str(registro.get("cliente_id", "")).strip()
    if cliente_id and reg_id:
        return reg_id == str(cliente_id).strip()
    return norm(registro.get("cliente", "")) == norm(cliente)

def erro_apenas_response_200(exc):
    return "<Response [200]>" in str(exc) or "Response [200]" in str(exc)

def salvar_contato_realizado(
    cliente_id, cliente, vendedor, observacao="", origem="prioridade", status="já liguei"
):
    agora = datetime.now()
    registro = {
        "cliente_id": str(cliente_id or ""),
        "cliente": str(cliente),
        "vendedor": str(vendedor or "Sem vendedor"),
        "data": agora.strftime("%d/%m/%Y"),
        "hora": agora.strftime("%H:%M:%S"),
        "status": str(status or "já liguei"),
        "observacao": str(observacao or ""),
        "origem": str(origem),
    }
    abas = garantir_abas_crm()
    try:
        abas["ContatosRealizados"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            raise
    st.session_state.contatos_realizados.append(registro)

    if observacao:
        try:
            salvar_observacao_cliente(
                cliente_id, cliente, vendedor, observacao
            )
        except Exception as e:
            st.warning(
                f"Contato salvo, mas a observação não pôde ser duplicada "
                f"no histórico: {e}"
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
    abas = garantir_abas_crm()
    try:
        abas["ObservacoesClientes"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            raise
    st.session_state.observacoes_clientes.append(registro)
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
    abas = garantir_abas_crm()
    try:
        abas["RetornosProgramados"].append_row(list(registro.values()))
    except Exception as e:
        if not erro_apenas_response_200(e):
            raise
    st.session_state.retornos_programados.append(registro)
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
                    ws.update_cell(linha, 8, "concluído")
                    ws.update_cell(linha, 10, agora)
                    retorno["status"] = "concluído"
                    retorno["concluido_em"] = agora
                    break
    except Exception as e:
        st.warning(f"Contato salvo, mas o retorno não pôde ser concluído: {e}")

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
        st.warning(f"Não consegui salvar no Google Sheets: {e}")

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
        st.warning(f"Não consegui salvar observação: {e}")

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

def status_orcamento(dias):
    if dias <= 1:
        return "✅ Aceitável"
    if dias == 2:
        return "📞 Ligar hoje"
    if dias == 3:
        return "⚠️ Urgente"
    return "🚨 Risco de ter perdido"

def score_risco(media_atraso):
    if pd.isna(media_atraso) or media_atraso <= 0:
        return 100
    return max(0, min(100, int(100 - media_atraso * 2)))

def descricao_score(score):
    if score >= 85:
        return "🟢 Baixo risco de inadimplência"
    if score >= 65:
        return "🟡 Risco moderado de inadimplência"
    if score >= 40:
        return "🟠 Alto risco de inadimplência"
    return "🔴 Risco crítico de inadimplência"

def temperatura_cliente(dias, intervalo):
    if intervalo <= 0:
        if dias <= 30:
            return "🟣 NOVO"
        if dias <= 60:
            return "🟡 ATENÇÃO"
        return "⚫ CLIENTE INATIVO"
    if intervalo * 0.9 <= dias <= intervalo * 1.2:
        return "🟢 QUENTE"
    if intervalo * 1.2 < dias <= intervalo * 1.5:
        return "🟡 ATENÇÃO"
    if intervalo * 1.5 < dias <= intervalo * 2:
        return "🔴 ATRASADO NA RECOMPRA"
    if dias > intervalo * 2:
        return "⚫ CLIENTE INATIVO"
    return "🔵 CEDO"

def sugestao_ia(dias, intervalo, orcs, inad, potencial):
    temp = temperatura_cliente(dias, intervalo)
    if inad > 0:
        return "💸 Cliente com inadimplência. Priorizar cobrança antes de nova venda."
    if orcs > 0 and temp in ["🟢 QUENTE", "🟡 ATENÇÃO"]:
        return "📄 Cliente com orçamento em aberto e bom momento de compra. Priorizar fechamento hoje."
    if temp == "🟢 QUENTE":
        return f"🟢 Momento ideal. Ligar com oferta direta. Potencial mensal: {fmt(potencial)}."
    if temp == "🟡 ATENÇÃO":
        return "🟡 Cliente passou levemente do ciclo. Fazer contato de retomada antes que esfrie."
    if temp == "🔴 ATRASADO NA RECOMPRA":
        return "🔴 Cliente atrasado na recompra. Entender se comprou de concorrente ou se esqueceu."
    if temp == "⚫ CLIENTE INATIVO":
        return "⚫ Cliente inativo. Usar abordagem de reativação com condição especial."
    if orcs > 0:
        return "📄 Cliente com orçamento em aberto. Fazer follow-up comercial."
    if temp == "🔵 CEDO":
        return "🔵 Ainda cedo para venda direta. Manter relacionamento ou aquecer contato."
    return "🟣 Cliente novo. Iniciar relacionamento comercial."

def score_comercial(row):
    score = 0
    temp = row["temperatura"]
    if temp == "🟢 QUENTE":
        score += 40
    elif temp == "🟡 ATENÇÃO":
        score += 30
    elif temp == "🔴 ATRASADO NA RECOMPRA":
        score += 20
    elif temp == "⚫ CLIENTE INATIVO":
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

    def faixa(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido até 7 dias"
            if atraso <= 15:
                return "Vencido de 8 a 15 dias"
            if atraso <= 30:
                return "Vencido de 16 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A vencer em até 7 dias"
        if dias <= 15:
            return "A vencer de 8 a 15 dias"
        if dias <= 30:
            return "A vencer de 16 a 30 dias"
        if dias <= 60:
            return "A vencer de 31 a 60 dias"
        return "A vencer acima de 60 dias"

    financeiro["Faixa"] = financeiro.apply(faixa, axis=1)
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

    def faixa(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido até 7 dias"
            if atraso <= 30:
                return "Vencido de 8 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A pagar em até 7 dias"
        if dias <= 15:
            return "A pagar de 8 a 15 dias"
        if dias <= 30:
            return "A pagar de 16 a 30 dias"
        return "A pagar acima de 30 dias"

    pagar["Faixa"] = pagar.apply(faixa, axis=1)
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
            "O mês apresenta prejuízo financeiro: pagamentos liquidados superam "
            "os recebimentos. Congele despesas não essenciais e renegocie vencimentos."
        )
    elif resultado > 0:
        dicas.append(
            "O mês apresenta lucro financeiro. Preserve uma parcela como reserva "
            "antes de ampliar compras, despesas ou retiradas."
        )
    else:
        dicas.append(
            "O resultado financeiro mensal está no ponto de equilíbrio. "
            "Evite novos compromissos fixos até formar margem de segurança."
        )
    if saldo_30 < 0:
        dicas.append(
            f"Há déficit projetado de {fmt(abs(saldo_30))} para os próximos 30 dias. "
            "Antecipe cobranças e negocie fornecedores antes dos vencimentos."
        )
    else:
        dicas.append(
            f"A projeção de 30 dias indica sobra de {fmt(saldo_30)} entre entradas "
            "e saídas já registradas."
        )
    if vencido_pct >= 15:
        dicas.append(
            "A inadimplência está pressionando o caixa. Priorize cobranças por valor, "
            "idade da dívida e probabilidade de recuperação."
        )
    if pagar_vencido > 0:
        dicas.append(
            f"Existem {fmt(pagar_vencido)} em contas a pagar vencidas; regularize "
            "primeiro obrigações críticas para operação e crédito."
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
        ("Conservador", 0.70), ("Provável", 0.90), ("Otimista", 1.00)
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
            ] = "Não informado"
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
                "Mês": referencia.strftime("%m/%Y"),
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
        return "SAUDÁVEL"
    if dias > intervalo * 2:
        return "CHURN"
    if dias > intervalo:
        return "EM RISCO"
    return "SAUDÁVEL"

def classificar_status_em_data(datas, referencia):
    datas = pd.to_datetime(datas, errors="coerce").dropna().sort_values()
    datas = datas[datas <= referencia]
    if datas.empty:
        return None
    if len(datas) < 2:
        return "SAUDÁVEL"
    intervalo = datas.diff().dt.days.dropna().mean()
    if intervalo <= 0:
        return "SAUDÁVEL"
    dias_sem_comprar = (referencia - datas.max()).days
    if dias_sem_comprar > intervalo * 2:
        return "CHURN"
    if dias_sem_comprar > intervalo:
        return "EM RISCO"
    return "SAUDÁVEL"

@st.cache_data(show_spinner=False)
def calcular_indicadores_retencao_ceo(
    clientes, vendas, periodo_inicio, periodo_fim,
    custo_comercial, custo_marketing, custo_ferramentas
):
    vazio = {
        "clientes": pd.DataFrame(),
        "contagem_status": {"SAUDÁVEL": 0, "EM RISCO": 0, "CHURN": 0},
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
    for status in ("SAUDÁVEL", "EM RISCO", "CHURN"):
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
                    saudaveis = sum(1 for status in status_ref.values() if status == "SAUDÁVEL")
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
                        "Mês": mes.strftime("%m/%Y"),
                        "_mes": mes,
                        "CAC": cac_mes,
                        "Novos clientes": novos,
                        "Churn financeiro": churn_mes,
                        "Carteira em risco": risco_mes,
                        "Saudáveis": saudaveis,
                        "Em risco": em_risco,
                        "Churn": churn_qtd,
                        "Clientes em risco anterior": len(risco_anterior),
                        "Clientes recuperados": recuperados,
                        "Taxa de recuperação": taxa_mes,
                    })
                historico = pd.DataFrame(linhas)
                if not historico.empty:
                    atual = historico.iloc[-1]
                    anterior = historico.iloc[-2] if len(historico) > 1 else None
                    cac_atual = float(atual["CAC"])
                    novos_atual = int(atual["Novos clientes"])
                    taxa_recuperacao = float(atual["Taxa de recuperação"])
                    if anterior is not None:
                        cac_anterior = float(anterior["CAC"])
                        novos_anterior = int(anterior["Novos clientes"])

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
        "data_liquidação", "data_baixa", "data"
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
                "Comissão": comissao,
                "Pago no prazo": pago,
                "Status": "A pagar no dia 5" if pago else (
                    "Aguardando pagamento do cliente" if referencia <= prazo_pagamento else "Não pago no prazo"
                ),
            })

    itens_df = pd.DataFrame(linhas)
    pendentes_df = pd.DataFrame(pendentes)
    if not itens_df.empty:
        resumo = itens_df[itens_df["Pago no prazo"]].groupby("Vendedor").agg(
            Vendas=("Valor", "sum"),
            Comissao=("Comissão", "sum"),
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
    cv_item = achar_coluna(vendas, ["produto", "servico", "serviço", "item", "descricao", "descrição"])
    co_num = achar_coluna(orc, ["nº", "n°", "numero", "número"])
    co_cli = achar_coluna(orc, ["cliente"])
    co_cli_id = achar_coluna(orc, ["cliente id"])
    co_data = achar_coluna(orc, ["data"])
    co_status = achar_coluna(orc, ["situação", "situacao", "status"])
    co_valor = achar_coluna(orc, ["valor"])
    co_item = achar_coluna(orc, ["produto", "servico", "serviço", "item", "descricao", "descrição"])
    cc_cli = achar_coluna(contas, ["cliente", "destinado"])
    cc_cli_id = achar_coluna(contas, ["cliente id"])
    cc_venc = achar_coluna(contas, ["vencimento"])
    cc_status = achar_coluna(contas, ["situação", "situacao", "status"])
    cc_valor = achar_coluna(contas, ["valor total", "valor"])

    faltando = []
    for nome, col in {
        "Cliente vendas": cv_cli,
        "Data vendas": cv_data,
        "Valor vendas": cv_valor,
        "Nº orçamento": co_num,
        "Cliente orçamento": co_cli,
        "Data orçamento": co_data,
        "Status orçamento": co_status,
        "Cliente contas": cc_cli,
        "Valor contas": cc_valor,
    }.items():
        if col is None:
            faltando.append(nome)
    if faltando:
        raise Exception("Colunas não encontradas: " + ", ".join(faltando))

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

    intervalo = vendas.sort_values(cv_data).groupby("_cliente_chave")[cv_data].apply(
        lambda x: x.diff().mean().days if len(x.dropna()) > 1 else 0
    )

    clientes["intervalo"] = clientes["Cliente ID"].map(intervalo).fillna(0)
    clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days
    clientes["ticket_medio"] = clientes["faturamento"] / clientes["qtd_compras"]

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
    status_fechado = (
        "CONCRETIZADO|CANCELADO|PERDIDO|REPROVADO|FATURADO|"
        "FINALIZADO|FECHADO|VENDIDO"
    )
    orc_aberto = orc_aberto[
        ~orc_aberto[co_status].astype(str).str.upper().str.contains(
            status_fechado, na=False, regex=True
        )
    ]
    orc_aberto = orc_aberto[
        orc_aberto[co_data] >= (hoje - pd.Timedelta(days=90))
    ].copy()

    orc_aberto["dias_no_sistema"] = (hoje - orc_aberto[co_data]).dt.days
    orc_aberto["acao_recomendada_orcamento"] = orc_aberto["dias_no_sistema"].apply(status_orcamento)

    orc_count = orc_aberto.groupby(co_cli)[co_num].count()
    clientes["orcamentos_em_aberto"] = clientes["Cliente"].map(orc_count).fillna(0)

    orc_nums = orc_aberto.groupby(co_cli)[co_num].apply(lambda x: list(x.astype(str)))
    clientes["numeros_orcamentos"] = clientes["Cliente"].map(orc_nums).apply(lambda x: x if isinstance(x, list) else [])

    if cc_status:
        contas_atraso = contas[
            contas[cc_status].astype(str).str.upper().str.contains("ATRASADO|VENCIDO", na=False)
        ].copy()
    elif cc_venc:
        contas_atraso = contas[contas[cc_venc] < hoje].copy()
    else:
        contas_atraso = contas.iloc[0:0].copy()

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
        lambda x: x["potencial_mensal"] if x["temperatura"] in ["🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"] else 0,
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
        [["nº", "n°", "numero", "número"], ["cliente"], ["data"], ["situação", "status"]]
    )
    contas = carregar_excel(
        contas_file,
        [["cliente", "destinado"], ["vencimento"], ["valor"], ["situação", "status"]]
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
            "Vendedor", "Vendedor ID", "Documento", "_itens_texto", "_itens_comissao", "_venda_id"
        ])
    if orcamentos.empty:
        orcamentos = pd.DataFrame(columns=[
            "Numero", "Cliente", "Cliente ID", "Documento", "Data", "Situacao", "Valor", "Vendedor",
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
        proximo_recompra = bool(
            row["intervalo"] > 0
            and row["dias_sem_comprar"] >= row["intervalo"] * 0.9
        )
        motivos = []
        if retornos:
            motivos.append("Retorno programado")
        if orc.get("orc_risco", 0):
            motivos.append("Orçamento em risco de perda")
        elif orc.get("orc_urgente", 0):
            motivos.append("Orçamento urgente")
        elif orc.get("orc_ligar", 0):
            motivos.append("Orçamento: ligar hoje")
        if proximo_recompra:
            motivos.append("Próximo da recompra")
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
    temperaturas = clientes["temperatura"].isin([
        "🟢 QUENTE", "🟡 ATENÇÃO", "🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"
    ])
    regras = (
        clientes["retornos_hoje"].gt(0)
        | clientes["orc_ligar"].gt(0)
        | clientes["orc_urgente"].gt(0)
        | clientes["orc_risco"].gt(0)
    )
    return clientes[
        (temperaturas | regras) & (~clientes["ja_ligou_hoje"])
    ].sort_values("score_prioridade_dia", ascending=False)

def montar_resumo_diario(clientes):
    colunas = [
        "Vendedor", "Clientes para ligar", "Orcamentos sem retorno",
        "Proximos da recompra", "Retornos hoje", "Risco de perda"
    ]
    if clientes.empty:
        return pd.DataFrame(columns=colunas)

    base = clientes[~clientes["ja_ligou_hoje"]].copy()
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

    clientes_churn = clientes_com_ciclo[
        clientes_com_ciclo["dias_sem_comprar"] > clientes_com_ciclo["intervalo"] * 2
    ]
    taxa = len(clientes_churn) / len(clientes_com_ciclo) * 100
    return taxa, len(clientes_churn), len(clientes_com_ciclo)

def listar_clientes_churn(clientes):
    churn = clientes[
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] > clientes["intervalo"] * 2)
    ].copy()
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
        if pd.notna(ultima_compra) else "Não informada"
    )

    st.write(f"**Vendedor responsável:** {row.get('Vendedor', 'Sem vendedor')}")
    st.write(f"**Status atual:** {row.get('temperatura', 'Não informado')}")
    st.write(f"**Última compra:** {ultima_compra_txt}")

    st.markdown("**Orçamentos em aberto**")
    numeros = row.get("numeros_orcamentos", [])
    st.write(", ".join(str(numero) for numero in numeros[-5:])) if numeros else st.caption(
        "Nenhum orçamento em aberto."
    )

    st.markdown("**Últimos contatos**")
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

    st.markdown("**Últimas observações**")
    if observacoes:
        for observacao in observacoes[:5]:
            st.write(
                f"{observacao.get('data', '')} {observacao.get('hora', '')} - "
                f"{observacao.get('observacao', '')}"
            )
    else:
        st.caption("Nenhuma observação registrada.")

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
        raise RuntimeError("Loja não identificada. Atualize os dados pela API.")
    api = api_gestaoclick()
    situacao = situacao_inicial_orcamento(api, loja_id)
    if not situacao:
        raise RuntimeError("Não foi possível localizar uma situação inicial para orçamento.")
    produtos = []
    for item in itens:
        data_preco = item.get("data_preco")
        data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data não identificada"
        detalhes = (
            item.get("detalhes")
            or f"Preço sugerido com base na última venda em {data_txt}."
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
            raise RuntimeError(f"O orçamento {codigo} já existe no GestãoClick.")
        payload["codigo"] = int(codigo)
    if vendedor_id:
        payload["vendedor_id"] = int(vendedor_id)
    criado = api.create_budget(payload, loja_id)
    if not criado.get("id"):
        raise RuntimeError("O GestãoClick não retornou o ID do orçamento criado.")
    return api.budget(criado["id"], loja_id)

def status_aberto_resumo_diario(status):
    bloqueados = (
        "APROVADO|CANCELADO|CONFIRMADO|CONCRETIZADO|CONVERTIDO|"
        "FINALIZADO|FATURADO|PERDIDO|RECUSADO|VENDIDO|FECHADO"
    )
    return not bool(re.search(bloqueados, str(status or "").upper()))

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
    texto = str(texto or "").strip()
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
        if contato_realizado_hoje(cliente_id, cliente):
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
                "Última compra": ultima.strftime("%d/%m/%Y"),
                "Ticket médio": float(info.get("ticket_medio", 0) or 0),
                "_ultimo_valor_sugerido": ultimo_valor,
                "_ultima_data_preco": ultima_data_preco,
                "_loja_id": dados.get("loja_id", ""),
                "Oferta": (
                    f"{cliente} compra {item} a cada {intervalo_item} dias "
                    f"e está há {dias_sem} dias sem comprar. Ligar oferecendo {item}."
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
            ["_prioridade", "Ticket médio"], ascending=[False, False]
        )
    return ofertas

def montar_resumo_diario_oportunidades(dados, vendedor="Todas"):
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame()).copy()
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if orcamentos.empty:
        return pd.DataFrame(), {
            "calls": 0, "hot": 0, "returns": 0, "untouched": 0, "expiring": 0
        }

    co_num = dados.get("co_num") or achar_coluna(orcamentos, ["nº", "n°", "numero", "número"])
    co_cli = dados.get("co_cli") or achar_coluna(orcamentos, ["cliente"])
    co_data = dados.get("co_data") or achar_coluna(orcamentos, ["data"])
    co_valor = dados.get("co_valor") or achar_coluna(orcamentos, ["valor"])
    co_status = achar_coluna(orcamentos, ["situação", "situacao", "status"])
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
        cliente = str(row.get(co_cli, "Cliente sem nome")).strip()
        cliente_id = str(row.get(co_cli_id, "")).strip() if co_cli_id else ""
        chave = row["_cliente_chave_resumo"]
        data_orc = pd.to_datetime(row[co_data], errors="coerce")
        idade = int((hoje - data_orc.normalize()).days) if pd.notna(data_orc) else 0
        total = float(row.get(co_valor, 0) or 0) if co_valor else 0.0
        ja_ligou = contato_realizado_hoje(cliente_id, cliente)
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
            categorias.append((100, "RETORNO", "Orçamento com 2 dias: ligar hoje", "Ligar"))
            counters["returns"] += 1
        if not ja_ligou and idade == 3:
            categorias.append((105, "SEM CONTATO", "Urgente: orçamento com 3 dias", "Ligar urgente"))
            counters["untouched"] += 1
        elif not ja_ligou and idade >= 4:
            categorias.append((108, "SEM CONTATO", f"Risco de perda: orçamento com {idade} dias", "Priorizar"))
            counters["untouched"] += 1

        sinais = []
        if total >= 5000:
            sinais.append("alto valor")
        if compra_count > 0:
            sinais.append("já comprou")
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
            categorias.append((60, "NOVO", "Orçamento com 1 dia: acompanhamento normal", "Acompanhar"))

        if not categorias:
            continue
        prioridade, categoria, motivo, acao = max(categorias, key=lambda valor: valor[0])
        if categoria in ("RETORNO", "SEM CONTATO", "VENCENDO"):
            counters["calls"] += 1
        linhas.append({
            "Categoria": categoria,
            "Score": score,
            "Cliente": cliente,
            "Vendedor": row["_vendedor_resumo"],
            "Orçamento": str(row.get(co_num, "")),
            "Valor": total,
            "Idade": idade,
            "Último contato": "Hoje" if ja_ligou else f"{idade} dias sem contato",
            "Motivo": motivo,
            "Ação": acao,
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
    observacao_padrao = (
        f"Contato feito em {date.today():%d/%m/%Y} oferecendo: {oferta}"
        if oferta else ""
    )
    observacao = st.text_area(
        "Anotação para salvar no CRM",
        value=observacao_padrao,
        key=f"resumo_diario_anotacao_{chave}",
    )
    if st.button(
        "Já Liguei",
        key=f"resumo_diario_liguei_{chave}",
        type="primary",
        use_container_width=True,
    ):
        try:
            salvar_contato_realizado(
                cliente_id, cliente, vendedor, observacao, "resumo_diario"
            )
            st.success("Contato registrado e anotação salva no CRM.")
            st.rerun()
        except Exception as e:
            st.error(f"Não foi possível registrar o contato: {e}")

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
            "Observação do retorno",
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
                st.error(f"Não foi possível agendar o retorno: {e}")

def texto_email_resumo(cliente, vendedor, oferta, row=None):
    row = row if row is not None else {}
    produto = str(row.get("Produto", "") or "").strip()
    orcamento = str(row.get("Orçamento", "") or "").strip()
    categoria = str(row.get("Categoria", "") or "").strip()
    motivo = str(row.get("Motivo", oferta) or oferta)
    valor = row.get("Valor", row.get("Ticket médio", 0))
    intervalo = row.get("Intervalo", "")
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    valor_txt = fmt(valor) if valor else ""
    intervalo_num = valor_numerico_simples(intervalo, 0)
    dias_num = valor_numerico_simples(dias, 0)

    if orcamento:
        assunto = f"Sobre o orçamento {orcamento}"
        detalhe_valor = f" no valor de {valor_txt}" if valor_txt else ""
        urgencia = (
            f"Vi que ele já está há {dias} dias em aberto, então quis te chamar antes de perdermos o timing."
            if str(dias).strip() else
            "Quis te chamar para ver se ficou alguma dúvida ou se posso te ajudar a seguir com ele."
        )
        corpo = (
            f"Olá, tudo bem?\n\n"
            f"Passando rapidinho para saber se conseguimos avançar com o orçamento {orcamento}{detalhe_valor}.\n\n"
            f"{urgencia}\n\n"
            f"Se fizer sentido para você, posso revisar algum detalhe, ajustar quantidade ou ver uma condição para fecharmos.\n\n"
            f"Posso dar sequência por aqui?\n\n"
            f"Abraço,\n"
            f"{vendedor}\n"
            f"Novaprint"
        )
        return assunto, corpo

    if produto:
        assunto = f"Reposição de {produto}"
        ciclo = (
            f"Vi aqui que vocês costumam comprar {produto} a cada {int(intervalo_num)} dias"
            if intervalo_num > 0
            else f"Vi aqui uma oportunidade para reposição de {produto}"
        )
        tempo = (
            f" e já faz {int(dias_num)} dias desde a última compra."
            if dias_num > 0
            else "."
        )
        corpo = (
            f"Olá, tudo bem?\n\n"
            f"{ciclo}{tempo}\n\n"
            f"Quer que eu já separe uma condição para reposição? "
            f"Se quiser, também posso revisar a quantidade ideal para evitar falta ou compra maior que o necessário.\n\n"
            f"Posso te mandar uma proposta atualizada de {produto}?\n\n"
            f"Abraço,\n"
            f"{vendedor}\n"
            f"Novaprint"
        )
        return assunto, corpo

    assunto = f"Seguimos com essa demanda?"
    corpo = (
        f"Olá, tudo bem?\n\n"
        f"Passei para retomar com você esse ponto que ficou em aberto:\n\n"
        f"{motivo}\n\n"
        f"Se ainda fizer sentido, posso te ajudar a avançar com isso hoje ou ajustar o que for necessário.\n\n"
        f"Como prefere seguir?\n\n"
        f"Abraço,\n"
        f"{vendedor}\n"
        f"Novaprint"
    )
    return assunto, corpo

def renderizar_email_resumo(cliente, vendedor, oferta, chave, row=None):
    with st.expander("Preparar e-mail"):
        conta_saida = conta_email_para_vendedor(vendedor)
        if conta_saida:
            st.caption(
                f"Saída configurada: {conta_saida.get('name', conta_saida.get('email'))} "
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
                            f"E-mail enviado pelo CRM. Assunto: {assunto}. Oferta/ação: {oferta}",
                            "email",
                            "email enviado",
                        )
                        st.success(f"E-mail enviado por {origem}.")
                    except Exception as e:
                        st.error(f"Não foi possível enviar pelo CRM: {e}")
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
    produto = str(row.get("Produto", "") or "").strip()
    orcamento = str(row.get("Orçamento", "") or "").strip()
    valor = row.get("Valor", row.get("Ticket médio", 0))
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    intervalo = row.get("Intervalo", "")
    valor_txt = fmt(valor) if valor else ""

    if orcamento:
        trecho_valor = f" ({valor_txt})" if valor_txt else ""
        trecho_tempo = f" Vi que ele está há {dias} dias em aberto." if str(dias).strip() else ""
        return (
            f"Oi, tudo bem? Aqui é {vendedor}, da Novaprint.\n\n"
            f"Passando para ver se conseguimos avançar com o orçamento {orcamento}{trecho_valor}."
            f"{trecho_tempo}\n\n"
            "Ficou alguma dúvida ou quer que eu ajuste alguma condição para fecharmos?"
        )

    if produto:
        trecho_ciclo = (
            f"Vi aqui que vocês costumam comprar {produto} a cada {intervalo} dias. "
            if str(intervalo).strip() else
            f"Vi aqui uma oportunidade de reposição de {produto}. "
        )
        trecho_tempo = f"Já faz {dias} dias desde a última compra. " if str(dias).strip() else ""
        return (
            f"Oi, tudo bem? Aqui é {vendedor}, da Novaprint.\n\n"
            f"{trecho_ciclo}{trecho_tempo}"
            f"Quer que eu prepare uma condição atualizada de {produto} para você?"
        )

    return (
        f"Oi, tudo bem? Aqui é {vendedor}, da Novaprint.\n\n"
        f"Passando para retomar este ponto: {oferta}\n\n"
        "Quer que eu te ajude a dar sequência?"
    )

def renderizar_whatsapp_resumo(cliente, vendedor, oferta, chave, row=None):
    with st.expander("Preparar WhatsApp"):
        telefone = st.text_input(
            "WhatsApp do cliente",
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
                            f"WhatsApp enviado pelo Watidy. Oferta/ação: {oferta}",
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
    produto = str(row.get("Produto", "") or "").strip()
    cliente_id = str(row.get("Cliente ID", row.get("_cliente_id", "")) or "").strip()
    if not produto or not cliente_id:
        return
    with st.expander("Criar orçamento"):
        valor_sugerido = float(row.get("_ultimo_valor_sugerido", 0) or 0)
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
        data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data não identificada"
        st.caption(f"Preço sugerido da última venda em {data_txt}.")
        codigo = st.text_input(
            "Número do orçamento (opcional)",
            key=f"orc_sug_codigo_{chave}",
        )
        confirmado = st.checkbox(
            "Revisei e autorizo criar este orçamento no GestãoClick.",
            key=f"orc_sug_confirmar_{chave}",
        )
        if st.button(
            "Criar orçamento no GestãoClick",
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
                    "detalhes": f"Preço sugerido da última venda em {data_txt}.",
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
                st.success(f"Orçamento {numero} criado no GestãoClick.")
            except Exception as e:
                st.error(f"Não foi possível criar o orçamento: {e}")

def renderizar_card_resumo(row, indice, modo="prioridade"):
    cliente = str(row.get("Cliente", "Cliente sem nome"))
    vendedor = str(row.get("Vendedor", "Sem vendedor"))
    cliente_id = str(row.get("_cliente_id", row.get("Cliente ID", "")))
    valor = row.get("Valor", row.get("Ticket médio", 0))
    oferta = str(row.get("Oferta", row.get("Motivo", "")))
    categoria = str(row.get("Categoria", "Recompra")).strip()
    score = row.get("Score", "")
    orcamento = str(row.get("Orçamento", "") or "").strip()
    acao = str(row.get("Ação", "") or "").strip()
    produto = str(row.get("Produto", "") or "").strip()
    dias = row.get("Dias sem comprar", row.get("Idade", ""))
    intervalo = row.get("Intervalo", "")
    chave = chave_widget(
        f"resumo_{modo}_{row.get('_budget_id', '')}_{cliente_id}_{cliente}_{indice}"
    )
    detalhes = []
    if produto:
        detalhes.append(f"Produto sugerido: <b>{html_seguro(produto)}</b>")
    if orcamento:
        detalhes.append(f"Orçamento: <b>{html_seguro(orcamento)}</b>")
    if dias != "":
        detalhes.append(f"Dias em atenção: <b>{html_seguro(dias)}</b>")
    if intervalo != "":
        detalhes.append(f"Ciclo médio: <b>{html_seguro(intervalo)} dias</b>")
    if score != "":
        detalhes.append(f"Score: <b>{html_seguro(score)}</b>")
    if acao:
        detalhes.append(f"Ação sugerida: <b>{html_seguro(acao)}</b>")
    detalhes_html = "<br>".join(detalhes)

    st.markdown(
        f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:8px;">
<b>{html_seguro(cliente)}</b><br>
Vendedor: <b>{html_seguro(vendedor)}</b><br>
Valor/ticket: <b>{fmt_html(valor)}</b><br>
Tipo de prioridade: <b>{html_seguro(categoria)}</b><br>
<br>
<b>Por que está na fila?</b><br>
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
        st.info("Nenhum cliente encontrado para esta visão.")
        return
    linhas = list(df.head(30).iterrows())
    for i in range(0, len(linhas), 3):
        cols = st.columns(3)
        for j, (indice, row) in enumerate(linhas[i:i+3]):
            with cols[j]:
                renderizar_card_resumo(row, indice, modo)

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
                with st.spinner("Buscando cliente no GestãoClick..."):
                    st.session_state.clientes_api_cache[cache_key] = api_gestaoclick().clients(
                        loja_id, termo.strip()
                    )
            except Exception as e:
                st.warning(f"Não foi possível buscar clientes na API: {e}")
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
                "Vendedor": item.get("nome_vendedor") or "GestãoClick",
                "intervalo": 0,
                "dias_sem_comprar": 0,
                "itens_comprados": [],
                "itens_orcados": [],
                "_origem_busca": "API GestãoClick",
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
        "A busca considera a base carregada do CRM e consulta o GestãoClick pela API quando conectado."
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
                with st.expander("Produtos comprados e orçados"):
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
                with st.spinner("Buscando cliente no GestãoClick..."):
                    st.session_state.clientes_api_cache[cache_key] = api_gestaoclick().clients(
                        loja_id, termo_cliente.strip()
                    )
            except Exception as e:
                st.warning(f"Não foi possível buscar clientes na API: {e}")
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

    codigo = st.text_input("Número do orçamento (opcional)", key="gerar_orc_codigo")
    confirmado = st.checkbox(
        "Revisei cliente, vendedor e produtos. Autorizo criar o orçamento no GestãoClick.",
        key="gerar_orc_confirmar",
    )
    if st.button(
        "Criar orçamento no GestãoClick",
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
                data_txt = data_preco.strftime("%d/%m/%Y") if pd.notna(data_preco) else "data não identificada"
                itens_final.append({
                    **item,
                    "valor": valor,
                    "data_preco": data_preco,
                    "detalhes": f"Preço sugerido da última venda em {data_txt}.",
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
    st.subheader("Resumo Diário")
    st.caption("Gestão diária dos orçamentos, ofertas de recompra e prioridades das vendedoras.")
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame())
    clientes = dados.get("clientes", pd.DataFrame())
    if orcamentos.empty and clientes.empty:
        st.info("Carregue os dados da API para montar o resumo diário.")
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
        st.markdown("#### Ofertas de recompra para hoje")
        st.caption(
            "Essas ofertas vêm do ciclo real de compra do cliente e aparecem já na entrada do Resumo Diário."
        )
        renderizar_grid_resumo(ofertas, "inicio_oferta")

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
                "Vendedor", "Prioridades", "Ligações", "Quentes", "Retornos", "Valor"
            ])
        else:
            gestao = oportunidades.groupby("Vendedor").agg(
                Prioridades=("Cliente", "count"),
                Ligações=("Categoria", lambda s: int(s.isin(["RETORNO", "SEM CONTATO", "VENCENDO"]).sum())),
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
    return

    st.subheader("Resumo Diário")
    st.caption("Gestão diária dos orçamentos e prioridades das vendedoras.")
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame())
    if orcamentos.empty:
        st.info("Carregue os dados da API para montar o resumo diário.")
        return
    vendedor_col = achar_coluna(orcamentos, ["vendedor"])
    vendedores = ["Todas"]
    if vendedor_col:
        vendedores += sorted(
            nome for nome in orcamentos[vendedor_col].dropna().astype(str).str.strip().unique()
            if nome and nome.lower() not in {"nan", "none"}
        )
    col_filtro, col_visao = st.columns(2)
    vendedor = col_filtro.selectbox("Vendedor", vendedores, key="resumo_diario_vendedor")
    visao = col_visao.radio(
        "Visão",
        ["Visão do vendedor", "Visão de gestão"],
        horizontal=True,
        key="resumo_diario_visao"
    )
    oportunidades, counters = montar_resumo_diario_oportunidades(dados, vendedor)
    cols = st.columns(5)
    cols[0].metric("Ligações hoje", counters["calls"])
    cols[1].metric("Oportunidades quentes", counters["hot"])
    cols[2].metric("Retornos hoje", counters["returns"])
    cols[3].metric("Sem contato", counters["untouched"])
    cols[4].metric("Vencendo", counters["expiring"])

    if oportunidades.empty:
        st.success("Nenhuma prioridade encontrada para os filtros selecionados.")
        return

    filtro = st.radio(
        "Mostrar",
        ["Todas", "Oportunidades quentes", "Retornos hoje"],
        horizontal=True,
        key="resumo_diario_filtro"
    )
    exibicao = oportunidades.copy()
    if filtro == "Oportunidades quentes":
        exibicao = exibicao[exibicao["Categoria"] == "QUENTE"]
    elif filtro == "Retornos hoje":
        exibicao = exibicao[exibicao["Categoria"] == "RETORNO"]

    if visao == "Visão de gestão":
        st.markdown("#### Desempenho por vendedor")
        gestao = oportunidades.groupby("Vendedor").agg(
            Prioridades=("Cliente", "count"),
            Ligacoes=("Categoria", lambda s: int(s.isin(["RETORNO", "SEM CONTATO", "VENCENDO"]).sum())),
            Quentes=("Categoria", lambda s: int((s == "QUENTE").sum())),
            Retornos=("Categoria", lambda s: int((s == "RETORNO").sum())),
            Valor=("Valor", "sum"),
        ).reset_index()
        gestao["Valor"] = gestao["Valor"].map(fmt)
        st.dataframe(gestao, use_container_width=True, hide_index=True)

    tabela = exibicao[[
        "Categoria", "Score", "Cliente", "Vendedor", "Orçamento",
        "Valor", "Último contato", "Motivo", "Ação"
    ]].copy()
    tabela["Valor"] = tabela["Valor"].map(fmt)
    st.markdown("#### Fila de prioridades")
    st.dataframe(tabela, use_container_width=True, hide_index=True)

    st.markdown("#### Ações rápidas")
    st.caption("Use os cartões abaixo para registrar contato ou programar retorno sem sair do Resumo Diário.")
    for indice, row in exibicao.head(30).iterrows():
        cliente = str(row.get("Cliente", "Cliente sem nome"))
        vendedor_card = str(row.get("Vendedor", "Sem vendedor"))
        cliente_id = str(row.get("_cliente_id", ""))
        chave = chave_widget(
            f"resumo_diario_{row.get('_budget_id', '')}_{cliente}_{indice}"
        )
        with st.expander(
            f"{row.get('Categoria', 'PRIORIDADE')} | {cliente} | {fmt(row.get('Valor', 0))}"
        ):
            st.write(f"**Vendedor:** {vendedor_card}")
            st.write(f"**Orçamento:** {row.get('Orçamento', '')}")
            st.write(f"**Motivo:** {row.get('Motivo', '')}")
            st.write(f"**Ação sugerida:** {row.get('Ação', '')}")

            historico_contatos = [
                c for c in st.session_state.contatos_realizados
                if cliente_corresponde(c, cliente_id, cliente)
            ][-3:]
            historico_retornos = [
                r for r in st.session_state.retornos_programados
                if cliente_corresponde(r, cliente_id, cliente)
            ][-3:]
            if historico_contatos or historico_retornos:
                with st.expander("Ver histórico curto"):
                    for contato in historico_contatos:
                        st.write(
                            f"{contato.get('data', '')} {contato.get('hora', '')} - "
                            f"{contato.get('status', '')} - {contato.get('observacao', '')}"
                        )
                    for retorno in historico_retornos:
                        st.write(
                            f"Retorno {retorno.get('data_retorno', '')} - "
                            f"{retorno.get('motivo', '')} - {retorno.get('status', '')}"
                        )

            observacao = st.text_input(
                "Observação do contato",
                key=f"resumo_diario_obs_{chave}"
            )
            col_ligar, col_retorno = st.columns(2)
            if col_ligar.button(
                "Já Liguei",
                key=f"resumo_diario_liguei_{chave}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    salvar_contato_realizado(
                        cliente_id, cliente, vendedor_card, observacao, "resumo_diario"
                    )
                    st.success("Contato registrado. Esse cliente sai das prioridades de hoje.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Não foi possível registrar o contato: {e}")

            with col_retorno:
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
                    "Observação do retorno",
                    key=f"resumo_diario_obs_retorno_{chave}"
                )
                if st.button(
                    "Agendar Retorno",
                    key=f"resumo_diario_agendar_{chave}",
                    use_container_width=True,
                ):
                    try:
                        agendar_retorno_cliente(
                            cliente_id,
                            cliente,
                            vendedor_card,
                            data_retorno,
                            motivo,
                            observacao_retorno,
                        )
                        st.success(f"Retorno agendado para {data_retorno:%d/%m/%Y}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Não foi possível agendar o retorno: {e}")

def card_cliente(row, tipo, posicao):
    atraso = int(row["dias_sem_comprar"] - row["intervalo"])
    estrela = "⭐ Cliente estratégico<br>" if row["cliente_estrategico"] else ""
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
Está há <b>{int(row['dias_sem_comprar'])} dias</b> sem comprar<br>
Já era para ter comprado há <b>{max(atraso, 0)} dias</b><br><br>
Ticket médio: <b>{fmt_html(row['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(row['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt_html(row['potencial_recuperavel'])}</b><br>
Orçamentos em aberto: <b>{int(row['orcamentos_em_aberto'])}</b><br>
Vendedor responsável: <b>{vendedor_html}</b><br>
Inadimplência: <b>{fmt_html(row['inadimplencia'])}</b><br>
Score de risco: <b>{int(row['score_risco'])}/100 — {risco_html}</b><br><br>
Prioridade de hoje: <b>{motivo_html or 'Acompanhamento comercial'}</b><br>
Recomendação: <b>{acao_html}</b>
</div>
""", unsafe_allow_html=True)

    cliente_uid = chave_widget(identificador_cliente(row, posicao))
    chave_base = f"{tipo}_{cliente_uid}_{chave_widget(posicao)}"
    sufixo_uid = cliente_uid[-6:]

    with st.expander(f"Ver Histórico - {row['Cliente']} #{sufixo_uid}"):
        renderizar_historico_cliente(row)

    observacao_contato = st.text_input(
        "Observação do contato (opcional)",
        key=f"obs_contato_{chave_base}"
    )
    if st.button(
        f"Já Liguei - {row['Cliente']}",
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
            st.error(f"Não foi possível registrar o contato: {e}")

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
            "Observação",
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
                st.error(f"Não foi possível agendar o retorno: {e}")

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
        f"RESUMO COMERCIAL DIÁRIO - {hoje_txt}",
        f"Período das vendas analisadas: {periodo}",
        "",
        "VISÃO EXECUTIVA",
        f"- Faturamento histórico importado: {fmt(clientes['faturamento'].sum())}",
        f"- Potencial mensal da carteira: {fmt(clientes['potencial_mensal'].sum())}",
        f"- Capacidade estimada das prioridades de hoje: {fmt(prioridade['ticket_medio'].sum())}",
        f"- Potencial recuperável: {fmt(clientes['potencial_recuperavel'].sum())}",
        f"- Inadimplência identificada: {fmt(clientes['inadimplencia'].sum())}",
        f"- Churn estimado: {taxa_churn:.1f}% ({qtd_churn} de {base_churn} clientes com ciclo conhecido)",
        "",
        "FINANCEIRO",
        f"- Carteira a receber: {fmt(metricas_fin['total_aberto'])}",
        f"- Total vencido: {fmt(metricas_fin['total_vencido'])} ({metricas_fin['percentual_vencido']:.1f}%)",
        f"- Entradas previstas em até 7 dias: {fmt(metricas_fin['vence_7'])}",
        f"- Entradas previstas de 8 a 15 dias: {fmt(metricas_fin['vence_15'])}",
        f"- Entradas previstas de 16 a 30 dias: {fmt(metricas_fin['vence_30'])}",
        f"- Concentração nos 5 maiores clientes: {metricas_fin['concentracao_top5']:.1f}%",
        f"- Contas a pagar: {fmt(metricas_fin['total_pagar'])}",
        f"- Saldo total projetado: {fmt(metricas_fin['saldo_carteira'])}",
        f"- Sobra projetada em 30 dias: {fmt(metricas_fin['saldo_30_dias'])}",
        f"- Resultado financeiro do mês: {fmt(metricas_fin['resultado_mes'])}",
        "",
        "CARTEIRA",
        f"- Quentes: {int(temperaturas.get('🟢 QUENTE', 0))}",
        f"- Em atenção: {int(temperaturas.get('🟡 ATENÇÃO', 0))}",
        f"- Atrasados na recompra: {int(temperaturas.get('🔴 ATRASADO NA RECOMPRA', 0))}",
        f"- Inativos: {int(temperaturas.get('⚫ CLIENTE INATIVO', 0))}",
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

    linhas.extend(["", f"ORÇAMENTOS URGENTES ({len(orc_urgentes)})"])
    if orc_urgentes.empty:
        linhas.append("- Nenhum orçamento com dois dias ou mais sem retorno.")
    else:
        for i, (_, r) in enumerate(orc_urgentes.head(10).iterrows(), 1):
            valor = fmt(r[co_valor]) if co_valor else "valor não informado"
            linhas.append(
                f"{i}. Nº {r[co_num]} | {r[co_cli]} | {int(r['dias_no_sistema'])} dias | {valor}"
            )

    linhas.extend(["", f"CHURN PARA RECUPERAÇÃO ({len(clientes_churn)})"])
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
        f"- Realizar {len(prioridade)} contatos prioritários.",
        f"- Retornar {len(orc_urgentes)} orçamentos urgentes.",
        f"- Iniciar recuperação dos {min(len(clientes_churn), 10)} clientes de churn com maior potencial.",
        "- Tratar inadimplência antes de oferecer nova venda aos clientes com pendências.",
        "",
        "Observação: capacidade estimada não é previsão garantida; representa a soma dos tickets médios das prioridades."
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
        title="Relatório Comercial Executivo"
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

    elementos.append(Paragraph("RELATÓRIO COMERCIAL EXECUTIVO", styles["TituloCEO"]))
    elementos.append(Paragraph(
        f"Emitido em {datetime.now():%d/%m/%Y} | Vendas analisadas: {periodo}",
        styles["Normal"]
    ))
    elementos.append(Spacer(1, 8))

    indicadores = [
        [p("Indicador"), p("Resultado"), p("Leitura")],
        [p("Faturamento histórico"), p(fmt(clientes["faturamento"].sum())), p("Total existente no arquivo importado.")],
        [p("Potencial mensal"), p(fmt(clientes["potencial_mensal"].sum())), p("Média mensal das compras dos últimos três meses.")],
        [p("Capacidade das prioridades"), p(fmt(prioridade["ticket_medio"].sum())), p("Soma dos tickets médios; não é previsão garantida.")],
        [p("Potencial recuperável"), p(fmt(clientes["potencial_recuperavel"].sum())), p("Potencial de atrasados e inativos.")],
        [p("Inadimplência"), p(fmt(clientes["inadimplencia"].sum())), p("Pendências identificadas no contas a receber.")],
        [p("Carteira a receber"), p(fmt(metricas_fin["total_aberto"])), p("Total de recebimentos ainda em aberto.")],
        [p("Percentual vencido"), p(f"{metricas_fin['percentual_vencido']:.1f}%"), p("Participação dos títulos vencidos na carteira aberta.")],
        [p("Receber em até 7 dias"), p(fmt(metricas_fin["vence_7"])), p("Entradas previstas no curto prazo.")],
        [p("Contas a pagar"), p(fmt(metricas_fin["total_pagar"])), p("Obrigações ainda em aberto.")],
        [p("Sobra em 30 dias"), p(fmt(metricas_fin["saldo_30_dias"])), p("Entradas previstas menos saídas previstas.")],
        [p("Resultado financeiro mensal"), p(fmt(metricas_fin["resultado_mes"])), p("Recebimentos liquidados menos pagamentos liquidados.")],
        [p("Churn estimado"), p(f"{taxa_churn:.1f}%"), p(f"{qtd_churn} de {base_churn} clientes com ciclo conhecido.")],
    ]
    elementos.append(Paragraph("1. Painel executivo", styles["SecaoCEO"]))
    elementos.append(tabela(indicadores, [45 * mm, 35 * mm, 80 * mm]))

    temperaturas = clientes["temperatura"].value_counts()
    carteira = [
        [p("Situação"), p("Clientes")],
        [p("Quentes"), p(int(temperaturas.get("🟢 QUENTE", 0)))],
        [p("Em atenção"), p(int(temperaturas.get("🟡 ATENÇÃO", 0)))],
        [p("Atrasados na recompra"), p(int(temperaturas.get("🔴 ATRASADO NA RECOMPRA", 0)))],
        [p("Inativos"), p(int(temperaturas.get("⚫ CLIENTE INATIVO", 0)))],
        [p("Novos"), p(int(temperaturas.get("🟣 NOVO", 0)))],
    ]
    elementos.append(Paragraph("2. Situação da carteira", styles["SecaoCEO"]))
    elementos.append(tabela(carteira, [80 * mm, 35 * mm]))

    elementos.append(Paragraph("3. Prioridades comerciais", styles["SecaoCEO"]))
    prioridades_pdf = [[p("Cliente"), p("Dias"), p("Ticket"), p("Potencial"), p("Recomendação")]]
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
        "possui ciclo de recompra conhecido e ultrapassa duas vezes seu intervalo médio sem comprar.",
        styles["BodyText"]
    ))
    elementos.append(Spacer(1, 6))
    churn_pdf = [[p("Cliente"), p("Sem comprar"), p("Ciclo"), p("Além do limite"), p("Potencial em risco")]]
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

    elementos.append(Paragraph("5. Orçamentos que exigem retorno", styles["SecaoCEO"]))
    orc_pdf = [[p("Orçamento"), p("Cliente"), p("Dias"), p("Valor"), p("Prioridade")]]
    for _, r in orc_urgentes.head(25).iterrows():
        orc_pdf.append([
            p(r[co_num]), p(r[co_cli]), p(int(r["dias_no_sistema"])),
            p(fmt(r[co_valor]) if co_valor else "Não informado"), p(r["acao_recomendada_orcamento"])
        ])
    if len(orc_pdf) == 1:
        elementos.append(Paragraph("Nenhum orçamento urgente.", styles["Normal"]))
    else:
        elementos.append(tabela(orc_pdf, [25 * mm, 50 * mm, 15 * mm, 28 * mm, 42 * mm]))

    inadimplentes = clientes[clientes["inadimplencia"] > 0].sort_values(
        "inadimplencia", ascending=False
    )
    elementos.append(Paragraph("6. Inadimplência por cliente", styles["SecaoCEO"]))
    inad_pdf = [[p("Cliente"), p("Valor"), p("Média de atraso"), p("Risco")]]
    for _, r in inadimplentes.head(25).iterrows():
        inad_pdf.append([
            p(r["Cliente"]), p(fmt(r["inadimplencia"])),
            p(f"{int(r['media_dias_atraso'])} dias"), p(r["risco_inadimplencia"])
        ])
    if len(inad_pdf) == 1:
        elementos.append(Paragraph("Nenhuma inadimplência identificada.", styles["Normal"]))
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
        fornecedores_pdf = [[p("Fornecedor"), p("Total a pagar"), p("% das obrigações")]]
        for fornecedor, valor in pagar_fornecedor.items():
            participacao = (
                float(valor) / metricas_fin["total_pagar"] * 100
                if metricas_fin["total_pagar"] else 0
            )
            fornecedores_pdf.append([
                p(fornecedor), p(fmt(valor)), p(f"{participacao:.1f}%")
            ])
        elementos.append(tabela(fornecedores_pdf, [80 * mm, 42 * mm, 38 * mm]))

    elementos.append(Paragraph("9. Análise e plano de ação", styles["SecaoCEO"]))
    dicas_financeiras = estrategia_financeira(metricas_fin)
    elementos.append(Paragraph(
        f"<b>Hoje:</b> realizar {len(prioridade)} contatos prioritários e retornar "
        f"{len(orc_urgentes)} orçamentos urgentes.<br/>"
        f"<b>Próximos 7 dias:</b> acompanhar clientes em atenção e propostas ainda abertas.<br/>"
        f"<b>Recuperação:</b> abordar primeiro os {min(len(clientes_churn), 10)} clientes "
        "em churn com maior potencial mensal e tratar pendências financeiras antes de uma nova oferta.<br/>"
        f"<b>Financeiro:</b> {' '.join(dicas_financeiras)}",
        styles["BodyText"]
    ))

    elementos.append(Paragraph("10. Metodologia", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        "<b>Churn estimado:</b> clientes com ciclo conhecido e mais de duas vezes o intervalo "
        "médio sem comprar, dividido pela quantidade de clientes com ciclo conhecido.<br/>"
        "<b>Potencial mensal:</b> compras dos últimos três meses divididas por três.<br/>"
        "<b>Capacidade das prioridades:</b> soma dos tickets médios dos clientes quentes; "
        "não representa promessa de venda.<br/>"
        "<b>Percentual vencido:</b> valor vencido dividido pela carteira total ainda em aberto.<br/>"
        "<b>Concentração:</b> participação dos cinco maiores clientes no total a receber.<br/>"
        "<b>Resultado financeiro mensal:</b> recebimentos liquidados menos pagamentos liquidados; "
        "não equivale necessariamente ao lucro contábil.<br/>"
        "<b>Cliente estratégico:</b> cliente situado entre os 10% de maior faturamento histórico.",
        styles["BodyText"]
    ))

    def rodape(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#667788"))
        canvas.drawString(15 * mm, 9 * mm, "CRM Inteligente - Relatório Comercial")
        canvas.drawRightString(195 * mm, 9 * mm, f"Página {documento.page}")
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
        "Visão estratégica da carteira de recebimentos em aberto. "
        "Os valores representam entradas previstas, não saldo bancário disponível."
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
            "Lucro" if metricas["resultado_mes"] >= 0 else "Prejuízo",
            delta_color="normal"
        )
    else:
        linha1[3].metric("Resultado financeiro mensal", "Indisponível")

    linha2 = st.columns(4)
    linha2[0].metric(
        "Total vencido",
        fmt(metricas["total_vencido"]),
        f"{metricas['percentual_vencido']:.1f}% da carteira"
    )
    linha2[1].metric("Contas a pagar vencidas", fmt(metricas["pagar_vencido"]))
    linha2[2].metric("Sobra projetada em 30 dias", fmt(metricas["saldo_30_dias"]))
    linha2[3].metric(
        "Margem financeira do mês",
        f"{metricas['margem_caixa']:.1f}%"
    )

    with st.expander("Como o resultado e a sobra são calculados?"):
        st.markdown(
            f"""
            **Resultado financeiro de {mes_resultado}**

            `Recebimentos liquidados - pagamentos liquidados`

            {fmt(metricas['recebido_mes'])} - {fmt(metricas['pago_mes'])}
            = **{fmt(metricas['resultado_mes']) if resultado_disponivel else 'Indisponível no modo Excel'}**

            **Sobra projetada em 30 dias**

            `Contas a receber nos próximos 30 dias - contas a pagar nos próximos 30 dias`

            Este resultado é uma visão de caixa. Não inclui automaticamente estoque,
            depreciação, impostos provisionados ou despesas que ainda não foram lançadas.
            """
        )

    linha3 = st.columns(4)
    linha3[0].metric("Receber em até 7 dias", fmt(metricas["vence_7"]))
    linha3[1].metric("Pagar em até 7 dias", fmt(metricas["pagar_7"]))
    linha3[2].metric("Prazo médio a receber", f"{metricas['prazo_medio']:.0f} dias")
    linha3[3].metric("Concentração nos 5 maiores", f"{metricas['concentracao_top5']:.1f}%")

    if financeiro is None or financeiro.empty:
        st.info("Nenhuma conta em aberto foi encontrada para montar a visão financeira.")
        if contas_pagar is None or contas_pagar.empty:
            return

    potencial_churn = float(clientes_churn["potencial_mensal"].sum())
    receita_em_risco = metricas["total_vencido"] + potencial_churn
    st.metric(
        "Exposição estratégica estimada",
        fmt(receita_em_risco),
        help=(
            "Soma do valor vencido com o potencial mensal dos clientes em churn. "
            "É um indicador de exposição, não uma perda contábil confirmada."
        )
    )

    st.markdown("#### Alertas estratégicos")
    alertas = []
    if metricas["percentual_vencido"] >= 25:
        alertas.append(
            f"CRÍTICO: {metricas['percentual_vencido']:.1f}% da carteira está vencida."
        )
    elif metricas["percentual_vencido"] >= 10:
        alertas.append(
            f"ATENÇÃO: {metricas['percentual_vencido']:.1f}% da carteira está vencida."
        )
    if metricas["concentracao_top5"] >= 50:
        alertas.append(
            "A carteira está concentrada: os cinco maiores clientes representam "
            f"{metricas['concentracao_top5']:.1f}% do total a receber."
        )
    vencido_60 = float(financeiro.loc[
        financeiro["Dias_atraso"] > 60, "Valor"
    ].sum())
    if vencido_60 > 0:
        alertas.append(
            f"Existem {fmt(vencido_60)} vencidos há mais de 60 dias."
        )
    if metricas["vence_7"] > 0:
        alertas.append(
            f"Há {fmt(metricas['vence_7'])} previstos para entrar nos próximos 7 dias."
        )
    if metricas["saldo_30_dias"] < 0:
        alertas.append(
            f"Déficit projetado de {fmt(abs(metricas['saldo_30_dias']))} "
            "para os próximos 30 dias."
        )
    if not alertas:
        st.success("Nenhum alerta financeiro relevante pelos critérios atuais.")
    else:
        for alerta in alertas:
            st.warning(alerta)

    col_aging, col_fluxo = st.columns(2)
    ordem_faixas = [
        "Vencido acima de 60 dias",
        "Vencido de 31 a 60 dias",
        "Vencido de 16 a 30 dias",
        "Vencido de 8 a 15 dias",
        "Vencido até 7 dias",
        "A vencer em até 7 dias",
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
        st.markdown("#### Entradas previstas por mês")
        futuro = financeiro[~financeiro["Vencida"]].copy()
        if futuro.empty:
            st.info("Não há recebimentos futuros na carteira consultada.")
        else:
            futuro["Mês"] = futuro["Vencimento"].dt.strftime("%m/%Y")
            fluxo = futuro.groupby("Mês", sort=False)["Valor"].sum()
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
        "Titulos": "Títulos",
        "Maior_atraso": "Maior atraso (dias)"
    })
    st.dataframe(ranking, use_container_width=True, hide_index=True)

    st.markdown("#### Contas a pagar por fornecedor")
    if contas_pagar is None or contas_pagar.empty:
        st.info(
            "Contas a pagar não estão disponíveis. No modo API, atualize os dados; "
            "no modo Excel, seria necessário um quarto arquivo de contas a pagar."
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
            "Titulos": "Títulos",
            "Proximo_vencimento": "Próximo vencimento"
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
            "Descricao": "Descrição",
            "Situacao": "Situação",
            "Dias_para_vencer": "Dias para vencer"
        })
        st.dataframe(agenda, use_container_width=True, hide_index=True)

    st.markdown("#### Análise e estratégia financeira")
    if not resultado_disponivel:
        st.info(
            "O lucro ou prejuízo mensal exige os movimentos liquidados de recebimentos "
            "e pagamentos. Esse cálculo fica disponível automaticamente pelo modo API."
        )
    elif metricas["resultado_mes"] > 0:
        st.success(
            f"Há lucro financeiro de {fmt(metricas['resultado_mes'])} em "
            f"{mes_resultado}."
        )
    elif metricas["resultado_mes"] < 0:
        st.error(
            f"Há prejuízo financeiro de {fmt(abs(metricas['resultado_mes']))} em "
            f"{mes_resultado}."
        )
    else:
        st.warning(f"O resultado financeiro de {mes_resultado} está equilibrado.")
    if resultado_disponivel:
        for dica in estrategia_financeira(metricas):
            st.write(f"- {dica}")

def renderizar_financeiro_real(dados):
    configuracao = dados.get("configuracao", {})
    real = calcular_financeiro_real(dados, configuracao)
    st.markdown("---")
    st.subheader("Resultado econômico e cenários")
    if not real:
        st.info(
            "Os dados desta sessão foram carregados por uma versão anterior. "
            "Clique em 'Atualizar dados do GestãoClick' para calcular custos, "
            "margens e resultado econômico."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Receita do mês", fmt(real["receita_mes"]))
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
        "Lucro" if real["lucro_operacional"] >= 0 else "Prejuízo"
    )
    cols2[3].metric("Margem operacional", f"{real['margem_operacional']:.1f}%")
    if not real["custos_disponiveis"]:
        st.warning(
            "Os custos das vendas não estão preenchidos na API. O lucro bruto e "
            "operacional podem estar superestimados."
        )
    st.markdown("#### Cenários de caixa")
    cenarios = pd.DataFrame([
        {"Cenário": nome, "Caixa projetado": valor}
        for nome, valor in real["cenarios"].items()
    ])
    cenarios["Caixa projetado"] = cenarios["Caixa projetado"].map(fmt)
    st.dataframe(cenarios, use_container_width=True, hide_index=True)
    st.caption(
        "Os cenários consideram 70%, 90% ou 100% da carteira a receber, "
        "menos todas as contas a pagar registradas."
    )

def renderizar_gestao_comercial(dados):
    indicadores, vendedores = calcular_gestao_comercial(
        dados, dados.get("configuracao", {})
    )
    st.subheader("Gestão Comercial")
    if not indicadores:
        st.info(
            "Os dados desta sessão foram carregados por uma versão anterior. "
            "Clique em 'Atualizar dados do GestãoClick' para calcular metas, "
            "margens e desempenho por vendedor."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Meta geral", fmt(indicadores["meta_geral"]))
    cols[1].metric("Realizado no mês", fmt(indicadores["realizado"]))
    cols[2].metric("Projeção de fechamento", fmt(indicadores["projecao"]))
    cols[3].metric("Distância da meta", fmt(indicadores["distancia_meta"]))
    if indicadores.get("ciclo_meta_inicio") is not None:
        st.caption(
            f"Meta calculada no ciclo comercial de "
            f"{indicadores['ciclo_meta_inicio']:%d/%m/%Y} a "
            f"{indicadores['ciclo_meta_fim']:%d/%m/%Y}."
        )
    cols2 = st.columns(3)
    cols2[0].metric(
        "Conversão de orçamentos",
        f"{indicadores['conversao_orcamentos']:.1f}%"
    )
    cols2[1].metric("Orçamentos analisados", indicadores["orcamentos_total"])
    cols2[2].metric(
        "Idade média dos abertos",
        f"{indicadores['idade_media_abertos']:.0f} dias"
    )
    st.caption(
        "A conversão usa as situações dos orçamentos. Sem vínculo direto entre "
        "orçamento e venda, o tempo exato até fechamento não pode ser afirmado."
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
            "Ticket_medio": "Ticket médio",
            "Margem_pct": "Margem %",
            "Atingimento_pct": "Atingimento %",
            "Distancia_meta": "Distância da meta",
        })
        st.dataframe(exibir, use_container_width=True, hide_index=True)
    st.markdown("#### Motivos de perda")
    if indicadores["motivos_perda"].empty:
        st.info(
            "Nenhum motivo de perda foi encontrado nas observações dos orçamentos."
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
    cols[0].metric("Vendas excluídas", qualidade.get("vendas_canceladas", 0))
    cols[1].metric("Sem cliente ID", qualidade.get("vendas_sem_cliente_id", 0))
    cols[2].metric("Nomes duplicados", qualidade.get("clientes_nomes_duplicados", 0))
    cols[3].metric("Sem custo", qualidade.get("vendas_sem_custo", 0))
    cols[4].metric("Sem vendedor", qualidade.get("vendas_sem_vendedor", 0))
    problemas = sum(int(v) for v in qualidade.values())
    if problemas:
        st.warning(
            "Há registros que podem reduzir a precisão dos indicadores. "
            "Vendas canceladas e devolvidas foram excluídas automaticamente."
        )
    else:
        st.success("Nenhum problema relevante foi detectado na base consultada.")
    st.markdown(
        """
        **Regras aplicadas**

        - clientes são consolidados por `cliente_id`; o nome é apenas para exibição;
        - vendas canceladas, devolvidas, estornadas, reprovadas ou perdidas são excluídas;
        - registros duplicados da API são removidos pelo ID;
        - custos, vendedor e identificação ausentes são sinalizados;
        - contas futuras não entram na inadimplência antes do vencimento.
        """
    )

    st.markdown("#### Clientes ativos com pendências")
    if clientes.empty or "inadimplencia" not in clientes.columns:
        st.info(
            "Atualize os dados do GestãoClick para analisar clientes com pendências."
        )
        return
    ativos_inadimplentes = clientes[
        clientes["inadimplencia"] > 0
    ].sort_values("inadimplencia", ascending=False).copy()
    if ativos_inadimplentes.empty:
        st.success("Nenhum cliente da carteira comercial possui pendência identificada.")
    else:
        tabela_ativos = ativos_inadimplentes[[
            "Cliente", "ultima_compra", "inadimplencia",
            "media_dias_atraso", "temperatura"
        ]].head(20).copy()
        tabela_ativos["ultima_compra"] = tabela_ativos["ultima_compra"].dt.strftime("%d/%m/%Y")
        tabela_ativos["inadimplencia"] = tabela_ativos["inadimplencia"].map(fmt)
        tabela_ativos = tabela_ativos.rename(columns={
            "ultima_compra": "Última compra",
            "inadimplencia": "Valor vencido",
            "media_dias_atraso": "Média de atraso",
            "temperatura": "Situação comercial",
        })
        st.dataframe(tabela_ativos, use_container_width=True, hide_index=True)

def renderizar_card_metric(coluna, titulo, valor, detalhe="", ajuda=None):
    coluna.metric(titulo, valor, detalhe, help=ajuda)

def renderizar_cards_orcamentos_simples(orcamentos, co_num, co_cli, co_valor, incluir_acao=False):
    cards = list(orcamentos.head(24).iterrows())
    for i in range(0, len(cards), 3):
        cols = st.columns(3)
        for j, (_, r) in enumerate(cards[i:i+3]):
            with cols[j]:
                numero = html_seguro(r.get(co_num, ""))
                cliente = html_seguro(r.get(co_cli, "Cliente sem nome"))
                valor = fmt_html(r.get(co_valor, 0)) if co_valor else "Sem valor"
                dias = int(r.get("dias_no_sistema", 0) or 0)
                acao = html_seguro(r.get("acao_recomendada_orcamento", "")) if incluir_acao else ""
                linha_acao = f"<br>Ação: <b>{acao}</b>" if acao else ""
                st.markdown(f"""
<div style="background:white;padding:14px;border-radius:10px;border:1px solid #ddd;margin-bottom:10px;">
<b>Orçamento #{numero}</b><br>
Cliente: <b>{cliente}</b><br>
Valor: <b>{valor}</b><br>
Tempo no sistema: <b>{dias} dia(s)</b>{linha_acao}
</div>
""", unsafe_allow_html=True)

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
    comissao_paga = float(itens.loc[itens["Pago no prazo"], "Comissão"].sum()) if not itens.empty else 0.0
    comissao_total = float(itens["Comissão"].sum()) if not itens.empty else 0.0
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
            Comissao_potencial=("Comissão", "sum"),
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
            tabela["Comissão"] = tabela["Comissão"].map(fmt)
            tabela["Percentual"] = tabela["Percentual"].map(lambda x: f"{x:.2f}%".replace(".", ","))
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
    st.subheader("RETENÇÃO E CRESCIMENTO")
    contagem = indicadores["contagem_status"]
    historico = indicadores["historico"]
    receita_prevista = float(clientes["faturamento"].sum())
    venda_possivel = float(prioridade["ticket_medio"].sum())

    linha1 = st.columns(4)
    renderizar_card_metric(
        linha1[0], "💰 Receita Prevista", fmt(receita_prevista),
        ajuda="Faturamento total do período carregado no sistema."
    )
    renderizar_card_metric(
        linha1[1], "🎯 Venda Possível Hoje", fmt(venda_possivel),
        ajuda="Soma do ticket médio dos clientes na prioridade comercial de hoje."
    )
    renderizar_card_metric(
        linha1[2], "⚠️ Carteira em Risco",
        fmt(indicadores["carteira_risco_mensal"]),
        f"{indicadores['qtd_risco']} clientes",
        "Receita que pode ser perdida caso clientes em risco não sejam trabalhados."
    )
    renderizar_card_metric(
        linha1[3], "🔄 Potencial Recuperável",
        fmt(indicadores["potencial_recuperavel_mensal"]),
        f"{indicadores['qtd_recuperaveis']} clientes",
        "Receita que pode voltar para a empresa através da recuperação da carteira."
    )

    linha2 = st.columns(4)
    renderizar_card_metric(
        linha2[0], "🚨 Churn Financeiro",
        fmt(indicadores["churn_financeiro_mensal"]),
        f"Anual: {fmt(indicadores['churn_financeiro_anual'])}",
        "Receita potencial perdida por clientes que deixaram de comprar."
    )
    renderizar_card_metric(
        linha2[1], "👥 Clientes em Risco",
        int(contagem.get("EM RISCO", 0)),
        ajuda="Clientes que passaram do ciclo médio de compra, mas ainda não chegaram a 2x o ciclo."
    )
    renderizar_card_metric(
        linha2[2], "❌ Clientes Perdidos",
        int(contagem.get("CHURN", 0)),
        ajuda="Clientes há mais de duas vezes o ciclo médio de recompra sem comprar."
    )
    cac_valor = (
        fmt(indicadores["cac_atual"])
        if indicadores["novos_clientes_atual"] else "Sem novos clientes"
    )
    renderizar_card_metric(
        linha2[3], "💵 CAC Atual", cac_valor,
        texto_variacao(indicadores["cac_variacao"]),
        "Quanto custa adquirir um novo cliente."
    )

    st.caption(
        f"CAC anterior: {fmt(indicadores['cac_anterior'])} | "
        f"Novos clientes no mês atual: {indicadores['novos_clientes_atual']} | "
        f"Novos clientes no mês anterior: {indicadores['novos_clientes_anterior']}"
    )

    st.markdown("#### Clientes Perdidos")
    col_status = st.columns(3)
    col_status[0].metric("Saudáveis", int(contagem.get("SAUDÁVEL", 0)))
    col_status[1].metric("Em risco", int(contagem.get("EM RISCO", 0)))
    col_status[2].metric("Churn", int(contagem.get("CHURN", 0)))

    st.markdown("#### Taxa de Recuperação")
    st.metric(
        "Clientes recuperados / clientes marcados como em risco",
        f"{indicadores['taxa_recuperacao']:.1f}%",
        help="Clientes que estavam em risco no mês anterior e voltaram a comprar no mês atual."
    )

    if historico.empty:
        st.info("Ainda não há histórico mensal suficiente para os gráficos executivos.")
        return

    grafico = historico.set_index("Mês")
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.markdown("#### Evolução do Churn Financeiro")
        st.line_chart(grafico[["Churn financeiro"]])
    with col_g2:
        st.markdown("#### Evolução da Carteira em Risco")
        st.line_chart(grafico[["Carteira em risco"]])

    col_g3, col_g4 = st.columns(2)
    with col_g3:
        st.markdown("#### Evolução do CAC")
        st.line_chart(grafico[["CAC"]])
    with col_g4:
        st.markdown("#### Clientes por Status")
        status_df = pd.DataFrame({
            "Status": ["Saudáveis", "Em risco", "Churn"],
            "Clientes": [
                int(contagem.get("SAUDÁVEL", 0)),
                int(contagem.get("EM RISCO", 0)),
                int(contagem.get("CHURN", 0)),
            ]
        }).set_index("Status")
        st.bar_chart(status_df)

    st.markdown("#### Histórico da Taxa de Recuperação")
    st.line_chart(grafico[["Taxa de recuperação"]])

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

    if pagina == "Resumo Diário":
        renderizar_resumo_diario(dados)

    if pagina == "Geração de Orçamentos":
        renderizar_geracao_orcamentos()

    if pagina == "Comissão":
        renderizar_comissao(dados)

    if pagina == "Ações de Hoje":
        st.subheader("Ações de Hoje")
        st.caption("Fila operacional do dia com clientes, orçamentos e retornos que precisam de ação.")
        orc_2_dias = orc_aberto[orc_aberto["dias_no_sistema"] == 2].copy() if not orc_aberto.empty else pd.DataFrame()
        orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 3].copy() if not orc_aberto.empty else pd.DataFrame()
        atraso_recompra = clientes[
            clientes["temperatura"].isin(["🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"])
            & (~clientes["ja_ligou_hoje"])
        ].copy()
        retornos_hoje = clientes[
            clientes.get("retornos_hoje", pd.Series(0, index=clientes.index)).gt(0)
            & (~clientes["ja_ligou_hoje"])
        ].copy()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Clientes para ligar", len(prioridade))
        c2.metric("Orçamentos com 2 dias", len(orc_2_dias))
        c3.metric("Orçamentos urgentes 3+ dias", len(orc_urgentes))
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
                st.info("Nenhum cliente prioritário para ligar agora.")

        with st.expander("Orçamentos com 2 dias"):
            if orc_2_dias.empty:
                st.info("Nenhum orçamento com 2 dias.")
            else:
                renderizar_cards_orcamentos_simples(orc_2_dias, co_num, co_cli, co_valor)

        with st.expander("Orçamentos urgentes com 3+ dias"):
            if orc_urgentes.empty:
                st.info("Nenhum orçamento urgente.")
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

    if pagina == "👑 CEO":
        st.subheader("👑 Painel CEO")

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
                **Fórmula**

                `Taxa de churn = clientes em churn ÷ clientes com ciclo conhecido × 100`

                Um cliente entra em **churn estimado** quando:

                - possui pelo menos duas compras, permitindo calcular seu intervalo médio;
                - está sem comprar há mais de duas vezes o seu intervalo médio de recompra.

                **Exemplo:** se um cliente costuma comprar a cada 30 dias e está há mais
                de 60 dias sem comprar, ele é considerado em churn. Clientes com apenas
                uma compra não entram na base, pois ainda não possuem ciclo conhecido.
                """
            )
            st.write(
                f"Cálculo atual: {qtd_churn} ÷ {base_churn} × 100 = {taxa_churn:.1f}%"
                if base_churn
                else "Ainda não há clientes com histórico suficiente para calcular o churn."
            )

        st.markdown(f"**Receita prevista:** **{fmt(clientes['faturamento'].sum())}**")
        st.caption("Soma do faturamento total existente no relatório de vendas importado. O período depende do arquivo enviado.")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.caption("Média mensal de compras dos últimos 3 meses.")
        st.markdown(f"**Venda possível hoje:** **{fmt(prioridade['ticket_medio'].sum())}**")
        st.caption("Soma do ticket médio dos clientes classificados como QUENTE na aba Prioridade.")
        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.caption("Soma do potencial mensal dos clientes classificados como ATRASADO NA RECOMPRA ou CLIENTE INATIVO.")
        st.markdown(f"**Inadimplência real:** **{fmt(clientes['inadimplencia'].sum())}**")
        renderizar_retencao_crescimento_ceo(
            indicadores_retencao, clientes, prioridade
        )

    if pagina == "💰 Financeiro CEO":
        renderizar_financeiro_ceo(
            financeiro, contas_pagar, recebido_mes, pago_mes,
            mes_resultado, resultado_disponivel, clientes, clientes_churn
        )
        renderizar_financeiro_real(dados)

    if pagina == "🎯 Gestão Comercial":
        renderizar_gestao_comercial(dados)

    if pagina == "📉 Churn":
        st.subheader("📉 Clientes em churn")
        st.caption(
            "Clientes com ciclo de recompra conhecido que estão há mais de duas vezes "
            "o intervalo médio sem comprar."
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
            "Recuperações históricas",
            churn_avancado["recuperados_historicos"],
            f"{churn_avancado['taxa_recuperacao_historica']:.1f}% da base recorrente"
        )
        avancado[3].metric(
            "Clientes sazonais",
            churn_avancado["sazonais"]
        )
        st.caption(
            "O churn ponderado considera o faturamento dos clientes perdidos. "
            "Clientes sazonais são sinalizados separadamente por apresentarem ciclos irregulares."
        )
        if not churn_avancado["tendencia_mensal"].empty:
            st.markdown("#### Evolução mensal do churn")
            tendencia = churn_avancado["tendencia_mensal"].set_index("Mês")
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
                "intervalo": "Ciclo médio",
                "potencial_mensal": "Potencial mensal",
                "temperatura": "Situação",
            })
            st.dataframe(migrando, use_container_width=True, hide_index=True)

        if clientes_churn.empty:
            st.success("Nenhum cliente está classificado em churn.")
        else:
            for _, r in clientes_churn.iterrows():
                cliente_html = html_seguro(r["Cliente"])
                ultima_compra = r["ultima_compra"].strftime("%d/%m/%Y")
                st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border-left:6px solid #d62728;border-top:1px solid #ddd;border-right:1px solid #ddd;border-bottom:1px solid #ddd;">
<b>{cliente_html}</b><br>
Última compra: <b>{ultima_compra}</b><br>
Está há <b>{int(r['dias_sem_comprar'])} dias</b> sem comprar<br>
Ciclo médio: <b>{int(r['intervalo'])} dias</b><br>
Limite para churn: <b>{int(r['limite_churn_dias'])} dias</b><br>
Passou do limite há: <b>{int(r['dias_alem_limite'])} dias</b><br><br>
Faturamento histórico: <b>{fmt_html(r['faturamento'])}</b><br>
Ticket médio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal em risco: <b>{fmt_html(r['potencial_mensal'])}</b><br>
Inadimplência: <b>{fmt_html(r['inadimplencia'])}</b>
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

    if pagina == "🔥 Prioridade":
        st.subheader("🔥 Prioridade")
        if prioridade.empty:
            st.info("Nenhum cliente no timing ideal hoje.")
        cards = list(prioridade.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "prioridade", f"{indice}_{i}_{j}")

    if pagina == "📋 Resumo":
        st.subheader("📋 Resumo Comercial")
        st.markdown(f"**Clientes para ação:** **{len(resumo)}**")
        st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(resumo['potencial_recuperavel'].sum())}**")
        cards = list(resumo.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "resumo", f"{indice}_{i}_{j}")

    if pagina == "📄 Orçamentos":
        st.subheader("📄 Orçamentos em aberto para retorno")
        if orc_aberto.empty:
            st.info("Nenhum orçamento em aberto nos últimos 30 dias.")
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
<b>Orçamento Nº {num_orc_html}</b><br>
Cliente: <b>{cliente_orc_html}</b><br>
Tempo no sistema: <b>{int(r['dias_no_sistema'])} dia(s)</b><br>
Status: <b>{status_orc_html}</b><br>
Valor: <b>{valor_txt}</b>
</div>
""", unsafe_allow_html=True)

                        if dados.get("origem") == "api" and str(r.get("_observacoes_interna", "")).strip():
                            with st.expander("Ver histórico do GestãoClick"):
                                st.text(str(r.get("_observacoes_interna", "")))

                        obs = st.text_area(
                            "Nova observação" if dados.get("origem") == "api" else "Observação",
                            value=st.session_state.observacoes_orc.get(num_orc, ""),
                            key=chave_obs
                        )

                        if st.button(
                            f"💾 Salvar observação {num_orc}",
                            key=f"salvar_obs_{orcamento_uid}"
                        ):
                            try:
                                if not obs.strip():
                                    raise RuntimeError("Digite uma observação antes de salvar.")
                                if dados.get("origem") == "api":
                                    orcamento_id = str(r.get("_orcamento_id") or "").strip()
                                    if not orcamento_id:
                                        raise RuntimeError("ID interno do orçamento não encontrado.")
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
                                    st.success("Observação salva no Google Sheets.")
                            except Exception as e:
                                st.error(f"Não foi possível salvar a observação: {e}")

                        pendente = st.session_state.alteracao_gestaoclick_pendente
                        if (
                            dados.get("origem") == "api" and pendente and
                            pendente.get("numero") == num_orc and
                            pendente.get("orcamento_id") == orcamento_id
                        ):
                            st.warning(
                                "Confirme a alteração no GestãoClick.\n\n"
                                f"Orçamento: {num_orc}\n\n"
                                f"Cliente: {pendente['cliente']}\n\n"
                                f"Nova observação: {pendente['observacao']}"
                            )
                            confirmado = st.checkbox(
                                "Revisei os dados e autorizo a gravação no GestãoClick.",
                                key=f"confirmar_gc_{orcamento_uid}"
                            )
                            col_confirmar, col_cancelar = st.columns(2)
                            if col_confirmar.button(
                                "Confirmar gravação",
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
                                        "Alteração confirmada e gravada no GestãoClick."
                                    )
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Falha ao gravar no GestãoClick: {e}")
                            if col_cancelar.button(
                                "Cancelar",
                                key=f"cancelar_gc_{orcamento_uid}"
                            ):
                                st.session_state.alteracao_gestaoclick_pendente = None
                                st.rerun()

    if pagina == "🧠 Gestão":
        st.subheader("🧠 Gestão")
        st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
        st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
        st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.markdown(f"**Inadimplência total:** **{fmt(clientes['inadimplencia'].sum())}**")
        st.markdown(f"**Taxa de churn estimada:** **{taxa_churn:.1f}%**")
        st.caption(f"Clientes em churn: {qtd_churn} | Base analisada: {base_churn}")

    if pagina == "✅ Qualidade":
        renderizar_qualidade_dados(dados)

    if pagina == "📊 Base":
        st.subheader("📊 Base completa")
        acoes = ["Todas"] + sorted(clientes["acao_ia"].unique().tolist())
        temperaturas = ["Todas"] + sorted(clientes["temperatura"].unique().tolist())
        col1, col2 = st.columns(2)
        with col1:
            filtro_acao = st.selectbox("Filtrar por ação sugerida", acoes, key="filtro_base_acao")
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
                    estrela = "⭐ Cliente estratégico<br>" if r["cliente_estrategico"] else ""
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
Ticket médio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(r['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt_html(r['potencial_recuperavel'])}</b><br>
Compras: <b>{int(r['qtd_compras'])}</b><br>
Intervalo médio: <b>{int(r['intervalo'])} dias</b><br>
Última compra: <b>{r['ultima_compra'].strftime('%d/%m/%Y')}</b><br>
Dias sem comprar: <b>{int(r['dias_sem_comprar'])}</b><br>
Orçamentos em aberto: <b>{int(r['orcamentos_em_aberto'])}</b><br>
Inadimplência: <b>{fmt_html(r['inadimplencia'])}</b><br>
Score de risco: <b>{int(r['score_risco'])}/100 — {risco_html}</b><br>
Recomendação: <b>{acao_html}</b>
</div>
""", unsafe_allow_html=True)
                    cliente_uid = chave_widget(identificador_cliente(r, f"base_{i}_{j}"))
                    with st.expander(f"Ver itens comprados e orçados - {r['Cliente']} #{cliente_uid[-6:]}"):
                        renderizar_lista_itens(
                            "Itens comprados",
                            r.get("itens_comprados", [])
                        )
                        renderizar_lista_itens(
                            "Itens orçados",
                            r.get("itens_orcados", [])
                        )

    if pagina == "✉️ Resumo E-mail":
        st.subheader("✉️ Resumo para E-mail")
        st.caption(
            "Resumo diário e acionável para a equipe: indicadores, prioridades, "
            "orçamentos urgentes, churn e plano do dia."
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

    if pagina == "📧 Relatório Comercial":
        st.subheader("📧 Relatório Comercial")
        st.caption(
            "Relatório executivo completo com período analisado, indicadores, carteira, "
            "prioridades, churn, orçamentos, inadimplência, plano de ação e metodologia."
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
                    "📄 Baixar Relatório Executivo em PDF",
                    pdf,
                    nome_pdf,
                    "application/pdf",
                ),
                unsafe_allow_html=True
            )
        else:
            st.warning("PDF indisponível. Verifique se 'reportlab' está no requirements.txt.")

opcoes_menu_crm = [
    "Ações de Hoje",
    "👑 CEO",
    "💰 Financeiro CEO",
    "🎯 Gestão Comercial",
    "📉 Churn",
    "🔥 Prioridade",
    "📋 Resumo",
    "📄 Orçamentos",
    "🧠 Gestão",
    "✅ Qualidade",
    "📊 Base",
    "✉️ Resumo E-mail",
    "📧 Relatório Comercial",
]
if "pagina_atual_crm" not in st.session_state:
    st.session_state.pagina_atual_crm = "👑 CEO"
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
    st.session_state.pagina_atual_crm = "👑 CEO"

with st.sidebar.expander("📊 CRM Inteligente", expanded=True):
    pagina_radio_atual = st.session_state.get("menu_lateral_crm", "👑 CEO")
    if pagina_radio_atual not in opcoes_menu_crm:
        pagina_radio_atual = "👑 CEO"
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

with st.sidebar.expander("Resumo Diário", expanded=False):
    secoes_resumo = [
        "Início",
        "Fila de prioridades",
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
        st.session_state.pagina_atual_crm = "Resumo Diário"
        st.session_state.menu_lateral_resumo_anterior = secao_resumo_lateral
        st.rerun()
    if st.button("Abrir Resumo Diário", use_container_width=True):
        st.session_state.resumo_diario_secao = secao_resumo_lateral
        st.session_state.abrir_resumo_diario = True
        st.session_state.abrir_geracao_orcamentos = False
        st.session_state.abrir_comissao = False
        st.session_state.pagina_atual_crm = "Resumo Diário"
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
                st.success("Supabase conectado e tabelas acessíveis.")
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
        except Exception as e:
            st.error(f"Erro ao processar: {e}")

if st.session_state.dados_processados is not None:
    if not st.session_state.persistencia_crm_tentada:
        carregar_persistencia_crm()
    renderizar()
else:
    st.info(
        "Conecte o GestãoClick ou use os arquivos Excel na barra lateral."
    )
