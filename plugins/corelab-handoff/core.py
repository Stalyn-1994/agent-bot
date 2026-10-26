"""Same-bot human handoff through a fixed, locally authorized Telegram group."""
import asyncio
import contextvars
import json
import logging
import threading
import time
from .store import Store

logger = logging.getLogger("corelab_handoff")
REASONS = {"customer_request", "knowledge_missing", "staff_confirmation", "clinical_question"}
REASON_LABELS = {"customer_request": "Solicitud del cliente", "knowledge_missing": "Información no confirmada",
                 "staff_confirmation": "Confirmación del personal", "clinical_question": "Consulta clínica"}


def bound_identity():
    import gateway.session_context  # Register the actual host ContextVars.
    bound = {var.name: value for var, value in contextvars.copy_context().items()
             if var.name.startswith("HERMES_SESSION_") and isinstance(value, str)}
    if (bound.get("HERMES_SESSION_PLATFORM", "").lower() != "telegram" or
            bound.get("HERMES_SESSION_CHAT_TYPE", "").lower() not in {"dm", "private"}):
        raise PermissionError("La derivacion requiere un chat privado de Telegram identificado por Hermes")
    user = int(bound.get("HERMES_SESSION_USER_ID", "0"))
    chat = int(bound.get("HERMES_SESSION_CHAT_ID", "0"))
    if user <= 0 or chat <= 0:
        raise PermissionError("Falta la identidad vinculada por Hermes")
    thread = bound.get("HERMES_SESSION_THREAD_ID", "")
    return {"chat_id": chat, "user_id": user, "thread_id": int(thread) if thread else None,
            "session_id": bound.get("HERMES_SESSION_ID", "")}


