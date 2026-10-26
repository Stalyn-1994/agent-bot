"""Native Corelab appointment tools and Telegram confirmation callbacks."""
import asyncio
import contextvars
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
import io

from .common import config, read_json, write_json, fingerprint, MAX_ACTIVE_BOOKINGS
from .patient import masked_cedula
from .google_api import Google
from .service import Service, advisor, dt
from .store import Store
from .privacy import ConsentRequired

plugin_name = "corelab-citas"
plugin_version = "1.3.0"
logger = logging.getLogger("corelab_citas")


def bound_identity():
    import gateway.session_context  # The host owns these ContextVars; environment variables are not identity.
    bound={var.name:value for var,value in contextvars.copy_context().items()
           if var.name.startswith("HERMES_SESSION_") and isinstance(value,str)}
    if (bound.get("HERMES_SESSION_PLATFORM", "").lower() != "telegram" or
            bound.get("HERMES_SESSION_CHAT_TYPE", "").lower() not in {"private","dm"}):
        raise PermissionError("La agenda requiere un chat privado de Telegram identificado por Hermes")
    user,chat=int(bound.get("HERMES_SESSION_USER_ID","0")),int(bound.get("HERMES_SESSION_CHAT_ID","0"))
    if user <= 0 or chat != user:
        raise PermissionError("Falta la identidad privada vinculada por Hermes")
    thread=bound.get("HERMES_SESSION_THREAD_ID","")
    return {"user_id":user,"chat_id":chat,"thread_id":int(thread) if thread else None}


