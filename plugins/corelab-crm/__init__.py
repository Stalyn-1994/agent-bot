"""Corelab CRM: passive Telegram indicators; no customer-facing tools or commands."""
import os
from pathlib import Path


def register(ctx):
    if not all(callable(getattr(ctx, n, None)) for n in ("register_platform_handler", "spawn_task")):
        raise RuntimeError("Hermes no admite los manejadores de plataforma requeridos")
    from .collector import register_platform
    register_platform(ctx, Path(os.environ["HERMES_HOME"]).resolve())
