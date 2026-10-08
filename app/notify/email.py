"""Email over SMTP: optional, stdlib only.

Settings live in the settings row (`smtp`: host, port, tls, username, from_address);
the password is an encrypted secret (`smtp.password`). With SMTP not configured, no
email rows are ever created and email features are hidden. Nothing here logs an
address, a password or a message body.
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from sqlalchemy import Connection, select

from app.core.config import Settings
from app.models import InstanceSettingsRow
from app.services import secrets

TIMEOUT_SECONDS = 20


class EmailError(Exception):
    """A send failed. `permanent`: retrying won't help (rejected address, auth)."""

    def __init__(self, reason: str, *, permanent: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    tls: str
    username: str
    from_address: str
    password: str | None

    def __repr__(self) -> str:  # never show the password
        return f"SmtpConfig(host={self.host!r}, port={self.port}, tls={self.tls!r})"


def stored(conn: Connection) -> dict[str, Any]:
    data = conn.execute(select(InstanceSettingsRow.smtp)).scalar_one_or_none()
    return dict(data or {})


def configured(conn: Connection) -> bool:
    return bool(stored(conn).get("configured"))


def load(conn: Connection, settings: Settings) -> SmtpConfig | None:
    data = stored(conn)
    if not data.get("configured"):
        return None
    password = secrets.get(conn, settings.app_secret_key.get_secret_value(), "smtp.password")
    return SmtpConfig(
        host=str(data["host"]),
        port=int(data["port"]),
        tls=str(data.get("tls") or "starttls"),
        username=str(data.get("username") or ""),
        from_address=str(data["from_address"]),
        password=password,
    )


def build(cfg: SmtpConfig, app_name: str, to: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((app_name, cfg.from_address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.from_address.rsplit("@", 1)[-1])
    msg.set_content(text)
    return msg


def send(cfg: SmtpConfig, msg: EmailMessage) -> None:
    """Deliver one message, or raise EmailError (no secrets in the reason)."""
    context = ssl.create_default_context()
    try:
        if cfg.tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_SECONDS)
        with server:
            if cfg.tls == "starttls":
                server.starttls(context=context)
            if cfg.username:
                server.login(cfg.username, cfg.password or "")
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "the SMTP server refused the username or password", permanent=True
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailError("the SMTP server refused the recipient", permanent=True) from exc
    except smtplib.SMTPResponseException as exc:
        raise EmailError(
            f"the SMTP server answered {exc.smtp_code}", permanent=exc.smtp_code >= 500
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise EmailError(f"couldn't reach the SMTP server ({type(exc).__name__})") from exc
