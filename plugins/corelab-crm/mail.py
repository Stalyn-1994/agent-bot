"""Send-only Gmail OAuth. No inbox access, redirects, token logging or blind POST retries."""
import base64
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .common import Credentials, RECIPIENT, TZ
from .render import period

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class ApiError(Exception):
    def __init__(self, status):
        self.status = int(status)
        super().__init__("Gmail/OAuth devolvio HTTP " + str(self.status))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(method, url, data=None, headers=None):
    if url not in {TOKEN_URL, SEND_URL}:
        raise ValueError("Destino de correo no permitido")
    try:
        with build_opener(NoRedirect).open(Request(url, data=data, headers=headers or {}, method=method), timeout=25) as response:
            return json.loads(response.read(2_000_000))
    except HTTPError as exc:
        raise ApiError(exc.code) from None
    except (URLError, TimeoutError, OSError):
        raise ApiError(0) from None


def valid_sender(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise ValueError("Indica un correo de remitente valido sin nombre ni saltos de linea")
    return value


def mime(report, pdf, *, message_id, sender):
    from datetime import datetime
    if not pdf.startswith(b"%PDF-") or len(pdf) > 10_000_000:
        raise ValueError("PDF adjunto invalido o demasiado grande")
    message = EmailMessage(policy=SMTP)
    message["To"] = RECIPIENT
    message["From"] = valid_sender(sender)
    # Gmail enforces whether this authenticated account can use the configured sender.
    message["Subject"] = "Corelab | Informe gerencial semanal | " + period(report)
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime.now(TZ))
    m, ops = report["metrics"], report["operations"]
    body = ["Informe gerencial semanal de Corelab", period(report), "",
        "Clientes identificados: " + str(m["customers"]), "Mensajes de clientes: " + str(m["messages"]),
        "Citas confirmadas: " + str(ops["bookings_confirmed"] if ops["bookings_confirmed"] is not None else "N/D"),
        "", "Prioridades para revision:"]
    for p in report["priorities"][:3]:
        body.append("- " + p["focus"] + ": " + p["evidence"])
    body.extend(["", "El PDF adjunto contiene graficos, interpretacion por reglas y seguimiento seudonimizado.",
                 "No se adjuntan conversaciones literales ni datos de pacientes. Interpretaciones sujetas a revision humana."])
    if report.get("demo"):
        body.insert(0, "DEMOSTRACION: DATOS FICTICIOS")
    message.set_content("\n".join(body))
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename="Corelab-informe-" + report["period_start"][:10] + ".pdf")
    return message.as_bytes()


class Gmail:
    def __init__(self, path, *, credentials=None, transport=http):
        self.credentials = credentials or Credentials(path)
        self.transport = transport

    def token(self, force=False):
        stored = self.credentials.read()
        if not force and stored.get("access_token") and stored.get("expires_at", 0) > time.time()+60:
            return stored["access_token"]
        response = self.transport("POST", TOKEN_URL, urlencode({"client_id": stored["client_id"],
            "client_secret": stored["client_secret"], "refresh_token": stored["refresh_token"],
            "grant_type": "refresh_token"}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        if not response.get("access_token"):
            raise RuntimeError("Google no devolvio acceso para enviar correo; reconecta Gmail")
        if response.get("scope") and not set(SCOPES) <= set(response["scope"].split()):
            raise PermissionError("Google no concedio gmail.send")
        stored.update(access_token=response["access_token"], expires_at=time.time()+int(response.get("expires_in",3600)))
        if response.get("refresh_token"):
            stored["refresh_token"] = response["refresh_token"]
        self.credentials.write(stored)
        return stored["access_token"]

    def send(self, message):
        token = self.token()
        # Exactly one send request. A timeout or ambiguous response is reconciled manually.
        response = self.transport("POST", SEND_URL, json.dumps({"raw": base64.urlsafe_b64encode(message).decode()}).encode(),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        if not response.get("id"):
            raise RuntimeError("Gmail no devolvio ID: entrega no confirmada, revisar Enviados")
        return response["id"]