class Runtime:
    def __init__(self, home):
        self.store = Store(home / "plugin-data" / "corelab-handoff" / "handoff.db")
        self.application = self.loop = None
        self.loop_thread = None
        self.locks = {}
        self.topics_checked = 0

    def case_lock(self, case_id):
        return self.locks.setdefault(case_id, asyncio.Lock())

    def bind(self, application):
        if self.application is None:
            self.store.recover_interrupted()
        self.application = application
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()

    @property
    def bot(self):
        if self.application is None:
            raise RuntimeError("La conexion de Telegram aun no esta disponible")
        return self.application.bot

    async def send(self, chat, text, *, thread=None, reply=None, buttons=None):
        from telegram import ReplyParameters
        if int(chat) < 0:
            config = self.store.config()
            if config.get("group_id") != int(chat) or config.get("bot_id") != self.bot.id:
                raise PermissionError("Destino de atencion no autorizado")
            info = await asyncio.wait_for(self.bot.get_chat(int(chat), connect_timeout=5,
                read_timeout=10, write_timeout=5, pool_timeout=5), timeout=15)
            if getattr(info, "username", None) or getattr(info, "active_usernames", None):
                raise PermissionError("El grupo de atencion dejo de ser privado; entrega bloqueada")
        kwargs = {"chat_id": int(chat), "text": text, "parse_mode": None,
                  "disable_web_page_preview": True, "connect_timeout": 5,
                  "read_timeout": 10, "write_timeout": 5, "pool_timeout": 5}
        if thread:
            kwargs["message_thread_id"] = int(thread)
        if reply:
            kwargs["reply_parameters"] = ReplyParameters(message_id=int(reply))
        if buttons is not None:
            kwargs["reply_markup"] = buttons
        return await asyncio.wait_for(self.bot.send_message(**kwargs), timeout=20)

    async def require_topics(self, force=False):
        if not force and self.topics_checked and time.monotonic() - self.topics_checked < 20:
            return
        me = await asyncio.wait_for(self.bot.get_me(connect_timeout=5, read_timeout=8,
                write_timeout=5, pool_timeout=5), timeout=12)
        enabled = getattr(me, "has_topics_enabled", None)
        if enabled is None:
            enabled = getattr(me, "api_kwargs", {}).get("has_topics_enabled")
        if enabled is not True:
            raise RuntimeError("Habilita los temas en chats privados para este bot desde BotFather; "
                               "getMe aun no confirma has_topics_enabled=true")
        self.topics_checked = time.monotonic()

    async def register_private(self, message, token):
        user = message.from_user
        if (message.chat.type != "private" or not user or user.is_bot or message.sender_chat or
                user.id != message.chat.id):
            raise PermissionError("Envia este codigo desde tu cuenta humana en el chat privado del bot")
        self.store.check_private_token(token, self.bot.id, user.id)
        await self.require_topics(force=True)
        # Probe with non-customer data before granting a private destination.
        topic = await asyncio.wait_for(self.bot.create_forum_topic(chat_id=user.id,
            name="Configuración de atención Corelab", connect_timeout=5, read_timeout=8,
            write_timeout=5, pool_timeout=5), timeout=12)
        await self.send(user.id, "Verificación de temas privados de Corelab. Este tema es de configuración.",
                        thread=topic.message_thread_id)
        # closeForumTopic only supports forum supergroups. Private topics stay as
        # history; successful creation and delivery are the enrollment probe.
        self.store.consume_private(token, self.bot.id, user.id, message.chat.id, user.full_name)

    @staticmethod
    def buttons(case_id):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Tomar caso", callback_data="clh:take:" + case_id),
            InlineKeyboardButton("Cerrar caso", callback_data="clh:close:" + case_id),
        ]])

    async def request(self, identity, reason, summary):
        routing = self.store.config()
        if not routing or routing.get("bot_id") != self.bot.id:
            raise RuntimeError("El grupo de atencion no esta vinculado a este bot")
        private = self.store.mode() == "private_topics"
        if private:
            if self.store.is_operator_chat(self.bot.id, identity["chat_id"], identity["user_id"]):
                raise PermissionError("Usa otra cuenta para probar como cliente; esta cuenta es de un asesor")
            await self.require_topics()
        case, created = self.store.create(identity, self.bot.id, reason, summary, private=private)
        if created:
            async with self.case_lock(case["id"]):
                try:
                    destination, thread = routing["group_id"], None
                    route = self.store.private_route(case["id"])
                    if route:
                        destination = route["operator_chat"]
                        self.store.authorize_private(self.bot.id, destination, case["operator_id"])
                        topic = await asyncio.wait_for(self.bot.create_forum_topic(chat_id=destination,
                            name="Caso " + case["id"], connect_timeout=5, read_timeout=8,
                            write_timeout=5, pool_timeout=5), timeout=12)
                        thread = topic.message_thread_id
                        self.store.set_topic(case["id"], destination, thread)
                    instructions = ("Pulsa Tomar caso. Despues escribe directamente dentro de este tema. "
                        "Solo texto, hasta 3500 caracteres." if route else
                        "Pulsa Tomar caso. Despues usa Responder sobre este mensaje o sobre "
                        "un mensaje del cliente reenviado por el bot. Solo texto, hasta 3500 caracteres.")
                    sent = await self.send(destination,
                        f"Caso {case['id']}\nMotivo: {REASON_LABELS[reason]}\nResumen: {summary}\n\n"
                        + instructions,
                        buttons=self.buttons(case["id"]), thread=thread)
                    self.store.route(self.bot.id, destination, sent.message_id, case["id"])
                    self.store.notified(case["id"], sent.message_id)
                except BaseException:
                    self.store.notification_failed(case["id"])
                    raise
            case = self.store.case(case["id"])
        if case["state"] == "notifying":
            return {"success": False, "error": "La solicitud todavia no tiene entrega confirmada"}
        return {"success": True, "case_id": case["id"], "status": case["state"],
                "message": "Solicitud enviada al canal autorizado de atencion; no garantiza disponibilidad inmediata"}

    def tool(self, args, **kwargs):
        try:
            if not isinstance(args, dict) or set(args) - {"reason", "summary"}:
                raise PermissionError("Argumentos no permitidos")
            reason, summary = args.get("reason", "customer_request"), args.get("summary", "")
            if reason not in REASONS or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 600:
                raise ValueError("Se requiere un motivo valido y un resumen de 1 a 600 caracteres")
            identity = bound_identity()
            host_session = kwargs.get("session_id")
            if host_session and identity["session_id"] and host_session != identity["session_id"]:
                raise PermissionError("Las identidades de sesion no coinciden")
            if self.loop is None or not self.loop.is_running():
                raise RuntimeError("Telegram no esta conectado")
            if threading.get_ident() == self.loop_thread:
                raise RuntimeError("La herramienta requiere el trabajador de herramientas de Hermes")
            future = asyncio.run_coroutine_threadsafe(self.request(identity, reason, summary.strip()), self.loop)
            try:
                result = future.result(timeout=50)
            except TimeoutError:
                # Do not start another send or claim a delivery we did not observe.
                return json.dumps({"success": False, "error": "No se pudo confirmar la derivacion a tiempo"})
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Human handoff rejected/failed: %s", type(exc).__name__)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    def customer_case(self, message):
        if (message.chat.type != "private" or not message.from_user or
                message.from_user.is_bot or message.sender_chat):
            return None
        return self.store.active(self.bot.id, message.chat.id, message.from_user.id)

    async def customer_message(self, message):
        case = self.customer_case(message)
        if case is None:
            return False
        async with self.case_lock(case["id"]):
            if self.customer_case(message) is None:
                return False
            await self._customer_message(message, case)
            return True

    async def _customer_message(self, message, case):
        if not message.text or len(message.text) > 3500:
            await self.send(case["chat_id"], "La atención humana por este canal admite texto. "
                "Escribe tu consulta en mensajes de hasta 3500 caracteres; los archivos no se han reenviado.",
                thread=case["thread_id"])
            return
        routing = self.store.config()
        if routing.get("bot_id") != case["bot_id"]:
            raise RuntimeError("La vinculacion de Telegram cambio")
        key = f"customer:{self.bot.id}:{message.chat.id}:{message.message_id}"
        if not self.store.reserve_delivery(key, case["id"]):
            return
        try:
            route = self.store.private_route(case["id"])
            destination = route["operator_chat"] if route else routing["group_id"]
            if route:
                self.store.authorize_private(self.bot.id, destination, case["operator_id"])
            sent = await self.send(destination, f"Cliente · Caso {case['id']}\n{message.text}",
                reply=case["anchor"], thread=route["operator_topic"] if route else None)
            self.store.route(self.bot.id, destination, sent.message_id, case["id"])
            self.store.delivery(key, True)
        except Exception:
            self.store.delivery(key, False, "Telegram delivery not confirmed")
            await self.send(case["chat_id"], "No pude confirmar la entrega de ese mensaje al personal. "
                "Por favor, vuelve a enviarlo. La atención humana sigue abierta.", thread=case["thread_id"])

    async def operator_reply(self, message):
        if not message.from_user or message.from_user.is_bot or message.sender_chat:
            raise PermissionError("No se aceptan respuestas anonimas ni de otros bots")
        if message.chat.type == "private":
            self.store.authorize_private(self.bot.id, message.chat.id, message.from_user.id)
            case = self.store.topic_case(self.bot.id, message.chat.id, getattr(message, "message_thread_id", None))
            reply = message.reply_to_message
            if reply:
                other = self.store.lookup(self.bot.id, message.chat.id, reply.message_id)
                if other and case and other["id"] != case["id"]:
                    raise PermissionError("La respuesta pertenece a otro tema de atención")
            if not case:
                raise ValueError("Abre el tema del caso asignado. Los mensajes fuera de un tema no se envian al cliente")
        else:
            if self.store.mode() == "private_topics":
                raise PermissionError("La atencion se gestiona en el chat privado de cada asesor")
            self.store.authorize(self.bot.id, message.chat.id, message.from_user.id)
            reply = message.reply_to_message
            if not reply or not reply.from_user or reply.from_user.id != self.bot.id:
                return False
            case = self.store.lookup(self.bot.id, message.chat.id, reply.message_id)
        if not case:
            raise ValueError("Responde a un mensaje de caso enviado por este bot")
        async with self.case_lock(case["id"]):
            return await self._operator_reply(message, case)

    async def _operator_reply(self, message, case):
        self.store.assigned(case["id"], message.from_user.id)
        if not message.text or not 1 <= len(message.text) <= 3500:
            raise ValueError("La respuesta debe ser texto de hasta 3500 caracteres")
        key = f"operator:{self.bot.id}:{message.chat.id}:{message.message_id}"
        if not self.store.reserve_delivery(key, case["id"]):
            return False
        try:
            await self.send(case["chat_id"], "Atención Corelab:\n" + message.text, thread=case["thread_id"])
            self.store.delivery(key, True)
            return True
        except Exception:
            self.store.delivery(key, False, "Telegram delivery not confirmed")
            raise RuntimeError("No se confirmo la entrega al cliente. Reenvia tu respuesta como mensaje nuevo.")

    async def button(self, bot, group, user, message_id, action, case_id, thread=None):
        route = self.store.private_route(case_id)
        if route:
            self.store.authorize_private(bot, group, user)
            case = self.store.case(case_id)
            if (group != route["operator_chat"] or user != case["operator_id"] or
                    thread != route["operator_topic"]):
                raise PermissionError("Este tema o caso no pertenece a tu cuenta")
        else:
            if self.store.mode() == "private_topics":
                raise PermissionError("El caso no pertenece al modo privado")
            self.store.authorize(bot, group, user)
        async with self.case_lock(case_id):
            return await self._button(bot, group, user, message_id, action, case_id)

    async def _button(self, bot, group, user, message_id, action, case_id):
        case = self.store.case(case_id)
        if case["bot_id"] != bot or case["anchor"] != message_id:
            raise PermissionError("Boton no asociado a este mensaje de caso")
        if action == "take":
            changed = self.store.claim(case_id, user)
            if changed:
                try:
                    await self.send(case["chat_id"], "Una persona del equipo de Corelab ha tomado tu caso.",
                                    thread=case["thread_id"])
                except Exception:
                    return "Caso tomado. No se confirmo el aviso al cliente; puedes responderle."
            return "Caso tomado. Responde sobre los mensajes del caso."
        if action == "close":
            if case["state"] == "closed" and case["operator_id"] == user:
                return "Este caso ya esta cerrado."
            self.store.close(case_id, user)
            try:
                await self.send(case["chat_id"], "La atención humana ha finalizado. "
                    "Puedes seguir consultando información de Corelab con el asistente.", thread=case["thread_id"])
            except BaseException:
                self.store.close_failed(case_id)
                raise
            self.store.closed(case_id)
            route = self.store.private_route(case_id)
            if route:
                try:
                    await self.send(route["operator_chat"], "Caso cerrado. El cliente vuelve al asistente automático.",
                                    thread=route["operator_topic"])
                    await asyncio.wait_for(self.bot.edit_forum_topic(chat_id=route["operator_chat"],
                        message_thread_id=route["operator_topic"], name="Cerrado · Caso " + case_id,
                        connect_timeout=5, read_timeout=8,
                        write_timeout=5, pool_timeout=5), timeout=12)
                except Exception:
                    logger.warning("Case closed; could not confirm operator topic label")
            return "Caso cerrado. El asistente puede responder nuevamente a este cliente."
        raise ValueError("Accion desconocida")
