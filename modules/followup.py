"""Fila real de follow-up automático."""

FOLLOWUP_STATUS = ["pendente", "enviado", "erro", "respondido", "cancelado"]

FOLLOWUP_CAMPOS = [
    "id",
    "cliente_id",
    "cliente",
    "vendedor",
    "canal",
    "telefone",
    "email",
    "mensagem",
    "status",
    "data_programada",
    "enviado_em",
    "erro",
    "origem",
    "criado_em",
]
