import os
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

API_BASE = os.getenv("GESTAOCLICK_API_BASE", "https://api.gestaoclick.com").rstrip("/")
ACCESS_TOKEN = os.getenv("GESTAOCLICK_ACCESS_TOKEN", "")
SECRET_TOKEN = os.getenv("GESTAOCLICK_SECRET_TOKEN", "")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app = FastAPI(title="Novaprint CRM API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOWED_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _headers() -> dict[str, str]:
    if not ACCESS_TOKEN or not SECRET_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Credenciais do GestãoClick ainda não configuradas no servidor.",
        )
    return {
        "Content-Type": "application/json",
        "access-token": ACCESS_TOKEN,
        "secret-access-token": SECRET_TOKEN,
    }


async def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=_headers(), params=params or {})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao acessar GestãoClick: {exc}") from exc

    if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
        raise HTTPException(status_code=502, detail=payload.get("message") or "Resposta inválida do GestãoClick")
    return payload.get("data", payload) if isinstance(payload, dict) else payload


async def _list_all(path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    base = dict(params or {})
    page = 1
    records: list[dict[str, Any]] = []
    while True:
        data = await _request(path, {**base, "pagina": page})
        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict):
            batch = data.get("data") or data.get("registros") or data.get("items") or []
        else:
            batch = []
        records.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        page += 1
        if page > 100:
            break
    return records


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "novaprint-crm-api",
        "gestaoclick_configured": bool(ACCESS_TOKEN and SECRET_TOKEN),
    }


@app.get("/stores")
async def stores() -> list[dict[str, Any]]:
    return await _list_all("/lojas")


@app.get("/users")
async def users(store_id: str = Query(...)) -> list[dict[str, Any]]:
    return await _list_all("/usuarios", {"loja_id": store_id})


@app.get("/clients")
async def clients(store_id: str = Query(...), search: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"loja_id": store_id}
    if search.strip():
        params["busca"] = search.strip()
    return await _list_all("/clientes", params)


@app.get("/sales")
async def sales(
    store_id: str = Query(...),
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=90)),
    end_date: date = Query(default_factory=date.today),
) -> list[dict[str, Any]]:
    return await _list_all(
        "/vendas",
        {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        },
    )


@app.get("/budgets")
async def budgets(
    store_id: str = Query(...),
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=90)),
    end_date: date = Query(default_factory=date.today),
) -> list[dict[str, Any]]:
    return await _list_all(
        "/orcamentos",
        {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        },
    )


@app.get("/receivables")
async def receivables(store_id: str = Query(...)) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for status, label in (("ab", "EM ABERTO"), ("at", "ATRASADO")):
        for item in await _list_all("/recebimentos", {"loja_id": store_id, "liquidado": status}):
            records.append({**item, "_status_financeiro": label})
    return records


@app.get("/payables")
async def payables(store_id: str = Query(...)) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for status, label in (("ab", "EM ABERTO"), ("at", "ATRASADO")):
        for item in await _list_all("/pagamentos", {"loja_id": store_id, "liquidado": status}):
            records.append({**item, "_status_financeiro": label})
    return records


@app.get("/dashboard")
async def dashboard(
    store_id: str = Query(...),
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=90)),
    end_date: date = Query(default_factory=date.today),
) -> dict[str, Any]:
    sales_data = await sales(store_id, start_date, end_date)
    budgets_data = await budgets(store_id, start_date, end_date)
    receivables_data = await receivables(store_id)

    def money(item: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                try:
                    return float(str(value).replace(".", "").replace(",", "."))
                except ValueError:
                    continue
        return 0.0

    faturamento = sum(money(item, "valor_total", "total", "valor") for item in sales_data)
    inadimplencia = sum(
        money(item, "valor", "valor_total", "saldo")
        for item in receivables_data
        if item.get("_status_financeiro") == "ATRASADO"
    )
    clientes_ids = {
        str(item.get("cliente_id") or (item.get("cliente") or {}).get("id") or "")
        for item in sales_data
    }
    clientes_ids.discard("")
    conversao = (len(sales_data) / len(budgets_data) * 100) if budgets_data else 0.0
    ticket_medio = (faturamento / len(sales_data)) if sales_data else 0.0

    return {
        "faturamento": faturamento,
        "inadimplencia": inadimplencia,
        "clientes_ativos": len(clientes_ids),
        "ticket_medio": ticket_medio,
        "conversao": conversao,
        "vendas": len(sales_data),
        "orcamentos": len(budgets_data),
        "periodo": {"inicio": start_date.isoformat(), "fim": end_date.isoformat()},
    }
