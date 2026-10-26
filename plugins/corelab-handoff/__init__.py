"""Corelab human handoff. One restricted tool and native Telegram callbacks."""
import asyncio
import logging
import os
from pathlib import Path
from .core import Runtime

plugin_name = "corelab-handoff"
plugin_version = "2.0.1"
logger = logging.getLogger("corelab_handoff")
_runtimes = {}


def register(ctx):
    if not hasattr(ctx, "register_platform_handler") and not hasattr(ctx, "register_telegram_handler"):
        raise RuntimeError("This Hermes version lacks the native platform-handler API")
    home = Path(os.environ["HERMES_HOME"]).resolve()
    # Repeated registration of this loaded plugin shares its native connection.
    # Restart after installing/updating; do not modify Hermes' module registry.
    runtime = _runtimes.get(str(home))
    if runtime is None:
        runtime = Runtime(home)
        _runtimes[str(home)] = runtime

    def wire(application, adapter):
        from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler,
                                  CommandHandler, MessageHandler, filters)
        runtime.bind(application)

        class OperatorGroup(filters.MessageFilter):
            def filter(self, message):
                if message.chat.type not in {"group", "supergroup"}:
                    return False
                try:
                    config = runtime.store.config()
                    return (message.chat.id == config.get("group_id") and
                            runtime.bot.id == config.get("bot_id"))
                except Exception:
                    logger.exception("Cannot inspect operator routing; group held")
                    return True

        class PausedCustomer(filters.MessageFilter):
            def filter(self, message):
                if message.chat.type != "private":
                    return False
                try:
                    return runtime.customer_case(message) is not None
                except Exception:
                    # Database errors must not wake an automated reply during human attention.
                    logger.exception("Cannot read handoff routing; private message held")
                    return True

        class OperatorPrivate(filters.MessageFilter):
            def filter(self, message):
                if message.chat.type != "private" or not message.from_user or message.from_user.is_bot:
                    return False
                try:
                    return (runtime.store.mode() == "private_topics" and runtime.store.is_operator_chat(
                        runtime.bot.id, message.chat.id, message.from_user.id))
                except Exception:
                    logger.exception("Cannot inspect private operator routing; private message held")
                    return True

        async def enroll_private(update, context):
            message = update.effective_message
            try:
                if len(context.args) != 1:
                    raise ValueError("Se necesita el codigo temporal de configurar.cmd --privado")
                await runtime.register_private(message, context.args[0])
                await message.reply_text("Asesor privado verificado. Los casos se recibiran en temas separados "
                    "cuando el administrador active el modo privado.", parse_mode=None)
            except Exception as exc:
                await message.reply_text(str(exc), parse_mode=None)
            finally:
                raise ApplicationHandlerStop

        async def own_id(update, context):
            message, user = update.effective_message, update.effective_user
            try:
                if user and not user.is_bot and not message.sender_chat and message.chat.id == user.id:
                    await message.reply_text("Tu ID de Telegram para registrar la atención: " + str(user.id),
                                             parse_mode=None)
            finally:
                raise ApplicationHandlerStop

        async def private_operator(update, context):
            message = update.effective_message
            try:
                runtime.store.authorize_private(runtime.bot.id, message.chat.id, update.effective_user.id)
                command = (message.text or "").split(" ")[0].split("@")[0]
                if command == "/atencion_casos":
                    cases = [c for c in runtime.store.open_cases() if c["operator_id"] == update.effective_user.id
                             and runtime.store.private_route(c["id"])]
                    lines = [f"Caso {c['id']} · {c['state']}" for c in cases]
                    await message.reply_text("Tus casos abiertos:\n" + "\n".join(lines[:40])
                        if lines else "No tienes casos abiertos.", parse_mode=None)
                elif command.startswith("/"):
                    await message.reply_text("Estás en el canal de asesores. Abre el tema de un caso y toma "
                        "el caso antes de responder. /atencion_casos muestra tus casos.", parse_mode=None)
                elif any(getattr(message, attr, None) for attr in
                         ("forum_topic_created", "forum_topic_closed", "forum_topic_reopened", "forum_topic_edited")):
                    pass
                else:
                    await runtime.operator_reply(message)
            except Exception as exc:
                await message.reply_text(str(exc), parse_mode=None)
            finally:
                raise ApplicationHandlerStop

        async def enroll(update, context):
            message = update.effective_message
            try:
                runtime.bind(application)
                user, chat = update.effective_user, update.effective_chat
                if (not user or user.is_bot or message.sender_chat or
                        chat.type not in {"group", "supergroup"}):
                    raise PermissionError("Usa este codigo desde tu cuenta en el grupo privado de atencion")
                command = message.text.split()[0].split("@")[0]
                kind = "bind" if command == "/atencion_vincular" else "invite"
                if len(context.args) != 1:
                    raise ValueError("Se necesita el codigo temporal generado en configurar.cmd")
                info = await application.bot.get_chat(chat.id)
                if getattr(info, "username", None) or getattr(info, "active_usernames", None):
                    raise PermissionError("El grupo de atencion debe ser privado, sin nombre de usuario publico")
                member = await application.bot.get_chat_member(chat.id, user.id)
                if kind == "bind" and member.status not in {"administrator", "creator", "owner"}:
                    raise PermissionError("La primera vinculacion debe realizarla un administrador del grupo")
                if member.status in {"left", "kicked", "banned"}:
                    raise PermissionError("El operador debe pertenecer al grupo")
                runtime.store.consume_enrollment(context.args[0], kind, chat.id, runtime.bot.id, user.id,
                                                  chat.title or "Atencion", user.full_name)
                await message.reply_text("Grupo de atencion vinculado y operador autorizado."
                    if kind == "bind" else "Operador autorizado para atender casos.", parse_mode=None)
            except Exception as exc:
                await message.reply_text(str(exc), parse_mode=None)
            finally:
                raise ApplicationHandlerStop

        async def paused(update, context):
            try:
                if not await runtime.customer_message(update.effective_message):
                    return  # Case closed before dispatch; ordinary Hermes handling may continue.
            except Exception:
                logger.exception("Human-mode message not delivered; automatic agent stays paused")
                try:
                    await update.effective_message.reply_text(
                        "No pude procesar tu mensaje para el personal. Intenta enviarlo nuevamente.", parse_mode=None)
                except Exception:
                    pass
            raise ApplicationHandlerStop

        async def operator(update, context):
            message = update.effective_message
            try:
                if runtime.store.mode() == "private_topics":
                    if (message.text or "").startswith("/atencion_"):
                        await message.reply_text("La atención está en modo privado. Usa tu chat privado con el bot.",
                                                 parse_mode=None)
                    return
                if message.text and message.text.split()[0].split("@")[0] == "/atencion_casos":
                    runtime.store.authorize(runtime.bot.id, message.chat.id, update.effective_user.id)
                    cases = runtime.store.open_cases()
                    lines = [f"{case['id']} · {case['state']}" for case in cases]
                    await message.reply_text("Casos abiertos:\n" + "\n".join(lines[:40])
                                             if lines else "No hay casos abiertos.", parse_mode=None)
                else:
                    await runtime.operator_reply(message)
            except Exception as exc:
                if message.reply_to_message or (message.text or "").startswith("/atencion_"):
                    await message.reply_text(str(exc), parse_mode=None)
            finally:
                # Never ask the conversational model to read the internal operator group.
                raise ApplicationHandlerStop

        async def button(update, context):
            query = update.callback_query
            try:
                user, message = query.from_user, query.message
                if not user or user.is_bot or not message or not message.from_user or message.from_user.id != runtime.bot.id:
                    raise PermissionError("Boton no autorizado")
                if message.chat.type == "private":
                    runtime.store.authorize_private(runtime.bot.id, message.chat.id, user.id)
                else:
                    runtime.store.authorize(runtime.bot.id, message.chat.id, user.id)
                _, action, case_id = query.data.split(":", 2)
                await query.answer("Procesando")
                result = await runtime.button(runtime.bot.id, message.chat.id, user.id,
                    message.message_id, action, case_id, thread=getattr(message, "message_thread_id", None))
                route = runtime.store.private_route(case_id)
                if not (route and action == "close"):
                    sent = await runtime.send(message.chat.id, result, reply=message.message_id,
                        thread=route["operator_topic"] if route else None)
                    runtime.store.route(runtime.bot.id, message.chat.id, sent.message_id, case_id)
            except Exception as exc:
                try:
                    await query.answer(str(exc)[:180], show_alert=True)
                except Exception:
                    pass
            finally:
                raise ApplicationHandlerStop

        # First-match handlers are narrowly scoped. Paused-customer handling includes
        # /new and /reset: a customer cannot bypass an active human assignment.
        application.add_handler(CommandHandler(["atencion_vincular", "atencion_alta"], enroll,
                                                filters=filters.ChatType.GROUPS, block=True), group=-90)
        application.add_handler(CommandHandler("atencion_privada", enroll_private,
                                                filters=filters.ChatType.PRIVATE, block=True), group=-90)
        application.add_handler(CommandHandler("atencion_mi_id", own_id,
                                                filters=filters.ChatType.PRIVATE, block=True), group=-90)
        application.add_handler(CallbackQueryHandler(button, pattern=r"^clh:(take|close):[a-f0-9]{16}$", block=True), group=-90)
        application.add_handler(MessageHandler(OperatorGroup(), operator, block=True), group=-90)
        application.add_handler(MessageHandler(OperatorPrivate(), private_operator, block=True), group=-90)
        application.add_handler(MessageHandler(PausedCustomer(), paused, block=True), group=-90)

    if hasattr(ctx, "register_platform_handler"):
        ctx.register_platform_handler("telegram", wire)
    else:
        ctx.register_telegram_handler(wire)
    ctx.register_tool(name="derivar_a_humano", toolset="corelab_handoff", handler=runtime.tool,
        schema={"name": "derivar_a_humano", "description":
            "Solicita atencion humana para el cliente actual de Corelab en Telegram. "
            "Usa cuando el cliente pide una persona o acepta la derivacion. "
            "El destino y la identidad los fija el servidor. No garantiza respuesta inmediata.",
            "parameters": {"type": "object", "additionalProperties": False,
                "properties": {
                    "reason": {"type": "string", "enum": ["customer_request", "knowledge_missing",
                                                             "staff_confirmation", "clinical_question"]},
                    "summary": {"type": "string", "maxLength": 600,
                                "description": "Breve motivo de atencion, sin historial completo ni detalles clinicos sensibles"}},
                "required": ["reason", "summary"]}})