class Runtime:
    def __init__(self, home):
        self.home=home
        self.directory=home / "plugin-data/corelab-citas"
        self.store=Store(self.directory / "appointments.db")
        self.app=self.loop=None
        self.loop_thread=None
        self.wakeup=None
        self.privacy_locks={}

    def settings(self):
        settings=config(self.home)
        if self.app is None:
            raise RuntimeError("La conexion de Telegram aun no esta lista")
        advisor(self.home,self.app.bot.id,int(settings["advisor_user_id"]))
        return settings

    def service(self):
        settings=self.settings()
        return Service(settings,self.store,Google(self.directory,settings["calendar_id"]),self.app.bot.id)

    async def send(self, chat, text, *, thread=None, buttons=None):
        kwargs={"chat_id":chat,"text":text,"parse_mode":None,"disable_web_page_preview":True,
                "connect_timeout":5,"read_timeout":8,"write_timeout":8,"pool_timeout":3}
        if thread:
            kwargs["message_thread_id"]=thread
        if buttons is not None:
            kwargs["reply_markup"]=buttons
        return await asyncio.wait_for(self.app.bot.send_message(**kwargs),timeout=18)

    async def send_notice(self,identity):
        from telegram import InputFile
        document=InputFile(io.BytesIO(self.store.privacy.notice.pdf),filename="Corelab-aviso-privacidad-piloto.pdf")
        args={"chat_id":identity["chat_id"],"document":document,
              "caption":"Revisa este aviso antes de decidir. Piloto demostrativo: utiliza datos ficticios.",
              "parse_mode":None,"connect_timeout":5,"read_timeout":15,"write_timeout":15,"pool_timeout":3}
        if identity.get("thread_id"):args["message_thread_id"]=identity["thread_id"]
        return await asyncio.wait_for(self.app.bot.send_document(**args),timeout=25)

    async def privacy_flow(self,identity,action):
        from telegram import InlineKeyboardButton,InlineKeyboardMarkup
        if action not in {"solicitar","consultar","aviso","retirar"}:
            raise ValueError("Accion de privacidad desconocida")
        lock=self.privacy_locks.setdefault(identity["user_id"],asyncio.Lock())
        async with lock:
            bot=self.app.bot.id
            current=await asyncio.to_thread(self.store.privacy.current,identity,bot)
            result={"success":True,"autorizado":bool(current),"aviso":self.store.privacy.notice.version,
                    "piloto_datos_ficticios":True}
            if action=="consultar":return result
            if current and action=="solicitar":return result
            if current and action=="aviso":
                await self.send_notice(identity)
                return result
            if action=="retirar" and not current:return result
            kind="withdraw" if action=="retirar" else "consent"
            prompt=await asyncio.to_thread(self.store.privacy.prompt,identity,bot,kind)
            if prompt["state"]=="ready":
                return {**result,"estado":"esperando_decision_del_cliente"}
            try:
                if kind=="consent":
                    doc=await self.send_notice(identity)
                    await asyncio.to_thread(self.store.privacy.sent,prompt["token"],document=doc.message_id)
                    text="¿Autorizas el tratamiento de tus propios datos para gestionar tus citas segun el aviso?\nPiloto: utiliza datos ficticios."
                    buttons=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Autorizo mis datos",callback_data="clp:accept:"+prompt["token"]),
                        InlineKeyboardButton("No autorizo",callback_data="clp:decline:"+prompt["token"])]])
                else:
                    text="¿Quieres retirar tu autorizacion? Se bloquearan nuevas solicitudes. Esto no cancela automaticamente las citas ya confirmadas."
                    buttons=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Retirar autorizacion",callback_data="clp:withdraw:"+prompt["token"]),
                        InlineKeyboardButton("Mantener",callback_data="clp:keep:"+prompt["token"])]])
                sent=await self.send(identity["chat_id"],text,thread=identity.get("thread_id"),buttons=buttons)
                await asyncio.to_thread(self.store.privacy.sent,prompt["token"],anchor=sent.message_id)
            except Exception:
                await asyncio.to_thread(self.store.privacy.failed,prompt["token"])
                raise RuntimeError("No pude confirmar la entrega del aviso o sus botones. No se registro ninguna autorizacion") from None
            return {**result,"estado":"esperando_decision_del_cliente"}

    async def privacy_callback(self,update,_context):
        from telegram.ext import ApplicationHandlerStop
        query=update.callback_query
        try:
            message,user=query.message,query.from_user
            if (not message or not user or user.is_bot or message.chat.type!="private" or
                    message.chat.id!=user.id or not message.from_user or message.from_user.id!=self.app.bot.id):
                raise PermissionError("Confirmacion de privacidad no autorizada")
            await query.answer("Procesando tu decision")
            identity={"user_id":user.id,"chat_id":message.chat.id,
                      "thread_id":getattr(message,"message_thread_id",None)}
            _,action,token=query.data.split(":",2)
            mapping={"accept":"accepted","decline":"declined","withdraw":"withdrawn","keep":"keep"}
            if action not in mapping:raise ValueError("Decision de privacidad no valida")
            await asyncio.to_thread(self.store.privacy.decide,token,mapping[action],identity,self.app.bot.id,
                                   message.message_id,identity["thread_id"],query.id)
            texts={"accept":"Autorizacion registrada para esta cuenta. Ahora indica tu nombre, cedula y telefono de contacto. Durante el piloto utiliza datos ficticios.",
                   "decline":"Sin esta autorizacion no podemos continuar el agendamiento por este bot. Puedes consultar informacion general o contactar al personal de Corelab.",
                   "withdraw":"Autorizacion retirada. Las nuevas solicitudes quedan bloqueadas. Para acceso, correccion o eliminacion de datos puedes pedir atencion humana.",
                   "keep":"Se mantuvo tu decision actual."}
            await self.send(user.id,texts[action],thread=identity["thread_id"])
        except Exception as exc:
            text=str(exc) if isinstance(exc,ValueError) else "No pude verificar esta decision. Solicita nuevamente el aviso o atencion humana."
            if query.message and query.from_user and query.message.chat.id==query.from_user.id:
                await self.send(query.from_user.id,text,thread=getattr(query.message,"message_thread_id",None))
        finally:
            raise ApplicationHandlerStop

    @staticmethod
    def buttons(booking, cancel=False):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        action="cancel" if cancel else "book"
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirmar cancelacion" if cancel else "Confirmar cita",
                                 callback_data="clc:"+action+":"+booking),
            InlineKeyboardButton("Volver",callback_data="clc:dismiss:"+booking)]])

    @staticmethod
    def receipt(row, *, staff=False):
        Service.patients(row["patients"])
        point=dt(row["start"])
        labels={"confirmed":"Cita confirmada", "cancelled":"Cita cancelada",
                "uncertain":"Solicitud de cita en verificacion", "failed":"No se confirmo la cita"}
        text=labels.get(row["state"],"Solicitud en proceso")+" · "+row["id"][:8].upper()+"\n"
        text+=point.strftime("%d/%m/%Y · %H:%M")+" (hora de Ecuador)\n"
        text+="Duracion: 30 minutos. Cita individual para una persona."
        text+="\nPaciente: "+row["contact"]
        text+="\nCedula: "+((row.get("cedula") or "No registrada") if staff else masked_cedula(row.get("cedula")))
        text+="\nTelefono: "+(row.get("telefono") or "No registrado")
        if staff:
            text+="\nAutorizacion de datos: "+("registrada" if row.get("consent_event_id") else "sin evidencia registrada en la cita anterior")
            text+="\nConsulta el calendario privado Corelab – Citas."
        elif row["state"] == "uncertain":
            text+="\nAun no esta confirmada. Te avisaremos por este chat cuando termine la verificacion."
        return text

    async def dispatch(self, name, args, identity):
        allowed={"consultar_cupos":{"fecha"},"solicitar_cita":{"inicio","contacto","cedula","telefono"},
                 "mis_citas":set(),"cancelar_cita":{"codigo"},"privacidad_citas":{"accion"}}
        if name not in allowed or not isinstance(args,dict):
            raise ValueError("Solicitud de agenda no valida")
        if "pacientes" in args:
            raise ValueError("Cada cita corresponde a una sola persona; solicita una cita distinta para cada paciente")
        if not set(args) <= allowed[name]:
            raise ValueError("La solicitud contiene argumentos no permitidos")
        if name=="privacidad_citas":
            return await self.privacy_flow(identity,args.get("accion","solicitar"))
        if name=="solicitar_cita" and not await asyncio.to_thread(self.store.privacy.current,identity,self.app.bot.id):
            result=await self.privacy_flow(identity,"solicitar")
            return {**result,"success":False,"requiere_autorizacion":True,
                    "message":"Debe decidir mediante el boton antes de pedir o registrar los datos de la cita. No se guardaron esos datos en el registro de citas."}
        if not allowed[name] <= set(args):
            missing=sorted(allowed[name]-set(args))
            raise ValueError("Faltan datos obligatorios: "+", ".join(missing)+". Solicitalos al cliente sin inventarlos")
        service=await asyncio.to_thread(self.service)
        if identity["user_id"] == service.settings["advisor_user_id"]:
            raise PermissionError("Esta cuenta es del asesor. Usa otra cuenta para reservar como cliente")
        if name == "consultar_cupos":
            return {"success":True,"zona":"America/Guayaquil","fecha":args["fecha"],
                    "unidad_cupo":"una cita individual para una persona","maximo_citas_por_franja":2,
                    "maximo_citas_activas_por_cliente":MAX_ACTIVE_BOOKINGS,
                    "franjas":await asyncio.to_thread(service.availability,args["fecha"])}
        if name == "mis_citas":
            return {"success":True,"citas":await asyncio.to_thread(service.own,identity)}
        if name == "solicitar_cita":
            row=await asyncio.to_thread(service.proposal,identity,args["inicio"],1,args["contacto"],
                                        args["cedula"],args["telefono"])
            if row["state"] != "proposed":
                return {"success":True,"estado":row["state"],"codigo":row["id"]}
            if not row["anchor"]:
                point=dt(row["start"])
                sent=await self.send(identity["chat_id"],"Confirma tu cita de Corelab:\n"+
                    point.strftime("%d/%m/%Y · %H:%M")+" (hora de Ecuador)\n30 minutos · Cita individual\nPaciente: "+row["contact"]+
                    "\nCedula: "+masked_cedula(row["cedula"])+"\nTelefono: "+row["telefono"]+
                    "\nEl cupo se comprobara al pulsar Confirmar cita. Este boton vence en 10 minutos.",
                    thread=identity.get("thread_id"),buttons=self.buttons(row["id"]))
                await asyncio.to_thread(self.store.update,row["id"],anchor=sent.message_id)
            return {"success":True,"estado":"esperando_confirmacion_del_cliente","codigo":row["id"],
                    "message":"Se envio el boton. La cita aun no esta reservada."}
        if name == "cancelar_cita":
            row,intent=await asyncio.to_thread(service.cancel_proposal,identity,args["codigo"])
            if not intent["anchor"]:
                sent=await self.send(identity["chat_id"],"¿Confirmas cancelar la cita "+row["id"][:8].upper()+
                    " del "+dt(row["start"]).strftime("%d/%m/%Y a las %H:%M")+"?\nPaciente: "+row["contact"],
                    thread=identity.get("thread_id"),buttons=self.buttons(intent["token"],cancel=True))
                await asyncio.to_thread(self.store.anchor_cancellation,intent["token"],sent.message_id)
            return {"success":True,"estado":"esperando_confirmacion_de_cancelacion"}
        raise ValueError("Herramienta de agenda desconocida")

    def tool(self, name, args):
        try:
            identity=bound_identity()
            if not self.loop or self.loop.is_closed() or not self.app or not self.app.running:
                raise RuntimeError("El gateway de Telegram no esta disponible")
            if threading.get_ident() == self.loop_thread:
                raise RuntimeError("La herramienta debe ejecutarse en el worker nativo de Hermes")
            future=asyncio.run_coroutine_threadsafe(self.dispatch(name,args,identity),self.loop)
            # No model write occurs here; writes require the customer's authenticated callback.
            return json.dumps(future.result(timeout=45),ensure_ascii=False)
        except Exception as exc:
            safe=str(exc) if isinstance(exc,(ValueError,PermissionError,RuntimeError)) else type(exc).__name__
            return json.dumps({"success":False,"error":safe},ensure_ascii=False)

    async def callback(self, update, _context):
        from telegram.ext import ApplicationHandlerStop
        query=update.callback_query
        try:
            message,user=query.message,query.from_user
            if (not message or not user or user.is_bot or message.chat.type != "private" or
                    message.chat.id != user.id or not message.from_user or message.from_user.id != self.app.bot.id):
                raise PermissionError("Confirmacion no autorizada")
            identity={"user_id":user.id,"chat_id":message.chat.id,
                      "thread_id":getattr(message,"message_thread_id",None)}
            await query.answer("Procesando tu solicitud")
            _,action,token=query.data.split(":",2)
            service=await asyncio.to_thread(self.service)
            if action == "book":
                row=await asyncio.to_thread(service.book,token,identity,
                    anchor=message.message_id,thread=identity["thread_id"])
            elif action == "cancel":
                row=await asyncio.to_thread(service.cancel,token,identity,message.message_id,identity["thread_id"])
            else:
                await asyncio.to_thread(self.dismiss,token,identity,message.message_id)
                await self.send(user.id,"Solicitud cerrada. Puedes elegir otra fecha cuando quieras.",
                                thread=identity["thread_id"])
                row=None
            if row and row["state"] == "conflict":
                await self.send(user.id,"Ese horario ya no tiene suficientes cupos. Elige otra hora.",
                                thread=identity["thread_id"])
            elif row and row["state"] in {"superseded","dismissed"}:
                await self.send(user.id,"Esta solicitud ya fue reemplazada o cerrada. Usa la confirmacion mas reciente.",
                                thread=identity["thread_id"])
            elif row and row["state"] == "failed":
                await self.send(user.id,"No pude confirmar la cita. Puedes solicitar ayuda al personal.",
                                thread=identity["thread_id"])
            elif row and row["state"] == "canceling":
                await self.send(user.id,"La cancelacion esta en verificacion; aun no esta confirmada.",
                                thread=identity["thread_id"])
            if self.wakeup:
                self.wakeup.set()
        except Exception as exc:
            safe=(str(exc) if isinstance(exc,ValueError) else "No pude verificar esta solicitud. "
                  "Puedes pedir ayuda a nuestro personal.")
            if query.message and query.from_user and query.message.chat.id == query.from_user.id:
                await self.send(query.from_user.id,safe,thread=getattr(query.message,"message_thread_id",None))
        finally:
            raise ApplicationHandlerStop

    def dismiss(self, token, identity, anchor):
        with self.store.transaction() as conn:
            intent=conn.execute("SELECT * FROM cancellations WHERE token=?",(token,)).fetchone()
            row=self.store.one(intent["booking"] if intent else token,conn)
            self.store.owner(row,identity,self.app.bot.id)
            if (intent["anchor"] if intent else row["anchor"])!=anchor:
                raise PermissionError("Boton no autorizado")
            if intent:
                if intent["thread"]!=identity.get("thread_id"):
                    raise PermissionError("Conversacion incorrecta")
                conn.execute("UPDATE cancellations SET state='dismissed' WHERE token=? AND state='proposed'",(token,))
            else:
                if row["thread"]!=identity.get("thread_id"):
                    raise PermissionError("Conversacion incorrecta")
                conn.execute("UPDATE bookings SET state='dismissed' WHERE id=? AND state='proposed'",(token,))

    async def deliver(self, row, field, settings):
        from telegram.error import RetryAfter, Forbidden
        if row[field] != "pending":
            return
        due="notify_due" if field == "notify" else "customer_due"
        if row[due] > time.time():
            return
        if field == "notify":
            target=await asyncio.to_thread(advisor,self.home,self.app.bot.id,int(settings["advisor_user_id"]))
            thread=None  # General in the existing advisor DM, separate from human cases.
        else:
            target,thread=row["chat"],row["thread"]
        if not await asyncio.to_thread(self.store.claim_notice,row["id"],field,row["state"]):
            return
        try:
            sent=await self.send(target,self.receipt(row,staff=field=="notify"),thread=thread)
            await asyncio.to_thread(self.store.finish_notice,row["id"],field,row["state"],"sent")
        except RetryAfter as exc:
            seconds=exc.retry_after.total_seconds() if hasattr(exc.retry_after,"total_seconds") else float(exc.retry_after)
            await asyncio.to_thread(self.store.finish_notice,row["id"],field,row["state"],"pending",time.time()+seconds+1)
        except Forbidden:
            await asyncio.to_thread(self.store.finish_notice,row["id"],field,row["state"],"failed")
        except Exception:
            # Telegram lacks sendMessage idempotency. A timeout may already have delivered.
            # Keep an explicit uncertain state instead of silently duplicating patient notices.
            await asyncio.to_thread(self.store.finish_notice,row["id"],field,row["state"],"ambiguous")

    async def maintain(self, application, adapter):
        self.app=application
        self.loop=asyncio.get_running_loop()
        self.loop_thread=threading.get_ident()
        self.wakeup=asyncio.Event()
        await asyncio.to_thread(self.store.recover_notices)
        while not application.running:
            await asyncio.sleep(.25)
        while application.running:
            status={"pid":os.getpid(),"process_fingerprint":fingerprint(os.getpid()),
                    "checked_at":datetime.now(timezone.utc).isoformat(),"stage":"starting",
                    "version":plugin_version,"patients_per_booking":1,"max_bookings_per_slot":2,
                    "max_active_bookings_per_customer":MAX_ACTIVE_BOOKINGS,
                    "required_patient_fields":["contacto","cedula","telefono"],
                    "privacy_notice_version":self.store.privacy.notice.version,
                    "privacy_notice_sha256":self.store.privacy.notice.digest,
                    "consent_required":True,"pilot_test_data_only":True,"automatic_deletion":False}
            try:
                stored=await asyncio.to_thread(read_json,self.directory/"config.json",{})
                if not stored.get("enabled"):
                    status["stage"]="paused" if stored.get("calendar_id") else "waiting_google_connection"
                    await asyncio.to_thread(write_json,self.home/"logs/corelab-citas.json",status)
                    await asyncio.sleep(5)
                    continue
                service=await asyncio.to_thread(self.service)
                metadata=await asyncio.to_thread(service.google.metadata)
                if metadata.get("id") != service.settings["calendar_id"] or metadata.get("timeZone") != "America/Guayaquil":
                    raise ValueError("El calendario debe ser el configurado y usar America/Guayaquil")
                await asyncio.to_thread(service.recover)
                rows=await asyncio.to_thread(self.store.rows,bot=application.bot.id)
                for row in rows:
                    if row["state"] in {"confirmed","cancelled","uncertain"}:
                        await self.deliver(row,"customer_notice",service.settings)
                        await self.deliver(row,"notify",service.settings)
                rows=await asyncio.to_thread(self.store.rows,bot=application.bot.id)
                status.update(stage="ready",bot_id=application.bot.id,calendar=metadata.get("summary"),
                              pending_review=sum(r["state"] in {"creating","uncertain","canceling"} or
                                  r["notify"] in {"ambiguous","failed","sending"} or
                                  r["customer_notice"] in {"ambiguous","failed","sending"} for r in rows))
            except Exception as exc:
                status.update(stage="error",error_type=type(exc).__name__)
                if hasattr(exc,"status"):
                    status["google_http_status"]=exc.status
                logger.warning("Appointment maintenance requires attention (%s)",type(exc).__name__)
            await asyncio.to_thread(write_json,self.home/"logs/corelab-citas.json",status)
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(),timeout=30)
            except asyncio.TimeoutError:
                pass


