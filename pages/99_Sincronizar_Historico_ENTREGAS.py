import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import streamlit as st

API_BASE = "https://api.gestaoclick.com"
BATCH_SIZE = 300

st.set_page_config(page_title="Sincronizar histórico", layout="wide")
st.title("Sincronizar histórico no Supabase ENTREGAS")
st.caption("Importa clientes, vendas, produtos vendidos, orçamentos e itens dos últimos 6 meses.")


def cfg(section_names, key_names):
    for section_name in section_names:
        try:
            section = st.secrets.get(section_name, {})
        except Exception:
            section = {}
        for key_name in key_names:
            value = str(section.get(key_name, "") or "").strip()
            if value:
                return value
    return ""


def gc_credentials():
    return (
        cfg(["gestaoclick", "gestao_click"], ["access_token", "GESTAOCLICK_ACCESS_TOKEN"]),
        cfg(["gestaoclick", "gestao_click"], ["secret_token", "secret_access_token", "GESTAOCLICK_SECRET_TOKEN"]),
    )


def supabase_credentials():
    url = cfg(
        ["supabase_entregas", "entregas", "supabase_historico"],
        ["url", "supabase_url", "SUPABASE_URL"],
    ).rstrip("/")
    key = cfg(
        ["supabase_entregas", "entregas", "supabase_historico"],
        ["service_role_key", "service_key", "key", "SUPABASE_SERVICE_ROLE_KEY"],
    )
    return url, key


def request_json(url, headers=None, method="GET", body=None, timeout=60):
    data = None if body is None else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Erro HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha de conexão: {exc.reason}") from exc


def gc_list_all(path, params, access_token, secret_token):
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
        payload = request_json(url, headers=headers, timeout=45)
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("message") or f"Resposta inválida em {path}")
        chunk = payload.get("data") or []
        if not chunk:
            break
        records.extend(chunk)
        meta = payload.get("meta") or {}
        if not meta.get("proxima_pagina") and len(chunk) < 100:
            break
        page += 1
        if page > 300:
            raise RuntimeError(f"Consulta de {path} excedeu 300 páginas")
        time.sleep(0.36)
    return records


def digits(value):
    return re.sub(r"\D+", "", str(value or ""))


def first(obj, *keys, default=None):
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else None
        if value not in (None, ""):
            return value
    return default


def nested_id(value):
    if isinstance(value, dict):
        return str(first(value, "id", "cliente_id", "usuario_id", "vendedor_id", default="") or "")
    return str(value or "")


def nested_name(value):
    if isinstance(value, dict):
        return str(first(value, "nome", "nome_fantasia", "razao_social", "descricao", default="") or "")
    return str(value or "")


def number(value, default=0.0):
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value))
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return default


def iso_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).isoformat()
        except Exception:
            pass
    return text


def cliente_row(item):
    vendedor = first(item, "vendedor", "usuario", "responsavel", default={})
    telefone = first(item, "telefone", "celular", "fone", "telefone1", default="")
    endereco = first(item, "endereco", default={})
    return {
        "id": str(first(item, "id", "cliente_id", default="") or ""),
        "razao_social": first(item, "razao_social", "nome", default=""),
        "nome_fantasia": first(item, "nome_fantasia", "fantasia", default=""),
        "cnpj_cpf": first(item, "cnpj", "cpf", "cpf_cnpj", "documento", default=""),
        "telefone": telefone,
        "telefone_normalizado": digits(telefone),
        "email": first(item, "email", "email_principal", default=""),
        "cidade": first(item, "cidade", default=first(endereco, "cidade", default="") if isinstance(endereco, dict) else ""),
        "uf": first(item, "uf", "estado", default=first(endereco, "uf", "estado", default="") if isinstance(endereco, dict) else ""),
        "vendedor_id": str(first(item, "vendedor_id", "usuario_id", default=nested_id(vendedor)) or ""),
        "vendedor_nome": first(item, "vendedor_nome", "usuario_nome", default=nested_name(vendedor)),
        "ativo": bool(first(item, "ativo", default=True)),
        "origem": "gestaoclick",
    }


def venda_row(item):
    vendedor = first(item, "vendedor", "usuario", "responsavel", default={})
    cliente = first(item, "cliente", default={})
    return {
        "id": str(first(item, "id", "venda_id", default="") or ""),
        "numero": str(first(item, "codigo", "numero", "id", default="") or ""),
        "cliente_id": str(first(item, "cliente_id", default=nested_id(cliente)) or ""),
        "vendedor_id": str(first(item, "vendedor_id", "usuario_id", default=nested_id(vendedor)) or ""),
        "vendedor_nome": first(item, "vendedor_nome", "usuario_nome", default=nested_name(vendedor)),
        "data_venda": iso_datetime(first(item, "data", "data_venda", "data_emissao", "criado_em")),
        "valor_total": number(first(item, "valor_total", "total", "valor", default=0)),
        "status": nested_name(first(item, "situacao", "status", default="")),
    }


