"""Observe Telegram input before handoff. Persist indicators, never message text."""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import sqlite3
import time

from .common import directory, readonly, columns, settings, write_json
from .taxonomy import classify


def staff(home, configured=()):
    users = {str(x) for x in configured}
    path = Path(home) / "plugin-data/corelab-handoff/handoff.db"
    if path.is_file():
        with readonly(path) as conn:
            if columns(conn, "private_operators"):
                users.update(str(row[0]) for row in conn.execute("SELECT user_id FROM private_operators"))
            if columns(conn, "settings"):
                row = conn.execute("SELECT value FROM settings WHERE key='routing'").fetchone()
                if row:
                    users.update(json.loads(row[0]).get("operators", {}).keys())
    return users


class Events:
    def __init__(self, home):
        self.home = Path(home)
        self.path = directory(home) / "crm.db"

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=0.5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS events(
            bot INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL,
            user INTEGER NOT NULL, timestamp REAL NOT NULL, thread INTEGER,
            kind TEXT NOT NULL, topics TEXT NOT NULL, signals TEXT NOT NULL,
            PRIMARY KEY(bot,chat,message));
          CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
        """)
        return conn

    def record(self, bot, message):
        cfg = settings(self.home)
        if int(bot) != int(cfg["bot_id"]):
            raise ValueError("Bot distinto al configurado para Corelab CRM")
        user = message.from_user
        chat = message.chat
        if not user or user.is_bot or chat.type != "private" or int(chat.id) != int(user.id):
            return False
        if str(user.id) in staff(self.home, cfg["excluded_users"]):
            return False
        if getattr(message, "text", "") and message.text.lstrip().startswith("/"):
            return False
        text = message.text or message.caption or ""
        result = classify(text)
        kind = "text" if message.text else "media"
        conn = self.connect()
        try:
            with conn:
                cursor = conn.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)", (
                    int(bot), int(chat.id), int(message.message_id), int(user.id),
                    message.date.timestamp(), message.message_thread_id, kind,
                    json.dumps(result["topics"]), json.dumps(result["signals"])))
                inserted = cursor.rowcount == 1
        finally:
            conn.close()
        return inserted


def register_platform(ctx, home):
    def wire(application, adapter):
        from telegram.ext import MessageHandler, filters
        events = Events(home)
        async def observe(update, context):
            if not update.message:
                return
            try:
                # Fast local transaction; no network, model, reply or propagation stop.
                await asyncio.to_thread(events.record, context.bot.id, update.message)
            except Exception as exc:
                write_json(Path(home) / "logs/corelab-crm-collection-error.json",
                    {"time": time.time(), "error_type": type(exc).__name__,
                     "message": "No se pudo registrar un indicador; revisar cobertura"})
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,
                                               observe, block=True), group=-120)
        async def ready():
            from .process import fingerprint
            from .common import directory
            import os
            try:
                me = await application.bot.get_me()
                cfg = settings(home)
                if int(cfg["bot_id"]) != int(me.id):
                    raise ValueError("El bot del perfil no coincide con Corelab CRM")
                if not cfg.get("collection_started"):
                    cfg["collection_started"] = time.time()
                    write_json(directory(home) / "config.json", cfg)
                write_json(Path(home) / "logs/corelab-crm-collector.json",
                    {"stage": "ready", "pid": os.getpid(), "bot_id": me.id,
                     "process_fingerprint": fingerprint(os.getpid()),
                     "at": datetime.now(timezone.utc).isoformat(), "stores_message_text": False})
            except Exception as exc:
                write_json(Path(home) / "logs/corelab-crm-collector.json",
                    {"stage": "error", "error_type": type(exc).__name__})
        ctx.spawn_task(ready(), name="corelab-crm:startup")
    ctx.register_platform_handler("telegram", wire)
