"""Warm original rag_memory.plugin inside the actual gateway process."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time

logger = logging.getLogger("hooks.rag-memory-preload")


def process_fingerprint(pid):
    """Windows process creation time prevents mistaking a reused PID for readiness."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("Gateway process is not running or cannot be inspected")
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            raise OSError("Gateway process is no longer running")
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise OSError("Cannot inspect gateway process creation time")
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def write_status(home, status):
    directory = Path(home) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "rag-memory-preload.json"
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, destination)


def run_preload(home):
    """No agent turn, no provider request, no customer identity or private notes."""
    started = time.perf_counter()
    status = {"stage": "starting", "pid": os.getpid(),
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        status["process_fingerprint"] = process_fingerprint(os.getpid())
        write_status(home, status)
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        settings = config.get("plugins", {}).get("entries", {}).get(
            "rag-memory", {}).get("settings", {})
        default_db = Path.home() / ".hermes" / "plugins" / "rag-memory" / "rag_core.db"
        db = Path(settings.get("db_path", str(default_db))).expanduser().resolve()
        status["database"] = str(db)
        logger.info("RAG preload starting (gateway PID %s)", os.getpid())
        from rag_memory.plugin import preload_rag_memory
        status.update(preload_rag_memory(db))
        status["stage"] = "ready"
        logger.info("RAG preload ready in %.2fs (gateway PID %s)",
                    time.perf_counter() - started, os.getpid())
    except Exception as exc:
        status.update(stage="error", error=str(exc))
        logger.exception("RAG preload failed; inspect logs/rag-memory-preload.json")
    status["duration_seconds"] = round(time.perf_counter() - started, 3)
    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_status(home, status)
    return status


async def handle(event_type, context):
    if event_type != "gateway:startup":
        return
    # Read the profile home on the gateway thread before entering the worker.
    home = Path(os.environ["HERMES_HOME"])
    # Await completion; do not start an untracked daemon or warm another process.
    await asyncio.to_thread(run_preload, home)
