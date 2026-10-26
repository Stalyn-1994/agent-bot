"""Local configuration, Windows user-bound credential protection and safe file writes."""
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

TZ = timezone(timedelta(hours=-5), "America/Guayaquil")
MAX_ACTIVE_BOOKINGS = 3


def now():
    return datetime.now(TZ)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


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


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def dpapi(data, *, decrypt=False):
    """Credential bytes can only be decrypted by this Windows user on this machine."""
    if os.name != "nt":
        raise RuntimeError("La proteccion de credenciales de este paquete requiere Windows")
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
        raise OSError("No se pudieron proteger o leer las credenciales de Google")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class Credentials:
    def __init__(self, path):
        self.path = Path(path)
    def read(self):
        raw = self.path.read_bytes()
        if not raw.startswith(b"CLCAL1\0"):
            raise RuntimeError("Formato de credenciales no reconocido")
        return json.loads(dpapi(raw[7:], decrypt=True))
    def write(self, value):
        atomic(self.path, b"CLCAL1\0" + dpapi(json.dumps(value).encode("utf-8")))


def config(home, *, active=True):
    value = read_json(Path(home) / "plugin-data/corelab-citas/config.json", {})
    if active and not value.get("enabled"):
        raise RuntimeError("La agenda aun no esta conectada. El administrador debe ejecutar conectar.cmd")
    if value.get("timezone") != "America/Guayaquil" or value.get("slot_minutes") != 30 or value.get("capacity") != 2:
        raise RuntimeError("Configuracion de agenda incompatible: requiere Guayaquil, 30 minutos y dos citas individuales por franja")
    if value.get("patients_per_booking",1) != 1:
        raise RuntimeError("Cada cita debe corresponder a una sola persona")
    if value.get("max_active_bookings_per_customer", MAX_ACTIVE_BOOKINGS) != MAX_ACTIVE_BOOKINGS:
        raise RuntimeError("El limite de esta version es de tres citas activas por cuenta de cliente")
    if active and (not isinstance(value.get("calendar_id"), str) or
                   not value["calendar_id"].endswith("@group.calendar.google.com")):
        raise RuntimeError("Selecciona un calendario exclusivo de citas, no el calendario principal")
    return value


def fingerprint(pid):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        raise OSError("El proceso del gateway no esta disponible")
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            raise OSError("El proceso del gateway ya termino")
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError("No se pudo verificar el proceso del gateway")
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)
