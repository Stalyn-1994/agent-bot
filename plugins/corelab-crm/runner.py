"""Weekly generation and a durable outbox with conservative ambiguous-delivery handling."""
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import time

from .analytics import build_report
from .common import directory, settings, write_json, atomic, RECIPIENT, TZ, previous_week
from .mail import Gmail, mime, ApiError
from .render import PDF, html_report


def export_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    pdf = PDF(report).build()
    atomic(output / "informe.pdf", pdf)
    atomic(output / "informe.html", html_report(report).encode("utf-8"))
    write_json(output / "indicadores.json", report)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["cliente", "mensajes", "dias_activo", "temas", "senales", "citas_confirmadas", "caso_abierto"])
    for client in report["clients"]:
        # Only fixed classifier labels, numbers and HMAC pseudonyms; never arbitrary user strings/formulas.
        writer.writerow([client["client"], client["messages"], client["days_active"], "; ".join(client["topics"]),
            "; ".join(client["signals"]), client["confirmed_bookings"], client["open_case"]])
    atomic(output / "clientes.csv", stream.getvalue().encode("utf-8-sig"))
    return pdf


class Outbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sends(
                  period TEXT NOT NULL,recipient TEXT NOT NULL,state TEXT NOT NULL,
                  message_id TEXT NOT NULL,gmail_id TEXT,pdf_sha256 TEXT,
                  updated REAL NOT NULL,error_type TEXT,
                  PRIMARY KEY(period,recipient));
                CREATE TABLE IF NOT EXISTS resolutions(
                  seq INTEGER PRIMARY KEY,period TEXT NOT NULL,recipient TEXT NOT NULL,
                  outcome TEXT NOT NULL,at REAL NOT NULL,actor TEXT NOT NULL);
            """)

    def connect(self):
        class Closing(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        return sqlite3.connect(self.path, timeout=5, factory=Closing)

    def reserve(self, period):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state,message_id FROM sends WHERE period=? AND recipient=?", (period, RECIPIENT)).fetchone()
            if row:
                if row[0] in {"sent", "manual_sent"}:
                    return None
                if row[0] in {"sending", "uncertain"}:
                    raise RuntimeError("Hay un envio sin resultado confirmado. Revisa Enviados de Gmail y revisar-envios.cmd antes de reintentar")
            mid = "<corelab-crm-" + hashlib.sha256((period+RECIPIENT).encode()).hexdigest()[:28] + "@corelab.invalid>"
            conn.execute("INSERT INTO sends(period,recipient,state,message_id,updated) VALUES(?,?,'preparing',?,?) "
                "ON CONFLICT(period,recipient) DO UPDATE SET state='preparing',updated=excluded.updated,error_type=NULL", (period, RECIPIENT, mid, time.time()))
            conn.commit()
            return mid
        finally:
            conn.close()

    def mark(self, period, state, *, gmail_id=None, digest=None, error=None):
        with self.connect() as conn:
            conn.execute("UPDATE sends SET state=?,gmail_id=COALESCE(?,gmail_id),pdf_sha256=COALESCE(?,pdf_sha256),updated=?,error_type=? "
                "WHERE period=? AND recipient=?", (state, gmail_id, digest, time.time(), error, period, RECIPIENT))

    def resolve(self, period, sent, actor):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM sends WHERE period=? AND recipient=?", (period, RECIPIENT)).fetchone()
            if not row or row[0] not in {"sending", "uncertain"}:
                raise ValueError("Solo se resuelven envios inciertos")
            outcome = "manual_sent" if sent else "failed"
            conn.execute("UPDATE sends SET state=?,updated=? WHERE period=? AND recipient=?", (outcome, time.time(), period, RECIPIENT))
            conn.execute("INSERT INTO resolutions(period,recipient,outcome,at,actor) VALUES(?,?,?,?,?)", (period, RECIPIENT, outcome, time.time(), actor))

    def rows(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM sends ORDER BY period DESC LIMIT 20")]


def run(home, *, send=False, start=None, end=None, gmail=None):
    home = Path(home)
    cfg = settings(home)
    if start is None:
        start, end = previous_week()
    if end > datetime.now(TZ):
        raise ValueError("El informe requiere una semana ya terminada; no se presentan dias futuros como datos completos")
    if send and not cfg.get("enabled"):
        raise RuntimeError("El envio CRM esta desactivado; ejecuta conectar.cmd y activar.cmd")
    period = start.date().isoformat()
    output = directory(home) / ("reports" if send else "previews") / period
    box = Outbox(directory(home) / "outbox.db") if send else None
    # Avoid concurrent generation, token refresh and sends across cron/manual runs.
    from contextlib import contextmanager
    @contextmanager
    def lock():
        path = directory(home) / "execution.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"1"); stream.flush()
            stream.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                unlock = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                unlock()
    with lock():
        mid = box.reserve(period) if box else None
        if box and mid is None:
            return {"stage": "already_sent", "period": period, "recipient": RECIPIENT}
        try:
            report = build_report(home, start, end)
            pdf = export_report(report, output)
            if not send:
                return {"stage": "generated_not_sent", "period": period, "pdf": str(output / "informe.pdf"), "clients": report["metrics"]["customers"]}
            client = gmail or Gmail(directory(home) / "gmail-token.bin")
            client.token()  # Authentication errors happen before the send claim.
            message = mime(report, pdf, message_id=mid, sender=cfg.get("sender"))
            box.mark(period, "sending", digest=hashlib.sha256(pdf).hexdigest())
            try:
                gmail_id = client.send(message)
            except ApiError as exc:
                # 4xx rejection except request timeout is definitive; 5xx/network are ambiguous.
                box.mark(period, "failed" if 400 <= exc.status < 500 and exc.status != 408 else "uncertain", error=type(exc).__name__+":"+str(exc.status))
                raise
            except Exception as exc:
                box.mark(period, "uncertain", error=type(exc).__name__)
                raise
            box.mark(period, "sent", gmail_id=gmail_id)
            status = {"stage": "sent", "period": period, "recipient": RECIPIENT, "gmail_message_id": gmail_id,
                      "at": datetime.now(TZ).isoformat(), "pdf": str(output / "informe.pdf")}
            write_json(home / "logs/corelab-crm-mail.json", status)
            return status
        except Exception as exc:
            if box:
                row = next((r for r in box.rows() if r["period"] == period), None)
                if row and row["state"] == "preparing":
                    box.mark(period, "failed", error=type(exc).__name__)
            raise
