"""Durable routing and operator permissions; independent of the RAG database."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

ACTIVE = ("notifying", "waiting", "active", "closing")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases(
                    id TEXT PRIMARY KEY, bot_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    thread_id INTEGER, session_id TEXT NOT NULL,
                    reason TEXT NOT NULL, summary TEXT NOT NULL,
                    state TEXT NOT NULL, operator_id INTEGER, anchor INTEGER,
                    created REAL NOT NULL, closed REAL, process_id INTEGER NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_customer
                ON cases(bot_id,chat_id,user_id)
                WHERE state IN ('notifying','waiting','active','closing');
                CREATE TABLE IF NOT EXISTS routes(
                    bot_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, case_id TEXT NOT NULL,
                    PRIMARY KEY(bot_id,group_id,message_id));
                CREATE TABLE IF NOT EXISTS deliveries(
                    key TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS private_operators(
                    user_id INTEGER PRIMARY KEY, bot_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    verified REAL NOT NULL, last_assigned REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS private_cases(
                    case_id TEXT PRIMARY KEY, operator_chat INTEGER NOT NULL,
                    operator_topic INTEGER,
                    UNIQUE(operator_chat,operator_topic));
            ''')

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def read(conn, key, default=None):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(conn, key, value):
        conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def config(self):
        with self.connection() as conn:
            return self.read(conn, "routing", {})

    def enrollment(self, kind):
        if kind not in {"bind", "invite"}:
            raise ValueError("Invalid enrollment type")
        with self.connection(write=True) as conn:
            routing = self.read(conn, "routing", {})
            if kind == "bind" and routing:
                raise ValueError("A group is already linked. Keep the current routing; see LEEME.txt.")
            if kind == "invite" and not routing:
                raise ValueError("Link the private operator group first")
            token = secrets.token_urlsafe(24)
            self.put(conn, "enrollment", {"kind": kind, "digest": hashlib.sha256(token.encode()).hexdigest(),
                                           "expires": time.time() + 600})
        return token

    def consume_enrollment(self, token, kind, group, bot, user, group_title, user_name):
        """Caller verifies real Telegram chat privacy, membership and administrator role."""
        with self.connection(write=True) as conn:
            pending = self.read(conn, "enrollment", {})
            supplied = hashlib.sha256(token.encode()).hexdigest()
            if (pending.get("kind") != kind or pending.get("expires", 0) < time.time() or
                    not hmac.compare_digest(pending.get("digest", ""), supplied)):
                raise PermissionError("Codigo invalido, utilizado o vencido")
            routing = self.read(conn, "routing", {})
            if kind == "bind":
                if routing:
                    raise PermissionError("Ya existe un grupo vinculado")
                routing = {"group_id": int(group), "bot_id": int(bot), "group_title": group_title,
                           "operators": {str(user): user_name}}
            else:
                if routing.get("group_id") != int(group) or routing.get("bot_id") != int(bot):
                    raise PermissionError("La invitacion solo sirve en el grupo configurado")
                routing["operators"][str(user)] = user_name
            self.put(conn, "routing", routing)
            conn.execute("DELETE FROM settings WHERE key='enrollment'")
        return routing

    def authorize(self, bot, group, user):
        routing = self.config()
        if (routing.get("bot_id") != int(bot) or routing.get("group_id") != int(group) or
                str(user) not in routing.get("operators", {})):
            raise PermissionError("Operador o grupo no autorizado")
        return routing

    def active(self, bot, chat, user):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cases WHERE bot_id=? AND chat_id=? AND user_id=? "
                               "AND state IN ('notifying','waiting','active','closing')",
                               (bot, chat, user)).fetchone()
            return dict(row) if row else None

    def case(self, case_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise ValueError("Caso no encontrado")
            return dict(row)

    def create(self, identity, bot, reason, summary, private=False):
        with self.connection(write=True) as conn:
            current_mode = self.read(conn, "handoff_mode", "group")
            if current_mode not in {"group", "private_topics"} or private != (current_mode == "private_topics"):
                raise RuntimeError("El modo de atencion cambio o es invalido; vuelve a solicitar el caso")
            row = conn.execute("SELECT * FROM cases WHERE bot_id=? AND chat_id=? AND user_id=? "
                               "AND state IN ('notifying','waiting','active','closing')",
                               (bot, identity["chat_id"], identity["user_id"])).fetchone()
            if row:
                return dict(row), False
            operator = None
            if private:
                routing = self.read(conn, "routing", {})
                if routing.get("bot_id") != int(bot):
                    raise PermissionError("Bot no autorizado")
                candidates = conn.execute("SELECT p.*, (SELECT COUNT(*) FROM cases c WHERE "
                    "c.operator_id=p.user_id AND c.state IN ('notifying','waiting','active','closing')) AS load "
                    "FROM private_operators p WHERE p.bot_id=? AND p.enabled=1 "
                    "ORDER BY load,p.last_assigned,p.user_id", (bot,)).fetchall()
                operator = next((p for p in candidates if str(p["user_id"]) in routing.get("operators", {})
                                 and p["user_id"] != identity["user_id"]), None)
                if operator is None:
                    raise RuntimeError("No hay otro asesor privado habilitado para recibir el caso")
            case_id = secrets.token_hex(8)
            conn.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                case_id, bot, identity["chat_id"], identity["user_id"], identity.get("thread_id"),
                identity.get("session_id", ""), reason, summary, "notifying",
                operator["user_id"] if operator else None, None, time.time(), None,
                os.getpid()))
            if operator:
                conn.execute("INSERT INTO private_cases VALUES(?,?,NULL)", (case_id, operator["chat_id"]))
                conn.execute("UPDATE private_operators SET last_assigned=? WHERE user_id=?",
                             (time.time(), operator["user_id"]))
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            return dict(row), True

    def notified(self, case_id, anchor):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE cases SET anchor=?,state='waiting' WHERE id=? AND state='notifying'",
                         (anchor, case_id))

    def notification_failed(self, case_id):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE cases SET state='failed',closed=? WHERE id=? AND state='notifying'",
                         (time.time(), case_id))

    def route(self, bot, group, message, case_id):
        with self.connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO routes VALUES(?,?,?,?)", (bot, group, message, case_id))

    def lookup(self, bot, group, message):
        with self.connection() as conn:
            row = conn.execute("SELECT case_id FROM routes WHERE bot_id=? AND group_id=? AND message_id=?",
                               (bot, group, message)).fetchone()
            return self.case(row[0]) if row else None

    def assigned(self, case_id, user):
        case = self.case(case_id)
        if case["state"] == "closed":
            raise PermissionError("Este caso ya esta cerrado; el mensaje no se envio al cliente")
        if case["state"] != "active" or case["operator_id"] != int(user):
            raise PermissionError("Debes tomar este caso antes de responder; otro operador no puede intervenir")
        return case

    def claim(self, case_id, user):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row or row["state"] not in {"waiting", "active"}:
                raise PermissionError("El caso no esta disponible para tomar")
            if row["operator_id"] not in {None, int(user)}:
                raise PermissionError("Otro operador ya tomo este caso")
            changed = row["state"] == "waiting"
            conn.execute("UPDATE cases SET state='active',operator_id=? WHERE id=?", (user, case_id))
        return changed

    def close(self, case_id, user):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row or row["operator_id"] != int(user) or row["state"] not in {"active", "closing"}:
                raise PermissionError("Solo el operador que tomo el caso puede cerrarlo")
            conn.execute("UPDATE cases SET state='closing' WHERE id=?", (case_id,))

    def closed(self, case_id):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE cases SET state='closed',closed=? WHERE id=? AND state='closing'",
                         (time.time(), case_id))

    def close_failed(self, case_id):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE cases SET state='active' WHERE id=? AND state='closing'", (case_id,))

    def reserve_delivery(self, key, case_id):
        with self.connection(write=True) as conn:
            return conn.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,'pending',NULL)",
                                (key, case_id)).rowcount == 1

    def delivery(self, key, success, error=None):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE deliveries SET state=?,error=? WHERE key=?",
                         ("sent" if success else "failed", error, key))

    def open_cases(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM cases WHERE state IN "
                    "('notifying','waiting','active','closing') ORDER BY created")]

    def recover_interrupted(self):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE cases SET state='failed',closed=? WHERE state='notifying' AND process_id!=?",
                         (time.time(), os.getpid()))
            conn.execute("UPDATE cases SET state='active' WHERE state='closing'")

    def mode(self):
        with self.connection() as conn:
            mode = self.read(conn, "handoff_mode", "group")
            if mode not in {"group", "private_topics"}:
                raise RuntimeError("Modo de atencion desconocido; no se enviara informacion al grupo")
            return mode

    def private_channels(self):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM private_operators ORDER BY user_id")]

    def private_enrollment(self, user):
        user = int(user)
        with self.connection(write=True) as conn:
            routing = self.read(conn, "routing", {})
            if not routing or user <= 0 or user == routing.get("bot_id"):
                raise ValueError("Se necesita un grupo vinculado y el ID positivo de la cuenta humana")
            if conn.execute("SELECT 1 FROM cases WHERE chat_id=? AND state IN "
                            "('notifying','waiting','active','closing')", (user,)).fetchone():
                raise RuntimeError("Esta cuenta tiene un caso como cliente. Cierralo antes de registrarla como asesor")
            token = secrets.token_urlsafe(24)
            self.put(conn, "private_enrollment_" + str(user), {"bot": routing["bot_id"], "user": user,
                "digest": hashlib.sha256(token.encode()).hexdigest(), "expires": time.time() + 600})
        return token

    def check_private_token(self, token, bot, user, conn=None):
        if conn is None:
            with self.connection() as c:
                return self.check_private_token(token, bot, user, c)
        pending = self.read(conn, "private_enrollment_" + str(user), {})
        if (pending.get("bot") != int(bot) or pending.get("user") != int(user) or
            pending.get("expires", 0) < time.time() or not hmac.compare_digest(
                pending.get("digest", ""), hashlib.sha256(token.encode()).hexdigest())):
            raise PermissionError("Codigo privado invalido, utilizado, vencido o destinado a otra cuenta")

    def consume_private(self, token, bot, user, chat, name):
        with self.connection(write=True) as conn:
            self.check_private_token(token, bot, user, conn)
            if int(user) != int(chat) or int(user) <= 0:
                raise PermissionError("Se requiere el chat privado de la cuenta autorizada")
            routing = self.read(conn, "routing", {})
            if routing.get("bot_id") != int(bot):
                raise PermissionError("Bot no autorizado")
            routing["operators"][str(user)] = name
            self.put(conn, "routing", routing)
            conn.execute("INSERT INTO private_operators(user_id,bot_id,chat_id,verified) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET bot_id=excluded.bot_id,chat_id=excluded.chat_id,"
                "verified=excluded.verified", (user, bot, chat, time.time()))
            conn.execute("DELETE FROM settings WHERE key=?", ("private_enrollment_" + str(user),))

    def authorize_private(self, bot, chat, user):
        routing = self.config()
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM private_operators WHERE user_id=? AND bot_id=? AND chat_id=?",
                               (user, bot, chat)).fetchone()
        if (int(chat) != int(user) or not row or routing.get("bot_id") != int(bot) or
                str(user) not in routing.get("operators", {})):
            raise PermissionError("Chat privado de asesor no autorizado")
        return dict(row)

    def is_operator_chat(self, bot, chat, user):
        routing = self.config()
        if routing.get("bot_id") != int(bot) or int(chat) != int(user):
            return False
        # Also hold whitelisted accounts not yet enrolled: never process staff messages with the LLM.
        return str(user) in routing.get("operators", {})

    def set_mode(self, mode):
        if mode not in {"group", "private_topics"}:
            raise ValueError("Modo no valido")
        with self.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM cases WHERE state IN ('notifying','waiting','active','closing')").fetchone():
                raise RuntimeError("Cierra todos los casos humanos antes de cambiar el modo")
            routing = self.read(conn, "routing", {})
            if mode == "private_topics" and not conn.execute(
                    "SELECT 1 FROM private_operators WHERE bot_id=? AND enabled=1",
                    (routing.get("bot_id", 0),)).fetchone():
                raise RuntimeError("Primero verifica al menos un asesor con /atencion_privada en su chat privado")
            self.put(conn, "handoff_mode", mode)

    def set_operator_enabled(self, user, enabled):
        with self.connection(write=True) as conn:
            if conn.execute("UPDATE private_operators SET enabled=? WHERE user_id=?",
                            (int(enabled), int(user))).rowcount != 1:
                raise ValueError("Asesor privado no registrado")

    def private_route(self, case_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM private_cases WHERE case_id=?", (case_id,)).fetchone()
            return dict(row) if row else None

    def set_topic(self, case_id, chat, topic):
        with self.connection(write=True) as conn:
            if int(topic) <= 0 or conn.execute("UPDATE private_cases SET operator_topic=? "
                    "WHERE case_id=? AND operator_chat=? AND operator_topic IS NULL",
                    (topic, case_id, chat)).rowcount != 1:
                raise PermissionError("Tema no asociado al asesor del caso")

    def topic_case(self, bot, chat, topic):
        if not topic:
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT c.* FROM cases c JOIN private_cases p ON c.id=p.case_id "
                "WHERE c.bot_id=? AND p.operator_chat=? AND p.operator_topic=?", (bot, chat, topic)).fetchone()
            return dict(row) if row else None
