"""Sending the sign-in code e-mail."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import CloudConfig

log = logging.getLogger("archicad_mcp_cloud.mail")


def _message(code: str) -> tuple[str, str, str]:
    subject = f"{code} is your Archicad MCP sign-in code"
    text = (
        f"Your Archicad MCP sign-in code is {code}\n\n"
        "Type it in the Archicad MCP panel. It expires in 10 minutes.\n"
        "If you did not try to sign in, you can ignore this e-mail.\n"
    )
    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;color:#1b1b1f">'
        "<p>Your Archicad MCP sign-in code is</p>"
        f'<p style="font-size:30px;font-weight:700;letter-spacing:6px;color:#15803d">{code}</p>'
        "<p>Type it in the Archicad MCP panel. It expires in 10 minutes.</p>"
        '<p style="color:#6b7280;font-size:13px">If you did not try to sign in, you can ignore this e-mail.</p></div>'
    )
    return subject, text, html


async def send_login_code(config: CloudConfig, email: str, code: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> None:
    subject, text, html = _message(code)
    if config.email_provider == "resend":
        async with httpx.AsyncClient(transport=transport, timeout=20) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.resend_api_key}"},
                json={"from": config.email_from, "to": [email], "subject": subject, "text": text, "html": html},
            )
        response.raise_for_status()
    elif config.email_provider == "smtp":
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = config.email_from, email, subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        def send() -> None:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if config.smtp_user:
                    smtp.login(config.smtp_user, config.smtp_password)
                smtp.send_message(message)

        await asyncio.to_thread(send)
    else:
        log.warning("Sign-in code for %s: %s (EMAIL_PROVIDER=console, nothing was sent)", email, code)
