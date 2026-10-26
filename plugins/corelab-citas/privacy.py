"""Account-bound native consent evidence. No tool argument can accept an authorization."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

NOTICE_VERSION = "piloto-2026-10-23.1"


class ConsentRequired(ValueError):
    pass


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


@dataclass(frozen=True)
class Notice:
    version: str
    digest: str
    text_digest: str
    text: str
    pdf: bytes

    @classmethod
    def read(cls):
        directory=Path(__file__).parent/"data"
        info=json.loads((directory/"aviso.json").read_text(encoding="utf-8"))
        if info.get("version")!=NOTICE_VERSION or info.get("status")!="demonstration_draft":
            raise RuntimeError("El aviso del piloto no corresponde a esta version")
        # Fixed filenames prevent a modified manifest from choosing arbitrary host files.
        pdf=(directory/"aviso-privacidad-piloto.pdf").read_bytes()
        raw=(directory/"aviso-privacidad-piloto.txt").read_bytes()
        digest,text_digest=hashlib.sha256(pdf).hexdigest(),hashlib.sha256(raw).hexdigest()
        if digest!=info.get("pdf_sha256") or text_digest!=info.get("text_sha256"):
            raise RuntimeError("El aviso de privacidad fue modificado; revisa el paquete")
        return cls(NOTICE_VERSION,digest,text_digest,raw.decode("utf-8"),pdf)


class Ledger:
    def __init__(self,store):
        self.store=store
        self.notice=Notice.read()

    def initialize(self,conn):
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS privacy_notices(
              version TEXT PRIMARY KEY,pdf_sha256 TEXT NOT NULL,text_sha256 TEXT NOT NULL,
              notice_text TEXT NOT NULL,pdf BLOB NOT NULL,registered_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS privacy_prompts(
              token TEXT PRIMARY KEY,bot INTEGER NOT NULL,user INTEGER NOT NULL,
              chat INTEGER NOT NULL,thread INTEGER,kind TEXT NOT NULL,version TEXT NOT NULL,
              pdf_sha256 TEXT NOT NULL,document_message INTEGER,anchor INTEGER,
              state TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS privacy_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
              bot INTEGER NOT NULL,user INTEGER NOT NULL,chat INTEGER NOT NULL,
              action TEXT NOT NULL CHECK(action IN ('accepted','declined','withdrawn')),
              version TEXT NOT NULL,pdf_sha256 TEXT NOT NULL,at_utc TEXT NOT NULL,
              prompt TEXT NOT NULL,document_message INTEGER,anchor INTEGER NOT NULL,
              callback_id TEXT NOT NULL UNIQUE,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL,
              evidence_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS privacy_account ON privacy_events(bot,user,seq);
            CREATE TRIGGER IF NOT EXISTS privacy_events_no_update BEFORE UPDATE ON privacy_events
              BEGIN SELECT RAISE(ABORT,'Consent evidence is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS privacy_events_no_delete BEFORE DELETE ON privacy_events
              BEGIN SELECT RAISE(ABORT,'Consent evidence is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS privacy_notice_no_update BEFORE UPDATE ON privacy_notices
              BEGIN SELECT RAISE(ABORT,'A published notice needs a new version'); END;
            CREATE TRIGGER IF NOT EXISTS privacy_notice_no_delete BEFORE DELETE ON privacy_notices
              BEGIN SELECT RAISE(ABORT,'Notice evidence is append-only'); END;
        ''')
        n=self.notice
        old=conn.execute("SELECT pdf_sha256,text_sha256,notice_text,pdf FROM privacy_notices WHERE version=?",(n.version,)).fetchone()
        if old:
            if tuple(old)!=(n.digest,n.text_digest,n.text,n.pdf):
                raise RuntimeError("El aviso archivado difiere: cambia su version antes de publicarlo")
        else:
            conn.execute("INSERT INTO privacy_notices VALUES(?,?,?,?,?,?)",
                (n.version,n.digest,n.text_digest,n.text,n.pdf,datetime.now(timezone.utc).isoformat()))

    def current(self,identity,bot,conn=None):
        if conn is None:
            with self.store.connect() as own:return self.current(identity,bot,own)
        row=conn.execute("SELECT * FROM privacy_events WHERE bot=? AND user=? ORDER BY seq DESC LIMIT 1",
                         (bot,identity["user_id"])).fetchone()
        if not row or row["action"]!="accepted":return None
        event=dict(row)
        if event["version"]!=self.notice.version or event["pdf_sha256"]!=self.notice.digest:return None
        if hashlib.sha256(event["evidence_json"].encode("utf-8")).hexdigest()!=event["event_hash"]:
            raise RuntimeError("La evidencia de autorizacion requiere revision")
        return event

    def require(self,identity,bot,conn=None):
        event=self.current(identity,bot,conn)
        if not event:
            raise ConsentRequired("Antes de registrar los datos de la cita necesitas autorizar su tratamiento mediante el boton del aviso")
        return event

    def prompt(self,identity,bot,kind="consent"):
        if kind not in {"consent","withdraw"}:raise ValueError("Solicitud de privacidad no valida")
        with self.store.transaction() as conn:
            if kind=="consent":
                row=conn.execute("SELECT * FROM privacy_prompts WHERE bot=? AND user=? AND chat=? "
                    "AND thread IS ? AND kind='consent' AND version=? AND pdf_sha256=? "
                    "AND state='ready' AND expires>? ORDER BY created DESC LIMIT 1",
                    (bot,identity["user_id"],identity["chat_id"],identity.get("thread_id"),
                     self.notice.version,self.notice.digest,time.time())).fetchone()
                if row:return dict(row)
            token=uuid.uuid4().hex
            conn.execute("INSERT INTO privacy_prompts(token,bot,user,chat,thread,kind,version,pdf_sha256,state,expires,created) "
                "VALUES(?,?,?,?,?,?,?,?,'preparing',?,?)",
                (token,bot,identity["user_id"],identity["chat_id"],identity.get("thread_id"),kind,
                 self.notice.version,self.notice.digest,time.time()+86400,time.time()))
            return dict(conn.execute("SELECT * FROM privacy_prompts WHERE token=?",(token,)).fetchone())

    def sent(self,token,*,document=None,anchor=None):
        with self.store.transaction() as conn:
            if document is not None:
                conn.execute("UPDATE privacy_prompts SET document_message=? WHERE token=? AND state='preparing'",(document,token))
            if anchor is not None:
                conn.execute("UPDATE privacy_prompts SET anchor=?,state='ready' WHERE token=? "
                    "AND state='preparing' AND (kind='withdraw' OR document_message IS NOT NULL)",(anchor,token))

    def failed(self,token):
        with self.store.transaction() as conn:
            conn.execute("UPDATE privacy_prompts SET state='failed' WHERE token=? AND state='preparing'",(token,))

    def decide(self,token,action,identity,bot,anchor,thread,callback_id):
        if action not in {"accepted","declined","withdrawn","keep"}:
            raise ValueError("Decision de privacidad no valida")
        if not isinstance(callback_id,str) or not callback_id or len(callback_id)>256:
            raise ValueError("Falta la referencia del boton de Telegram")
        with self.store.transaction() as conn:
            row=conn.execute("SELECT * FROM privacy_prompts WHERE token=?",(token,)).fetchone()
            if not row or (row["bot"],row["user"],row["chat"],row["anchor"],row["thread"])!=(
                    bot,identity["user_id"],identity["chat_id"],anchor,thread):
                raise PermissionError("Este boton de privacidad no pertenece a esta cuenta o conversacion")
            if (action in {"accepted","declined"} and row["kind"]!="consent") or (
                    action in {"withdrawn","keep"} and row["kind"]!="withdraw"):
                raise PermissionError("La accion no corresponde a este boton")
            if row["state"] in {"accepted","declined","withdrawn","kept"}:
                event=conn.execute("SELECT * FROM privacy_events WHERE prompt=? ORDER BY seq LIMIT 1",(token,)).fetchone()
                latest=conn.execute("SELECT id FROM privacy_events WHERE bot=? AND user=? ORDER BY seq DESC LIMIT 1",
                                    (bot,identity["user_id"])).fetchone()
                if row["state"]==action and event and latest and latest["id"]==event["id"]:
                    return dict(event)
                if action=="keep" and row["state"]=="kept":return None
                raise ValueError("Este boton ya fue utilizado. Consulta tu autorizacion actual")
            if row["state"]!="ready" or row["expires"]<time.time():
                raise ValueError("La decision vencio o no esta lista. Solicita nuevamente el aviso")
            if row["version"]!=self.notice.version or row["pdf_sha256"]!=self.notice.digest:
                raise ValueError("El aviso cambio. Revisa su version actual antes de decidir")
            if (action in {"accepted","declined"} and row["kind"]!="consent") or (
                    action in {"withdrawn","keep"} and row["kind"]!="withdraw"):
                raise PermissionError("La accion no corresponde a este boton")
            if action=="keep":
                conn.execute("UPDATE privacy_prompts SET state='kept' WHERE token=?",(token,))
                return None
            if action=="accepted" and not row["document_message"]:
                raise ValueError("El PDF debe entregarse antes de autorizar")
            latest=self.current(identity,bot,conn)
            if action=="accepted" and latest:
                conn.execute("UPDATE privacy_prompts SET state='kept' WHERE token=?",(token,))
                return latest
            if action=="withdrawn" and not latest:
                raise ValueError("No hay una autorizacion vigente para retirar")
            # Close other outstanding prompts so an older consent button cannot
            # silently re-authorize after a later decline or withdrawal.
            conn.execute("UPDATE privacy_prompts SET state='superseded' WHERE bot=? AND user=? "
                         "AND state IN ('ready','preparing') AND token!=?",(bot,identity["user_id"],token))
            previous=conn.execute("SELECT event_hash FROM privacy_events ORDER BY seq DESC LIMIT 1").fetchone()
            value={"id":uuid.uuid4().hex,"bot":bot,"user":identity["user_id"],"chat":identity["chat_id"],
                "action":action,"version":self.notice.version,"pdf_sha256":self.notice.digest,
                "at_utc":datetime.now(timezone.utc).isoformat(),"prompt":token,
                "document_message":row["document_message"],"anchor":anchor,"callback_id":callback_id,
                "previous_hash":previous[0] if previous else "","channel":"telegram","thread":thread}
            evidence=canonical(value)
            conn.execute("INSERT INTO privacy_events(id,bot,user,chat,action,version,pdf_sha256,at_utc,prompt,"
                "document_message,anchor,callback_id,previous_hash,event_hash,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*[value[k] for k in ["id","bot","user","chat","action","version","pdf_sha256","at_utc","prompt",
                    "document_message","anchor","callback_id","previous_hash"]],hashlib.sha256(evidence.encode("utf-8")).hexdigest(),evidence))
            conn.execute("UPDATE privacy_prompts SET state=? WHERE token=?",(action,token))
            return dict(conn.execute("SELECT * FROM privacy_events WHERE id=?",(value["id"],)).fetchone())

    def audit(self):
        with self.store.connect() as conn:
            events=[dict(r) for r in conn.execute("SELECT * FROM privacy_events ORDER BY seq")]
            notices=[dict(r) for r in conn.execute("SELECT version,pdf_sha256,text_sha256,notice_text,pdf FROM privacy_notices")]
            bookings=[dict(r) for r in conn.execute("SELECT id,bot,user,chat,consent_event_id,state,start FROM bookings ORDER BY created")]
        previous=""
        for e in events:
            value=json.loads(e["evidence_json"])
            if (value["previous_hash"]!=previous or e["previous_hash"]!=previous or
                    hashlib.sha256(e["evidence_json"].encode("utf-8")).hexdigest()!=e["event_hash"] or
                    any(value[k]!=e[k] for k in ["id","bot","user","chat","action","version","pdf_sha256","at_utc",
                        "prompt","document_message","anchor","callback_id"])):
                raise RuntimeError("La cadena de auditoria no coincide")
            previous=e["event_hash"]
        for n in notices:
            if (hashlib.sha256(n["pdf"]).hexdigest()!=n["pdf_sha256"] or
                    hashlib.sha256(n["notice_text"].encode("utf-8")).hexdigest()!=n["text_sha256"]):
                raise RuntimeError("El aviso archivado no coincide con sus huellas")
        by_id={e["id"]:e for e in events}
        for row in bookings:
            if row["consent_event_id"] and (row["consent_event_id"] not in by_id or
                    by_id[row["consent_event_id"]]["action"]!="accepted" or
                    any(by_id[row["consent_event_id"]][k]!=row[k] for k in ("bot","user","chat"))):
                raise RuntimeError("Una cita no coincide con su evidencia de autorizacion")
        return {"eventos":len(events),"avisos_archivados":len(notices),"cadena_verificada":True,
            "decisiones":[{k:e[k] for k in ["id","user","action","version","at_utc"]} for e in events],
            "citas":[{"codigo":r["id"],"estado":r["state"],"inicio":r["start"],
                      "autorizacion":r["consent_event_id"] or "sin evidencia anterior"} for r in bookings]}
