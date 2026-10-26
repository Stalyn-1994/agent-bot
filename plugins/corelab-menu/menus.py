"""Reconcile Telegram's visible command lists using the already connected native bot.

No second poller, no subprocess, no new tool or message handler that consumes updates.
Menu visibility never changes command authorization. Adapter access is read-only.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from .common import atomic_json, fingerprint, read_json

logger = logging.getLogger("corelab_menu")
BROAD = ("default", "all_private_chats", "all_group_chats", "all_chat_administrators")
OPERATOR_COMMANDS = [{"command": "atencion_casos", "description": "Ver mis casos de atencion"}]


def scope_object(value):
    from telegram import BotCommandScope
    return BotCommandScope.de_json(value, None)


def command_objects(rows):
    from telegram import BotCommand
    return [BotCommand.de_json(row, None) for row in rows]


def key_for(scope, language):
    return json.dumps([scope, language], sort_keys=True, separators=(",", ":"))


def positive_ids(values):
    if not isinstance(values, list):
        raise ValueError("admin_menu_user_ids debe ser una lista de IDs numericos")
    result = set()
    for value in values:
        if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
            raise ValueError("ID de administrador invalido para el menu")
        result.add(int(value))
    return result


def handoff_roles(home, bot_id):
    """Read only the routing table, never cases, histories, RAG or patient information."""
    path = Path(home) / "plugin-data" / "corelab-handoff" / "handoff.db"
    if not path.is_file():
        return set(), None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        settings = dict(conn.execute("SELECT key,value FROM settings")) if "settings" in tables else {}
        routing = json.loads(settings.get("routing", "{}"))
        mode = json.loads(settings.get("handoff_mode", '"group"'))
        operators = set()
        # Paused operators still need to handle cases already assigned to them.
        if mode == "private_topics" and "private_operators" in tables:
            operators = {int(r[0]) for r in conn.execute(
                "SELECT chat_id FROM private_operators WHERE bot_id=? AND verified>0", (bot_id,))}
        group = None
        if mode == "group" and routing.get("bot_id") == bot_id:
            group = routing.get("group_id")
        return operators, group


class Menus:
    def __init__(self, home, application, adapter):
        self.home, self.app, self.adapter = Path(home), application, adapter
        self.bot = application.bot
        self.directory = self.home / "plugin-data" / "corelab-menu"
        self.control = self.directory / "control.json"
        self.status_path = self.home / "logs" / "corelab-menu.json"
        self.state = None
        self.state_path = None
        self.pending = {}
        self.seen = {}
        self.wakeup = asyncio.Event()
        self.languages = {"", "es", "en"}
        self.admins, self.operators, self.group = set(), set(), None
        self.last_global = 0.0
        self.mode = None
        self.stopped = False
        self.restored = False

    async def observe(self, update, _context):
        """Queue only numeric routing metadata. Ordinary dispatch continues immediately."""
        if self.stopped or self.mode == "restore":
            return
        user, chat = update.effective_user, update.effective_chat
        if not user or user.is_bot or not chat:
            return
        if chat.type not in {"private", "group", "supergroup"}:
            return
        if chat.type == "private" and chat.id != user.id:
            return
        language = str(getattr(user, "language_code", "") or "").split("-")[0].lower()
        if re.fullmatch(r"[a-z]{2}", language) and language not in self.languages:
            self.languages.add(language)
            self.last_global = 0.0
        observed = (int(chat.id), chat.type)
        if time.monotonic() - self.seen.get(observed, -1e20) < 3600:
            return
        if len(self.pending) >= 2000:
            return
        self.pending[observed] = None
        self.wakeup.set()

    def status(self, stage, **extra):
        try:
            bot_id = self.bot.id
        except RuntimeError:
            bot_id = None  # Native Application has not completed initialize() yet.
        atomic_json(self.status_path, {"stage": stage, "pid": os.getpid(),
            "process_fingerprint": fingerprint(os.getpid()), "bot_id": bot_id,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "admin_menus": len(self.admins), "operator_menus": len(self.operators),
            "languages": sorted(self.languages), **extra})

    def save(self):
        atomic_json(self.state_path, self.state)

    async def get(self, scope, language):
        rows = await self.bot.get_my_commands(scope=scope_object(scope), language_code=language,
            read_timeout=8, write_timeout=8, connect_timeout=8, pool_timeout=3)
        return [row.to_dict() for row in rows]

    async def publish(self, scope, language, desired):
        kwargs = dict(scope=scope_object(scope), language_code=language,
            read_timeout=8, write_timeout=8, connect_timeout=8, pool_timeout=3)
        if desired:
            await self.bot.set_my_commands(command_objects(desired), **kwargs)
        else:
            # An empty scope falls back. Remove ALL broad scopes too, not just private.
            await self.bot.delete_my_commands(**kwargs)

    async def ensure(self, scope, language, desired):
        current = await self.get(scope, language)
        key = key_for(scope, language)
        entry = self.state["scopes"].get(key)
        if entry is None:
            entry = {"scope": scope, "language": language, "before": current, "expected": desired}
            self.state["scopes"][key] = entry
            self.save()  # Write-ahead backup before the first remote mutation.
        if entry["expected"] != desired:
            entry["expected"] = desired
            self.save()
        if current != desired:
            await self.publish(scope, language, desired)
            if await self.get(scope, language) != desired:
                raise RuntimeError("Telegram no confirmo el menu solicitado")

    async def capture_admin_source(self):
        # Capture the real Hermes menu after its native publication, including locale variants.
        sources = self.state.setdefault("admin_source", {})
        changed = False
        for language in sorted(self.languages):
            for kind in ("all_private_chats", "default"):
                current = await self.get({"type": kind}, language)
                if current:
                    if sources.get(language) != current:
                        sources[language] = current
                        changed = True
                    break
        if changed:
            self.save()

    def desired_chat(self, chat_id):
        if chat_id in self.admins:
            return "admin"
        if chat_id in self.operators or chat_id == self.group:
            return "operator"
        return "customer"

    async def chat(self, chat_id, chat_type="private"):
        role = self.desired_chat(chat_id)
        sources = self.state.get("admin_source", {})
        for language in sorted(self.languages):
            desired = (sources.get(language, sources.get("", [])) if role == "admin" else
                       OPERATOR_COMMANDS if role == "operator" else [])
            await self.ensure({"type": "chat", "chat_id": chat_id}, language, desired)
            if chat_type != "private":
                await self.ensure({"type": "chat_administrators", "chat_id": chat_id}, language, [])

    async def hide(self, settings, *, force=False):
        admins = positive_ids(settings.get("admin_menu_user_ids", []))
        operators, group = await asyncio.to_thread(handoff_roles, self.home, self.bot.id)
        roles = (admins, operators, group)
        changed = roles != (self.admins, self.operators, self.group)
        removed = (self.admins | self.operators | ({self.group} if self.group else set())) - (
            admins | operators | ({group} if group else set()))
        self.admins, self.operators, self.group = roles
        self.languages.update(settings.get("languages", ["es", "en"]))
        if any(not re.fullmatch(r"[a-z]{2}", lang) for lang in self.languages if lang):
            raise ValueError("Codigo de idioma invalido")
        if force or changed or time.monotonic() - self.last_global >= 60:
            await self.capture_admin_source()
            if self.admins and not self.state.get("admin_source", {}).get(""):
                raise RuntimeError("Hermes aun no publico un menu que pueda conservarse para administradores")
            for language in sorted(self.languages):
                for kind in BROAD:
                    await self.ensure({"type": kind}, language, [])
            # Every previously managed chat is cleared again at startup. This also removes
            # stale staff overrides after an operator or admin is removed locally.
            previous_chats = {entry["scope"]["chat_id"] for entry in self.state["scopes"].values()
                              if entry["scope"]["type"] == "chat"}
            targets = previous_chats if force else set()
            targets |= self.admins | self.operators | removed | ({group} if group else set())
            for chat_id in sorted(targets):
                await self.chat(chat_id, "private" if chat_id > 0 else "group")
            self.last_global = time.monotonic()
        queued, self.pending = self.pending, {}
        for chat_id, chat_type in queued:
            await self.chat(chat_id, chat_type)
            self.seen[(chat_id, chat_type)] = time.monotonic()
        if len(self.seen) > 2000:
            self.seen = dict(sorted(self.seen.items(), key=lambda item: item[1])[-1000:])
        self.status("ready", mode="hidden", verified=True)

    async def restore(self):
        conflicts = []
        for entry in self.state["scopes"].values():
            scope, language, original = entry["scope"], entry["language"], entry["before"]
            current = await self.get(scope, language)
            if current == original:
                continue
            if current != entry["expected"]:
                conflicts.append({"scope": scope, "language": language})
                continue
            await self.publish(scope, language, original)
            if await self.get(scope, language) != original:
                raise RuntimeError("Telegram no confirmo la restauracion del menu")
        if conflicts:
            self.status("restore_conflict", conflicts=conflicts)
        else:
            self.status("restored", verified=True)
            self.restored = True

    async def run(self):
        # This coroutine belongs to ctx.spawn_task: native plugin unload cancels it.
        self.status("starting")
        deadline = time.monotonic() + 180
        while not self.app.running or not getattr(self.adapter, "is_connected", False):
            if time.monotonic() >= deadline:
                return  # Abandoned/rebuilt Application; no remote changes.
            await asyncio.sleep(0.25)
        self.state_path = self.directory / f"menus-before-{self.bot.id}.json"
        try:
            self.state = read_json(self.state_path, {"version": 1, "bot_id": self.bot.id, "scopes": {}})
            if self.state.get("bot_id") != self.bot.id or self.state.get("version") != 1:
                raise ValueError("Respaldo incompatible")
        except Exception:
            self.status("error", error="Respaldo de menu incompatible")
            return
        self.status("starting")
        # Read-only lifecycle observation: don't race the native setMyCommands startup task.
        # Verified against the user's Hermes commit 9a0a1625 and current upstream.
        await asyncio.sleep(1)
        while self.app.running:
            task = getattr(self.adapter, "_post_connect_task", None)
            if task is not None and not task.done():
                await asyncio.sleep(0.25)
                continue
            try:
                settings = read_json(self.control, {})
                mode = settings.get("mode", "hide")
                if mode == "restore":
                    if not self.restored:
                        await self.restore()
                    else:
                        self.status("restored", verified=True)
                elif mode == "hide":
                    self.restored = False
                    await self.hide(settings, force=self.mode != mode)
                else:
                    raise ValueError("Modo de menu no reconocido")
                self.mode = mode
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Telegram transport exception messages may contain request URLs/tokens.
                logger.warning("Menu reconciliation failed (%s)", type(exc).__name__)
                self.status("error", error_type=type(exc).__name__,
                            error="No se pudo comprobar el menu; se reintentara sin bloquear mensajes")
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
        self.stopped = True
