"""Envio de e-mails transacionais do CRM."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


def enviar_email_smtp(conta: dict, destinatario: str, assunto: str, mensagem: str) -> str:
    host = conta.get("smtp_host")
    port = int(conta.get("smtp_port", 587))
    usuario = conta.get("email") or conta.get("usuario")
    senha = conta.get("senha") or conta.get("password")
    if not all([host, port, usuario, senha, destinatario]):
        raise ValueError("Conta SMTP incompleta.")

    msg = EmailMessage()
    msg["From"] = usuario
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg.set_content(mensagem)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(usuario, senha)
        smtp.send_message(msg)
    return "smtp"
