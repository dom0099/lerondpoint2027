"""Envoi d'e-mails via le relais SMTP.

Trois modes, dans cet ordre de priorité :
  1. `outbox` non nul  -> le message y est déposé sans être remis (tests).
  2. `SMTP_HOST` absent -> le message est journalisé (développement hors ligne).
  3. sinon              -> remise réelle au relais.

Rappel opérationnel : l'adresse de MAIL_FROM doit être un expéditeur vérifié côté
Brevo. Sinon Brevo accepte le message en SMTP puis le filtre silencieusement en aval
(incident du 2026-08-31, voir le DEPLOY_LOG.md de Pol.is).
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import settings

logger = logging.getLogger(__name__)

# Boîte d'envoi de test. Quand elle vaut une liste, rien n'est remis au relais.
outbox: list[EmailMessage] | None = None


def build_message(to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


async def send_email(to: str, subject: str, body: str) -> None:
    message = build_message(to, subject, body)

    if outbox is not None:
        outbox.append(message)
        return

    if not settings.smtp_host:
        logger.warning("SMTP non configuré — e-mail non remis à %s : %s", to, subject)
        return

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        # Brevo écoute en 587 avec STARTTLS : TLS implicite désactivé, TLS négocié.
        use_tls=settings.smtp_secure,
        start_tls=not settings.smtp_secure,
        username=settings.smtp_user,
        password=settings.smtp_password,
    )
    logger.info("E-mail remis au relais pour %s : %s", to, subject)
