"""Native Telegram menu policy, independent of tool permissions and handoff routing."""
import os
from pathlib import Path

plugin_name = "corelab-menu"
plugin_version = "1.0.0"


def register(ctx):
    from .menus import Menus

    for method in ("register_platform_handler", "spawn_task"):
        if not callable(getattr(ctx, method, None)):
            raise RuntimeError("Esta version de Hermes no admite " + method)
    home = Path(os.environ["HERMES_HOME"]).resolve()
    wired = set()

    def wire(application, adapter):
        from telegram import Update
        from telegram.ext import TypeHandler

        if id(application) in wired:
            return
        wired.add(id(application))
        menus = Menus(home, application, adapter)
        # Observer only: no LLM, no Bot API calls and no ApplicationHandlerStop.
        # Runs before the handoff's -90 group, without displacing its handlers.
        application.add_handler(TypeHandler(Update, menus.observe, block=True), group=-110)
        ctx.spawn_task(menus.run(), name="corelab-menu:telegram")

    ctx.register_platform_handler("telegram", wire)
