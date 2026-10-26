"""Private appointment ledger, serialized reservations and restart-safe notification queue."""
from contextlib import contextmanager
import sqlite3
import time
import uuid

from .common import MAX_ACTIVE_BOOKINGS, now
from .patient import patient_details
from .privacy import Ledger


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path, *, initialize=True):
        self.path = path
        self.privacy = Ledger(self)
        if not initialize:
            if not path.is_file():raise FileNotFoundError("No se encontro el registro de citas")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bookings(
                  id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,
                  bot INTEGER NOT NULL,user INTEGER NOT NULL,chat INTEGER NOT NULL,thread INTEGER,
                  start TEXT NOT NULL,patients INTEGER NOT NULL CHECK(patients = 1),
                  contact TEXT NOT NULL,cedula TEXT,telefono TEXT,state TEXT NOT NULL,expires REAL NOT NULL,
                  anchor INTEGER,created REAL NOT NULL,submitted REAL,confirmed REAL,
                  notify TEXT NOT NULL DEFAULT 'pending',notify_message INTEGER,
                  customer_notice TEXT NOT NULL DEFAULT 'pending',notify_due REAL DEFAULT 0,
                  customer_due REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS booking_slot ON bookings(start,state);
                CREATE INDEX IF NOT EXISTS booking_owner ON bookings(bot,user,chat);
                CREATE TRIGGER IF NOT EXISTS booking_single_patient_insert
                BEFORE INSERT ON bookings WHEN NEW.patients != 1
                BEGIN SELECT RAISE(ABORT,'Each booking is for one patient'); END;
                CREATE TRIGGER IF NOT EXISTS booking_single_patient_update
                BEFORE UPDATE OF patients ON bookings WHEN NEW.patients != 1
                BEGIN SELECT RAISE(ABORT,'Each booking is for one patient'); END;
                CREATE TABLE IF NOT EXISTS cancellations(
                  token TEXT PRIMARY KEY,booking TEXT NOT NULL,expires REAL NOT NULL,
                  anchor INTEGER,thread INTEGER,state TEXT NOT NULL DEFAULT 'proposed');
                CREATE TABLE IF NOT EXISTS topics(
                  bot INTEGER NOT NULL,advisor INTEGER NOT NULL,topic INTEGER NOT NULL,
                  PRIMARY KEY(bot,advisor));
            ''')
            # Preserve old appointments without inventing patient information.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}
            for field in ("cedula", "telefono", "consent_event_id"):
                if field not in columns:
                    conn.execute("ALTER TABLE bookings ADD COLUMN " + field + " TEXT")
            for operation, suffix in (("INSERT", "insert"), ("UPDATE OF cedula,telefono", "update")):
                conn.executescript("CREATE TRIGGER IF NOT EXISTS booking_patient_data_" + suffix +
                    " BEFORE " + operation + " ON bookings WHEN NEW.cedula IS NULL OR "
                    "length(NEW.cedula)!=10 OR NEW.cedula GLOB '*[^0-9]*' OR "
                    "NEW.telefono IS NULL OR substr(NEW.telefono,1,1)!='+' OR "
                    "length(NEW.telefono) NOT BETWEEN 8 AND 16 OR "
                    "substr(NEW.telefono,2) GLOB '*[^0-9]*' "
                    "BEGIN SELECT RAISE(ABORT,'Patient ID and phone are required'); END;")
            self.privacy.initialize(conn)
            # Enforce the same account-bound evidence at the storage boundary.
            # Legacy rows are preserved with NULL; only new inserts/changes require it.
            n=self.privacy.notice
            guard=("NEW.consent_event_id IS NULL OR NOT EXISTS (SELECT 1 FROM privacy_events p "
                "WHERE p.id=NEW.consent_event_id AND p.bot=NEW.bot AND p.user=NEW.user AND p.chat=NEW.chat "
                "AND p.action='accepted' AND p.version='"+n.version+"' AND p.pdf_sha256='"+n.digest+"' "
                "AND p.seq=(SELECT MAX(seq) FROM privacy_events WHERE bot=NEW.bot AND user=NEW.user))")
            conn.executescript("BEGIN IMMEDIATE; " + " ".join(
                "DROP TRIGGER IF EXISTS booking_consent_"+suffix+"; CREATE TRIGGER booking_consent_"+suffix+
                " BEFORE "+operation+" ON bookings WHEN "+guard+
                " BEGIN SELECT RAISE(ABORT,'Account consent is required'); END;"
                for operation,suffix in (("INSERT","insert"),("UPDATE OF consent_event_id","update")))+" COMMIT;")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=65, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, booking, conn=None):
        if conn is not None:
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
        else:
            with self.connect() as own:
                row = own.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
        if row is None:
            raise ValueError("Reserva no encontrada")
        return dict(row)

    @staticmethod
    def owner(row, identity, bot):
        if (row["bot"], row["user"], row["chat"]) != (bot, identity["user_id"], identity["chat_id"]):
            raise PermissionError("Esta reserva pertenece a otra cuenta")

    def active_count(self, identity, bot, instant, conn=None):
        query = ("SELECT COUNT(*) FROM bookings WHERE bot=? AND user=? "
                 "AND state IN ('creating','uncertain','confirmed','canceling') "
                 "AND (julianday(start,'+30 minutes')>julianday(?) OR julianday(start) IS NULL)")
        args = (bot, identity["user_id"], instant.isoformat())
        if conn is not None:
            return conn.execute(query, args).fetchone()[0]
        with self.connect() as own:
            return own.execute(query, args).fetchone()[0]

    def enforce_limit(self, identity, bot, instant, conn=None):
        if self.active_count(identity, bot, instant, conn) >= MAX_ACTIVE_BOOKINGS:
            raise ValueError("Ya tienes el maximo de tres citas activas por cuenta. "
                             "Puedes consultar o cancelar una cita propia antes de reservar otra")

    def reusable(self, identity, bot, start, contact, cedula, telefono, conn=None):
        if conn is None:
            with self.connect() as own:
                return self.reusable(identity, bot, start, contact, cedula, telefono, own)
        previous = conn.execute("SELECT * FROM bookings WHERE bot=? AND user=? AND chat=? AND start=? "
            "AND (state IN ('creating','uncertain','confirmed','canceling') OR (state='proposed' AND expires>?)) "
            "ORDER BY created DESC", (bot,identity["user_id"],identity["chat_id"],start,time.time())).fetchall()
        patient_key = " ".join(contact.split()).casefold()
        for previous_row in previous:
            row = dict(previous_row)
            if row["state"]=="proposed" and row["consent_event_id"]!=self.privacy.require(identity,bot,conn)["id"]:
                continue
            same_patient = row["cedula"] == cedula or (not row["cedula"] and
                " ".join(row["contact"].split()).casefold() == patient_key)
            if same_patient and (row["state"] != "proposed" or
                    (row["cedula"] == cedula and row["telefono"] == telefono and
                     " ".join(row["contact"].split()).casefold() == patient_key)):
                if row["patients"] != 1:
                    raise ValueError("La reserva antigua requiere revision del personal")
                return row
        return None

    def proposal(self, identity, bot, start, patients, contact, cedula, telefono, *, instant=None):
        if type(patients) is not int or patients != 1:
            raise ValueError("Cada cita corresponde a una sola persona")
        self.privacy.require(identity,bot)
        contact, cedula, telefono = patient_details(contact, cedula, telefono)
        with self.transaction() as conn:
            authorization=self.privacy.require(identity,bot,conn)
            previous = self.reusable(identity, bot, start, contact, cedula, telefono, conn)
            if previous:
                return previous
            self.enforce_limit(identity, bot, instant or now(), conn)
            # Corrected data requires a fresh button; an older pending button cannot
            # silently confirm different data from the message it was attached to.
            conn.execute("UPDATE bookings SET state='superseded' WHERE bot=? AND user=? AND chat=? "
                "AND start=? AND cedula=? AND state='proposed'",
                (bot,identity["user_id"],identity["chat_id"],start,cedula))
            code = uuid.uuid4().hex
            conn.execute("INSERT INTO bookings(id,event_id,bot,user,chat,thread,start,patients,contact,cedula,telefono,consent_event_id,state,expires,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?)", (code,"cb"+code,bot,identity["user_id"],identity["chat_id"],
                    identity.get("thread_id"),start,patients,contact,cedula,telefono,authorization["id"],time.time()+600,time.time()))
            return self.one(code, conn)

    def update(self, booking, **fields):
        allowed = {"state","anchor","confirmed","notify","notify_message","customer_notice","notify_due","customer_due"}
        if not fields.keys() <= allowed:
            raise ValueError("Campo interno no permitido")
        with self.transaction() as conn:
            conn.execute("UPDATE bookings SET " + ",".join(k+"=?" for k in fields) + " WHERE id=?",
                         (*fields.values(),booking))

    def rows(self, *, bot=None, state=None):
        clauses, args = [], []
        if bot is not None:
            clauses.append("bot=?"); args.append(bot)
        if state is not None:
            clauses.append("state=?"); args.append(state)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM bookings" +
                (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created", args)]

    def cancellation(self, booking, thread=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM cancellations WHERE booking=? AND thread IS ? AND state='proposed' AND expires>?",
                               (booking,thread,time.time())).fetchone()
            if row:
                return dict(row)
            token = uuid.uuid4().hex
            conn.execute("INSERT INTO cancellations(token,booking,expires,thread) VALUES(?,?,?,?)",
                         (token,booking,time.time()+600,thread))
            return dict(conn.execute("SELECT * FROM cancellations WHERE token=?", (token,)).fetchone())

    def anchor_cancellation(self, token, anchor):
        with self.transaction() as conn:
            conn.execute("UPDATE cancellations SET anchor=? WHERE token=?", (anchor,token))

    def claim_notice(self, booking, field, state):
        if field not in {"notify","customer_notice"}:
            raise ValueError("Canal de aviso invalido")
        with self.transaction() as conn:
            return conn.execute("UPDATE bookings SET " + field + "='sending' WHERE id=? AND state=? AND " + field + "='pending'",
                                (booking,state)).rowcount == 1

    def finish_notice(self, booking, field, state, outcome, due=0):
        if field not in {"notify","customer_notice"} or outcome not in {"sent","pending","failed","ambiguous"}:
            raise ValueError("Resultado de aviso invalido")
        due_field="notify_due" if field=="notify" else "customer_due"
        with self.transaction() as conn:
            conn.execute("UPDATE bookings SET "+field+"=?,"+due_field+"=? WHERE id=? AND state=? AND "+field+"='sending'",
                         (outcome,due,booking,state))

    def confirm(self, booking):
        with self.transaction() as conn:
            conn.execute("UPDATE bookings SET state='confirmed',confirmed=?,notify='pending',customer_notice='pending' "
                         "WHERE id=? AND state IN ('creating','uncertain')",(time.time(),booking))

    def create_error(self, booking, uncertain):
        with self.transaction() as conn:
            conn.execute("UPDATE bookings SET state=? WHERE id=? AND state IN ('creating','uncertain')",
                         ("uncertain" if uncertain else "failed",booking))

    def recover_notices(self):
        with self.transaction() as conn:
            for field in ("notify","customer_notice"):
                conn.execute("UPDATE bookings SET "+field+"='ambiguous' WHERE "+field+"='sending'")
