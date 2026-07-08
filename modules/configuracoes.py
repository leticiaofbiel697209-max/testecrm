"""Validação de configurações obrigatórias."""


def secrets_obrigatorios_ok(secrets: dict) -> tuple[bool, list[str]]:
    faltando = []
    if not secrets.get("gestaoclick"):
        faltando.append("gestaoclick")
    if not secrets.get("supabase"):
        faltando.append("supabase")
    return not faltando, faltando
