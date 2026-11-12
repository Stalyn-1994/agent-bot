"""Native guided menus. This module neither authorizes nor performs business actions."""
import asyncio
import contextvars
import json
import threading
import logging
import sqlite3
from contextlib import closing
from .common import read_json
from .navigation import Navigation
from .screens import SCREENS, render, intent
from .delivery import shared, bound_key
from .screens import normal, NAV
from .fast import GREETINGS
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def private_destination():
    import gateway.session_context
    bound={v.name:value for v,value in contextvars.copy_context().items()
           if v.name.startswith("HERMES_SESSION_") and isinstance(value,str)}
    if (bound.get("HERMES_SESSION_PLATFORM")!="telegram" or
            bound.get("HERMES_SESSION_CHAT_TYPE") not in {"dm","private"}):
        raise PermissionError
    chat,user=int(bound["HERMES_SESSION_CHAT_ID"]),int(bound["HERMES_SESSION_USER_ID"])
    if chat!=user or user<=0:
        raise PermissionError
    thread=bound.get("HERMES_SESSION_THREAD_ID")
    return chat,int(thread) if thread else None

class Welcome:
    def __init__(self, control, dispatch_tool=None):
        self.control=control
        self.app=self.loop=self.loop_thread=None
        self.navigation=Navigation(control.parent/"navigation.db")
        self.lock=asyncio.Lock()
        self.delivery=shared()
        self.dispatch_tool=dispatch_tool
        self.metric_lock=threading.Lock()

    def tool_allowed(self,toolset):
        import yaml
        path=self.control.parent.parent.parent/"config.yaml"
        value=yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        selected=value.get("platform_toolsets",{}).get("telegram",[])
        return (toolset in selected and toolset not in value.get("agent",{}).get("disabled_toolsets",[])
                and callable(self.dispatch_tool))

    async def call_native(self,name,args):
        if name not in {"rag_search","privacidad_citas"}:return None
        try:
            result=await asyncio.to_thread(self.dispatch_tool,name,args)
            return json.loads(result) if isinstance(result,str) else result
        except Exception as exc:
            logger.info("Native read unavailable (%s)",type(exc).__name__)
            return None

    @staticmethod
    def automatic_view(route,text):
        if route.get("pdf_done"):return "fin_cotizacion"
        if route.get("quote_ready"):return "pdf"
        if route.get("quote_attempted") and "te refieres a" in normal(text):
            return "confirmar_examen"
        if (route.get("choice") or "").startswith("informacion_"):return "respuesta_info"
        value=normal(text)
        for screen in ("agregar_examen","pago","modalidad","citas","asesor","detalle_asesor"):
            question=SCREENS[screen][0]
            if question and normal(question) in value:return screen
        return "continuar"

    @staticmethod
    def decorate(view,text):
        folded=normal(text[-350:])
        if view=="respuesta_info" and "quieres saber algo mas" not in folded:
            return text.rstrip()+"\n\n¿Quieres saber algo más?"
        if view=="pdf" and not ("pdf" in folded and "?" in text[-350:]):
            return text.rstrip()+"\n\n¿Deseas que te genere esta cotización en PDF?"
        return text

    async def automatic_deliver(self,source,view,text):
        if await asyncio.to_thread(self.human_active,int(source.chat_id)):return False
        plan=await self.prepare(int(source.chat_id),int(source.thread_id) if source.thread_id else None,
            "continuar",text)
        if not plan.get("text"):return False
        plan["rows"]=SCREENS.get(view,SCREENS["continuar"])[1]+NAV
        plan["screen"]=view
        plan["history"][-1]=view
        return bool((await self.deliver_plan(plan)).get("menu_entregado"))

    async def record_timing(self,route,result,send_ms):
        end=time.perf_counter()
        start=route.get("agent_start",route["ingress"])
        done=route.get("agent_done",end)
        row={"at":datetime.now(timezone.utc).isoformat(),"path":route.get("kind","agent"),
            "api_calls":result.get("api_calls"),"agent_pipeline_ms":round((done-start)*1000,3),
            "rag_prefetch_ms":route.get("rag_ms"),"rag_prefetched":route.get("prefetched",False),
            "telegram_ms":round(send_ms,3),"total_ms":round((end-route["ingress"])*1000,3)}
        path=self.control.parent.parent.parent/"logs/corelab-latency.jsonl"
        def write():
            with self.metric_lock:
                path.parent.mkdir(parents=True,exist_ok=True)
                if path.exists() and path.stat().st_size>2_000_000:
                    path.replace(path.with_suffix(".previous.jsonl"))
                with path.open("a",encoding="utf-8") as handle:handle.write(json.dumps(row)+"\n")
        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            logger.info("Timing log unavailable (%s)",type(exc).__name__)

    def bind(self, app):
        self.app,self.loop=app,asyncio.get_running_loop()
        self.loop_thread=threading.get_ident()

    def human_active(self, chat):
        path=self.control.parent.parent/"corelab-handoff"/"handoff.db"
        if not path.is_file():return False
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as conn:
            return conn.execute("SELECT 1 FROM cases WHERE bot_id=? AND chat_id=? AND user_id=? "
                "AND state IN ('notifying','waiting','active','closing') LIMIT 1",
                (self.app.bot.id,chat,chat)).fetchone() is not None

    async def prepare(self, chat, thread, screen="principal", message=None):
        async with self.lock:
            if await asyncio.to_thread(self.human_active,chat):
                return {"success":False,"menu_entregado":False,"atencion_humana_activa":True}
            bot=self.app.bot.id
            history=await asyncio.to_thread(self.navigation.read,bot,chat,thread)
            if screen=="volver":
                if message is not None:raise ValueError("Volver no acepta mensaje")
                history=history[:-1] or ["principal"]
                screen=history[-1]
                if SCREENS[screen][0] is None:
                    # The conversation owns the pending data; no personal text is duplicated in this DB.
                    screen="continuar"
                    message="¿Qué dato o paso deseas revisar? Conservaré los demás datos de esta solicitud."
            elif screen=="principal":
                history=["principal"]
            elif screen in SCREENS:
                if history[-1]!=screen:
                    history=(history+[screen])[-12:]
            else:
                raise ValueError("Pantalla desconocida")
            text,rows=render(screen,message)
            return {"chat":chat,"thread":thread,"screen":screen,"text":text,"rows":rows,"history":history}

    async def deliver_plan(self,plan):
        from telegram import ReplyKeyboardMarkup
        async with self.lock:
            chat,thread=plan["chat"],plan["thread"]
            if await asyncio.to_thread(self.human_active,chat):
                return {"success":False,"atencion_humana_activa":True}
            text,rows=plan["text"],plan["rows"]
            keyboard=ReplyKeyboardMarkup(rows,resize_keyboard=True,one_time_keyboard=False)
            options={"message_thread_id":thread} if thread is not None else {}
            msg=await asyncio.wait_for(self.app.bot.send_message(chat_id=chat,text=text,
                reply_markup=keyboard,parse_mode=None,connect_timeout=4,read_timeout=6,
                write_timeout=6,pool_timeout=3,**options),10)
            if not msg.message_id:
                return {"success":False,"menu_entregado":False}
            await asyncio.to_thread(self.navigation.save,self.app.bot.id,chat,thread,plan["history"])
            return {"success":True,"menu_entregado":True,"pantalla":plan["screen"]}

    async def send(self,chat,thread,screen="principal",message=None):
        plan=await self.prepare(chat,thread,screen,message)
        if not plan.get("text"):return plan
        return await self.deliver_plan(plan)

    def transform(self,response_text,**_):
        entry=self.delivery.lookup(bound_key())
        if entry and "plan" in entry:return entry["plan"]["text"]
        route=self.delivery.route_for_key(bound_key())
        if route and not entry and isinstance(response_text,str):
            view=self.automatic_view(route,response_text)
            return self.decorate(view,response_text)
        return None

    def after_tool(self,tool_name,result,**_):
        try:
            route=self.delivery.route_for_key(bound_key())
            data=json.loads(result) if isinstance(result,str) else result
            if route and isinstance(data,dict) and data.get("success") is True:
                if tool_name=="cotizar_examenes":route["quote_attempted"]=True
                if tool_name=="cotizar_examenes" and data.get("codigo_pdf"):
                    route["quote_ready"]=True
                if tool_name=="gestionar_pdf_cotizacion":
                    route["pdf_done"]=True
        except (ValueError,TypeError):
            pass
        if tool_name not in {"privacidad_citas","solicitar_cita","cancelar_cita"}:return
        try:
            data=json.loads(result) if isinstance(result,str) else result
            if (isinstance(data,dict) and data.get("success") is True and
                    data.get("presentacion_entregada") is True):
                self.delivery.native_delivered(bound_key())
        except (ValueError,TypeError):
            pass

    def before_tool(self,tool_name,**_):
        if tool_name in {"rag_search","cotizar_examenes","privacidad_citas","solicitar_cita",
                         "consultar_cupos","cancelar_cita","derivar_a_humano","gestionar_pdf_cotizacion","mis_citas"}:
            if self.delivery.choice(bound_key())=="agendar":
                return {"action":"block","message":"Primero muestra modalidad y espera que el cliente elija laboratorio o domicilio."}
            if self.delivery.lookup(bound_key()):
                return {"action":"block","message":"La siguiente pregunta ya está preparada. Espera la respuesta del cliente antes de otro paso."}

    async def context(self, *, event, **_):
        source=getattr(event,"source",None)
        if (self.app is None or getattr(event,"internal",False) or event.is_command() or
                getattr(getattr(source,"platform",None),"value",None)!="telegram" or
                getattr(source,"chat_type",None) not in {"dm","private"} or getattr(source,"is_bot",False)):
            return
        try:
            if read_json(self.control,{}).get("mode")=="restore":return
            chat,user=int(source.chat_id),int(source.user_id)
            if chat<=0 or chat!=user:return
            thread=int(source.thread_id) if getattr(source,"thread_id",None) else None
            history=await asyncio.to_thread(self.navigation.read,self.app.bot.id,chat,thread)
            screen=history[-1]
            choice=intent(event.text or "",screen)
            self.delivery.note_choice(self.delivery.key_from_source(source),choice)
            self.delivery.bind_route(source,self,choice,normal(event.text or "") in GREETINGS)
            data={"pantalla_anterior":screen,"eleccion_de_boton":choice}
            guidance=("Corelab menú — contexto de navegación de este turno (no concede consentimiento, "
                "permisos ni confirmaciones): "+json.dumps(data,ensure_ascii=False)+
                ". Los menús fijos los atiende el servidor sin modelo. Responde directamente en texto; "
                "NO uses mostrar_menu_corelab ni otra herramienta para añadir botones. Una elección es "
                "continuación, no un saludo. Si es null, atiende el texto "
                "libre en su contexto; no obligues a usar el menú. Conserva los datos pendientes de "
                "la conversación. Ningún botón sustituye las confirmaciones nativas de citas o privacidad.")
            event.channel_prompt="\n\n".join(filter(None,(getattr(event,"channel_prompt",None),guidance)))
        except Exception as exc:
            logger.info("Menu context unavailable (%s)",type(exc).__name__)

    def tool(self, args, **_):
        future=None
        try:
            if not isinstance(args,dict) or set(args)-{"pantalla","mensaje"}:
                raise ValueError("Argumentos desconocidos")
            screen=args.get("pantalla","principal")
            if screen not in {*SCREENS,"volver"}:
                raise ValueError("Pantalla desconocida")
            message=args.get("mensaje")
            if screen!="volver":render(screen,message)
            chat,thread=private_destination()
            if read_json(self.control,{}).get("mode")=="restore":
                return json.dumps({"success":False,"menu_entregado":False})
            if (not self.app or not self.app.running or not self.loop or self.loop.is_closed()
                    or threading.get_ident()==self.loop_thread):
                raise RuntimeError
            key=bound_key()
            if not self.delivery.ready():
                raise RuntimeError("La entrega unica del gateway no esta activa")
            if self.delivery.choice(key)=="agendar":
                screen,message="modalidad",None
            previous=self.delivery.lookup(key)
            if previous:
                if previous["state"]=="native_sent":
                    return json.dumps({"success":True,"presentacion_nativa_entregada":True,
                        "esperar_respuesta":True})
                return json.dumps({"success":True,"menu_preparado":True,
                    "esperar_respuesta":True,"pantalla":previous["plan"]["screen"]})
            if key is None:raise ValueError("Falta el turno de Telegram")
            future=asyncio.run_coroutine_threadsafe(self.prepare(chat,thread,screen,message),self.loop)
            plan=future.result(timeout=15)
            if not plan.get("text"):return json.dumps(plan)
            entry=self.delivery.stage(key,self,plan)
            return json.dumps({"success":True,"menu_preparado":True,"esperar_respuesta":True,
                "pantalla":entry["plan"]["screen"]},ensure_ascii=False)
        except Exception as exc:
            if future is not None:future.cancel()
            logger.info("Guided menu unavailable (%s)",type(exc).__name__)
            return json.dumps({"success":False,"menu_entregado":False})