def orcamento_row(item):
    vendedor = first(item, "vendedor", "usuario", "responsavel", default={})
    cliente = first(item, "cliente", default={})
    return {
        "id": str(first(item, "id", "orcamento_id", default="") or ""),
        "numero": str(first(item, "codigo", "numero", "id", default="") or ""),
        "cliente_id": str(first(item, "cliente_id", default=nested_id(cliente)) or ""),
        "cliente_nome": first(item, "cliente_nome", default=nested_name(cliente)),
        "vendedor_id": str(first(item, "vendedor_id", "usuario_id", default=nested_id(vendedor)) or ""),
        "vendedor_nome": first(item, "vendedor_nome", "usuario_nome", default=nested_name(vendedor)),
        "data_orcamento": iso_datetime(first(item, "data", "data_orcamento", "data_emissao", "criado_em")),
        "validade": iso_datetime(first(item, "validade", "data_validade")),
        "valor_total": number(first(item, "valor_total", "total", "valor", default=0)),
        "situacao": nested_name(first(item, "situacao", "status", default="")),
        "telefone": first(item, "telefone", default=first(cliente, "telefone", "celular", default="") if isinstance(cliente, dict) else ""),
        "email": first(item, "email", default=first(cliente, "email", default="") if isinstance(cliente, dict) else ""),
    }


def item_rows(parent, parent_key, output_parent_key):
    result = []
    parent_id = str(first(parent, "id", parent_key, default="") or "")
    wrappers = []
    for field in ("produtos", "itens", "servicos"):
        wrappers.extend(parent.get(field) or [])
    for index, wrapper in enumerate(wrappers):
        detail = wrapper
        if isinstance(wrapper, dict):
            detail = wrapper.get("produto") or wrapper.get("servico") or wrapper.get("item") or wrapper
        if not isinstance(detail, dict):
            continue
        item_id = str(first(wrapper if isinstance(wrapper, dict) else {}, "id", default="") or "")
        product_id = str(first(detail, "id", "produto_id", "servico_id", default="") or "")
        quantity = number(first(detail, "quantidade", "qtd", default=1), 1)
        unit = number(first(detail, "valor_unitario", "valor_venda", "valor", "preco", default=0))
        total = number(first(detail, "valor_total", "total", default=quantity * unit))
        result.append({
            "id": item_id or f"{parent_id}:{product_id or index}",
            output_parent_key: parent_id,
            "produto_id": product_id,
            "codigo": first(detail, "codigo", "sku", default=""),
            "nome": first(detail, "nome", "descricao", default=""),
            "quantidade": quantity,
            "valor_unitario": unit,
            "valor_total": total,
        })
    return result


