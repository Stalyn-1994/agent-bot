"""Bounded, private quote snapshots and native Telegram delivery."""
import asyncio
import contextvars
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from io import BytesIO
from threading import Lock, get_ident
from time import monotonic
from uuid import uuid4
from .quote_pdf import render_quote


def identity():
    bound={v.name:x for v,x in contextvars.copy_context().items() if isinstance(x,str)}
    if (bound.get("HERMES_SESSION_PLATFORM","").lower()!="telegram" or
            bound.get("HERMES_SESSION_CHAT_TYPE","").lower() not in {"private","dm"}):
        return None
    try:
        user=int(bound.get("HERMES_SESSION_USER_ID","0"))
        chat=int(bound.get("HERMES_SESSION_CHAT_ID","0"))
        thread=int(bound.get("HERMES_SESSION_THREAD_ID") or 0)
        if user==chat and user>0 and thread>=0:
            return chat,thread
    except ValueError:
        pass
    return None


class Delivery:
    def __init__(self):
        self.pending={}
        self.lock=Lock()
        self.application=None
        self.loop=None
        self.loop_thread=None

    def wire(self, application, adapter):
        self.application=application
        self.loop=asyncio.get_running_loop()
        self.loop_thread=get_ident()

    def remember(self, owner, quote):
        if not owner:
            return None
        with self.lock:
            now=monotonic()
            self.pending={k:v for k,v in self.pending.items() if v[0]>now}
            self.pending.pop(owner,None)
            if not quote or not quote["lineas_cotizadas"]:
                return None
            if len(self.pending)>=256:
                self.pending.pop(next(iter(self.pending)))
            code=uuid4().hex
            date=datetime.now(timezone(timedelta(hours=-5))).strftime("%d/%m/%Y")
            self.pending[owner]=(now+1800,code,date,deepcopy(quote))
            return code

    async def send(self, owner, code, date, quote):
        from telegram import InputFile
        name=f"Corelab-cotizacion-{code[:8]}.pdf"
        data=render_quote(quote,code[:8].upper(),date)
        args={"chat_id":owner[0],"document":InputFile(BytesIO(data),filename=name),
              "caption":"Tu cotización de Corelab en PDF.","read_timeout":10,"write_timeout":10}
        if owner[1]:
            args["message_thread_id"]=owner[1]
        await asyncio.wait_for(self.application.bot.send_document(**args),timeout=20)

    def decision(self, args):
        owner=identity()
        if not owner:
            return {"success":False,"error":"El PDF solo se puede enviar en el chat privado que hizo la cotización."}
        if (not isinstance(args,dict) or set(args)!={"accion","codigo"} or
                args["accion"] not in ("enviar","descartar") or not isinstance(args["codigo"],str)):
            return {"success":False,"error":"Decisión de PDF no válida."}
        with self.lock:
            saved=self.pending.get(owner)
            if not saved or saved[0]<=monotonic() or saved[1]!=args["codigo"]:
                return {"success":False,"error":"Esta cotización ya no está disponible para exportar. Puedes pedir una nueva."}
            if args["accion"]=="descartar":
                del self.pending[owner]
                return {"success":True,"estado":"pdf_descartado"}
            if (not self.loop or not self.loop.is_running() or not self.application or
                    not self.application.running or get_ident()==self.loop_thread):
                return {"success":False,"error":"No puedo enviar archivos por Telegram en este momento."}
            # Consume before delivery: a network timeout must not cause an automatic duplicate.
            del self.pending[owner]
        try:
            future=asyncio.run_coroutine_threadsafe(self.send(owner,*saved[1:]),self.loop)
            future.result(timeout=23)
            return {"success":True,"estado":"pdf_enviado"}
        except Exception:
            return {"success":False,"error":"No pude verificar la entrega. Si no ves el PDF, puedes pedir una nueva cotización."}