def register(ctx):
    for name in ("register_platform_handler","spawn_task","register_tool"):
        if not callable(getattr(ctx,name,None)):
            raise RuntimeError("Esta version de Hermes no admite "+name)
    runtime=Runtime(Path(os.environ["HERMES_HOME"]).resolve())
    def wire(application,adapter):
        from telegram.ext import CallbackQueryHandler
        application.add_handler(CallbackQueryHandler(runtime.privacy_callback,
            pattern=r"^clp:(accept|decline|withdraw|keep):[a-f0-9]{32}$",block=True),group=-96)
        application.add_handler(CallbackQueryHandler(runtime.callback,
            pattern=r"^clc:(book|cancel|dismiss):[a-f0-9]{32}$",block=True),group=-95)
        ctx.spawn_task(runtime.maintain(application,adapter),name="corelab-citas:maintenance")
    ctx.register_platform_handler("telegram",wire)
    definitions=[
        ("privacidad_citas","Muestra el PDF del aviso y botones nativos antes de pedir datos de una cita, consulta la autorizacion de la cuenta o permite retirarla. El modelo no puede aceptar por el cliente.",
         {"accion":{"type":"string","enum":["solicitar","consultar","aviso","retirar"],"default":"solicitar"}},[]),
        ("consultar_cupos","Consulta cupos para citas individuales de Corelab. Cada franja de 30 minutos admite dos citas distintas de una persona cada una.",
         {"fecha":{"type":"string","description":"Fecha local YYYY-MM-DD"}},["fecha"]),
        ("solicitar_cita","Envia un boton para confirmar UNA cita de UNA persona tras la autorizacion nativa. Piloto: datos propios ficticios. Exige nombre, cedula y telefono proporcionados por el cliente. Maximo tres citas activas por cuenta. Todavia no reserva.",
         {"inicio":{"type":"string","description":"Hora ISO con zona -05:00 devuelta por consultar_cupos"},
          "contacto":{"type":"string","maxLength":100,
             "description":"Nombre de la unica persona que se atendera en esta cita, proporcionado por el cliente; no inventar"},
          "cedula":{"type":"string","pattern":"^[0-9]{10}$",
             "description":"Cedula del paciente, 10 digitos proporcionados por el cliente. Nunca inventar ni completar"},
          "telefono":{"type":"string","maxLength":40,
             "description":"Telefono de contacto proporcionado por el cliente. Formato Ecuador o internacional con + y codigo de pais. Nunca inventar"}},["inicio","contacto","cedula","telefono"]),
        ("mis_citas","Muestra exclusivamente las citas registradas por la cuenta actual de Telegram.",{},[]),
        ("cancelar_cita","Envia un boton para que el cliente confirme cancelar una cita propia.",
         {"codigo":{"type":"string","description":"Codigo completo devuelto por mis_citas"}},["codigo"])]
    for name,description,properties,required in definitions:
        def handler(args,_name=name,**kwargs):
            return runtime.tool(_name,args)
        ctx.register_tool(name=name,toolset="corelab_citas",handler=handler,
            schema={"name":name,"description":description,"parameters":{"type":"object",
                "additionalProperties":False,"properties":properties,"required":required}})