def supabase_rpc(function_name, payload, url, key):
    headers = {
        "Content-Type": "application/json",
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    return request_json(f"{url}/rest/v1/rpc/{function_name}", headers=headers, method="POST", body=payload, timeout=180)


def batches(values, size=BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def import_batches(field_name, rows, start_date, end_date, url, key, progress, base, span):
    if not rows:
        return 0
    chunks = list(batches(rows))
    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "p_clientes": [], "p_vendas": [], "p_venda_itens": [],
            "p_orcamentos": [], "p_orcamento_itens": [],
            "p_periodo_inicio": start_date.isoformat(), "p_periodo_fim": end_date.isoformat(),
        }
        payload[f"p_{field_name}"] = chunk
        supabase_rpc("importar_crm_historico", payload, url, key)
        progress.progress(min(1.0, base + span * idx / len(chunks)))
    return len(rows)


access_token, secret_token = gc_credentials()
supa_url, supa_key = supabase_credentials()

col1, col2 = st.columns(2)
col1.metric("GestãoClick", "Configurado" if access_token and secret_token else "Secrets não encontrados")
col2.metric("Supabase ENTREGAS", "Configurado" if supa_url and supa_key else "Secrets não encontrados")

months = st.number_input("Meses de histórico", min_value=1, max_value=12, value=6, step=1)
end_date = date.today()
start_year = end_date.year
start_month = end_date.month - int(months)
while start_month <= 0:
    start_month += 12
    start_year -= 1
start_day = min(end_date.day, 28)
start_date = date(start_year, start_month, start_day)
st.info(f"Período: {start_date.strftime('%d/%m/%Y')} até {end_date.strftime('%d/%m/%Y')}")

if st.button("Importar agora para o ENTREGAS", type="primary", use_container_width=True):
    if not access_token or not secret_token:
        st.error("Não encontrei as secrets do GestãoClick.")
        st.stop()
    if not supa_url or not supa_key:
        st.error("Não encontrei as secrets do Supabase ENTREGAS. Verifique o nome da seção e das chaves.")
        st.stop()

    progress = st.progress(0.0)
    status = st.empty()
    try:
        before = supabase_rpc("crm_historico_uso", {}, supa_url, supa_key)
        status.write("Consultando lojas do GestãoClick...")
        stores = gc_list_all("/lojas", {}, access_token, secret_token)
        if not stores:
            raise RuntimeError("Nenhuma loja foi retornada pelo GestãoClick.")

        all_clients, all_sales, all_sale_items, all_budgets, all_budget_items = [], [], [], [], []
        for store_index, store in enumerate(stores, start=1):
            store_id = first(store, "id", "loja_id")
            status.write(f"Lendo loja {store_index}/{len(stores)}: clientes...")
            clients = gc_list_all("/clientes", {"loja_id": store_id}, access_token, secret_token)
            all_clients.extend(cliente_row(x) for x in clients)
            progress.progress(0.05 + 0.10 * store_index / len(stores))

            status.write(f"Lendo loja {store_index}/{len(stores)}: vendas...")
            sales = gc_list_all("/vendas", {
                "loja_id": store_id,
                "data_inicio": start_date.isoformat(),
                "data_fim": end_date.isoformat(),
            }, access_token, secret_token)
            all_sales.extend(venda_row(x) for x in sales)
            for sale in sales:
                all_sale_items.extend(item_rows(sale, "venda_id", "venda_id"))
            progress.progress(0.15 + 0.15 * store_index / len(stores))

            status.write(f"Lendo loja {store_index}/{len(stores)}: orçamentos...")
            budgets = gc_list_all("/orcamentos", {
                "loja_id": store_id,
                "data_inicio": start_date.isoformat(),
                "data_fim": end_date.isoformat(),
            }, access_token, secret_token)
            all_budgets.extend(orcamento_row(x) for x in budgets)
            for budget in budgets:
                all_budget_items.extend(item_rows(budget, "orcamento_id", "orcamento_id"))
            progress.progress(0.30 + 0.15 * store_index / len(stores))

        def dedupe(rows):
            return list({str(row.get("id")): row for row in rows if str(row.get("id") or "")}.values())

        all_clients = dedupe(all_clients)
        all_sales = dedupe(all_sales)
        all_sale_items = dedupe(all_sale_items)
        all_budgets = dedupe(all_budgets)
        all_budget_items = dedupe(all_budget_items)

        status.write("Gravando clientes no Supabase...")
        import_batches("clientes", all_clients, start_date, end_date, supa_url, supa_key, progress, 0.45, 0.10)
        status.write("Gravando vendas...")
        import_batches("vendas", all_sales, start_date, end_date, supa_url, supa_key, progress, 0.55, 0.10)
        status.write("Gravando itens vendidos...")
        import_batches("venda_itens", all_sale_items, start_date, end_date, supa_url, supa_key, progress, 0.65, 0.12)
        status.write("Gravando orçamentos...")
        import_batches("orcamentos", all_budgets, start_date, end_date, supa_url, supa_key, progress, 0.77, 0.09)
        status.write("Gravando itens orçados...")
        import_batches("orcamento_itens", all_budget_items, start_date, end_date, supa_url, supa_key, progress, 0.86, 0.12)

        after = supabase_rpc("crm_historico_uso", {}, supa_url, supa_key)
        progress.progress(1.0)
        status.empty()

        before_bytes = int(before.get("database_bytes", 0) or 0)
        after_bytes = int(after.get("database_bytes", 0) or 0)
        used_bytes = max(0, after_bytes - before_bytes)

        st.success("Sincronização concluída.")
        metrics = st.columns(5)
        metrics[0].metric("Clientes", f"{len(all_clients):,}".replace(",", "."))
        metrics[1].metric("Vendas", f"{len(all_sales):,}".replace(",", "."))
        metrics[2].metric("Itens vendidos", f"{len(all_sale_items):,}".replace(",", "."))
        metrics[3].metric("Orçamentos", f"{len(all_budgets):,}".replace(",", "."))
        metrics[4].metric("Itens orçados", f"{len(all_budget_items):,}".replace(",", "."))

        st.subheader("Espaço utilizado")
        st.write(f"Banco antes: **{before.get('database_size', '-')}**")
        st.write(f"Banco depois: **{after.get('database_size', '-')}**")
        st.write(f"Crescimento desta importação: **{used_bytes / 1024 / 1024:.2f} MB**")
        st.json(after)
    except Exception as exc:
        progress.empty()
        status.empty()
        st.exception(exc)
