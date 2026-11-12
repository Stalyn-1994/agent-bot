"""Native Telegram menu policy, independent of tool permissions and handoff routing."""
import os
from pathlib import Path

plugin_name = "corelab-menu"
plugin_version = "1.4.0"


def register(ctx):
    from .menus import Menus
    from .welcome import Welcome

    for method in ("register_platform_handler", "spawn_task", "register_hook", "dispatch_tool"):
        if not callable(getattr(ctx, method, None)):
            raise RuntimeError("Esta version de Hermes no admite " + method)
    home = Path(os.environ["HERMES_HOME"]).resolve()
    wired = set()
    welcome = Welcome(home / "plugin-data" / "corelab-menu" / "control.json",
                      dispatch_tool=getattr(ctx,"dispatch_tool",None))

    def wire(application, adapter):
        from telegram import Update
        from telegram.ext import TypeHandler

        if id(application) in wired:
            return
        wired.add(id(application))
        welcome.bind(application)
        menus = Menus(home, application, adapter)
        # Observer only: no LLM, no Bot API calls and no ApplicationHandlerStop.
        # Runs before the handoff's -90 group, without displacing its handlers.
        application.add_handler(TypeHandler(Update, menus.observe, block=True), group=-110)
        ctx.spawn_task(menus.run(), name="corelab-menu:telegram")

    ctx.register_platform_handler("telegram", wire)
    ctx.register_hook("pre_gateway_dispatch", welcome.context)
    ctx.register_hook("transform_llm_output", welcome.transform)
    ctx.register_hook("pre_tool_call", welcome.before_tool)
    ctx.register_hook("post_tool_call", welcome.after_tool)
    # Presentation is now native; no model-facing UI tool is registered.
