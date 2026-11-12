"""Profile-local files, read-only SQLite and Windows-protected mail credentials."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile

VERSION = "1.0.0"
RECIPIENT = "guzmantorreskaren50@gmail.com"
TZ = timezone(timedelta(hours=-5), "America/Guayaquil")
JOB_NAME = "corelab-crm-semanal"
SCRIPT = "corelab_crm_weekly.py"


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


@contextmanager
def readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("No existe la base requerida: " + str(path))
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def columns(conn, table):
    if table not in {"sessions", "messages", "gateway_routing", "bookings", "cases", "private_operators", "settings", "events"}:
        raise ValueError("Tabla no permitida")
    return {r["name"] for r in conn.execute("PRAGMA table_info(" + table + ")")}


def directory(home):
    return Path(home) / "plugin-data/corelab-crm"


def settings(home):
    result = read_json(directory(home) / "config.json", {})
    if result.get("recipient") != RECIPIENT or result.get("timezone") != "America/Guayaquil":
        raise ValueError("Configuracion CRM no valida: destinatario o zona horaria distintos")
    if not isinstance(result.get("excluded_users"), list):
        raise ValueError("Se requiere excluded_users")
    return result


def secret_key(home):
    path = directory(home) / "alias-key.bin"
    if path.is_file() and len(path.read_bytes()) == 32:
        return path.read_bytes()
    raise RuntimeError("Falta la clave local de seudonimos. Ejecuta instalar.cmd")


def alias(key, user):
    return "C-" + hmac.new(key, ("telegram:" + str(user)).encode(), hashlib.sha256).hexdigest()[:10].upper()


def previous_week(instant=None):
    local = (instant or datetime.now(TZ)).astimezone(TZ)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    return end - timedelta(days=7), end


def week_from_date(value):
    start = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    if start.weekday() != 0:
        raise ValueError("La semana debe empezar en lunes (YYYY-MM-DD)")
    return start, start + timedelta(days=7)


# DPAPI implementation is shared in design with the appointment package; tokens are separate.
def dpapi(data, *, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Las credenciales requieren el mismo usuario de Windows que ejecuta Hermes")
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("No fue posible proteger o descifrar el token de correo")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class Credentials:
    def __init__(self, path):
        self.path = Path(path)
    def read(self):
        data = self.path.read_bytes()
        if not data.startswith(b"CLCRM1\0"):
            raise ValueError("Token de correo no reconocido")
        return json.loads(dpapi(data[7:], decrypt=True))
    def write(self, value):
        atomic(self.path, b"CLCRM1\0" + dpapi(json.dumps(value).encode()))


def initial_config():
    return {"version": VERSION, "recipient": RECIPIENT, "timezone": "America/Guayaquil",
            "bot_id": 8634172328, "excluded_users": [957972425], "enabled": False,
            "state_db": "state.db", "collection_started": None}
