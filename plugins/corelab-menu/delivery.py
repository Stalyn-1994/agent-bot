"""One staged presentation per authenticated inbound message, delivered by the gateway."""
import contextvars
import logging
import sys
import threading
import time

API_VERSION=1
logger=logging.getLogger("corelab_menu.delivery")
_lock=threading.RLock()
_entries={}
_choices={}
_routes={}
NAME="_corelab_customer_delivery_v1"


def shared():
    return sys.modules.setdefault(NAME,sys.modules[__name__])


def key_from_source(source):
    if (getattr(getattr(source,"platform",None),"value",None)!="telegram" or
            getattr(source,"chat_type",None) not in {"dm","private"}):
        return None
    user,chat=str(getattr(source,"user_id","")),str(getattr(source,"chat_id",""))
    message=str(getattr(source,"message_id","") or "")
    if not user.isdigit() or int(user)<=0 or user!=chat or not message.isdigit():
        return None
    return (str(getattr(source,"profile","") or ""),chat,str(getattr(source,"thread_id","") or ""),message)


def bound_key():
    values={v.name:value for v,value in contextvars.copy_context().items()
            if v.name.startswith("HERMES_SESSION_") and isinstance(value,str)}
    if values.get("HERMES_SESSION_PLATFORM")!="telegram":return None
    if values.get("HERMES_SESSION_CHAT_TYPE") not in {"dm","private"}:return None
    user,chat=values.get("HERMES_SESSION_USER_ID",""),values.get("HERMES_SESSION_CHAT_ID","")
    message=values.get("HERMES_SESSION_MESSAGE_ID","")
    if user!=chat or not user.isdigit() or int(user)<=0 or not message.isdigit():return None
    return (values.get("HERMES_SESSION_PROFILE",""),chat,values.get("HERMES_SESSION_THREAD_ID",""),message)


def lookup(key):
    with _lock:
        entry=_entries.get(key)
        if entry and time.monotonic()-entry["created"]<900:return entry
        _entries.pop(key,None)
    return None


def stage(key,owner,plan):
    if key is None:raise ValueError("Falta el mensaje privado vinculado por Hermes")
    with _lock:
        old=lookup(key)
        if old:return old
        if len(_entries)>=2048:
            for k in list(_entries):
                if time.monotonic()-_entries[k]["created"]>900:_entries.pop(k,None)
            if len(_entries)>=2048:raise RuntimeError("La cola de presentacion esta ocupada")
        entry={"owner":owner,"plan":plan,"created":time.monotonic(),"state":"prepared"}
        _entries[key]=entry
        return entry


def note_choice(key,choice):
    if key is None:return
    with _lock:
        if len(_choices)>=2048:
            for k in list(_choices):
                if time.monotonic()-_choices[k][1]>900:_choices.pop(k,None)
        if len(_choices)<2048:_choices[key]=(choice,time.monotonic())


def choice(key):
    value=_choices.get(key)
    return value[0] if value and time.monotonic()-value[1]<900 else None


def ready():
    gateway=sys.modules.get("gateway.run_turn")
    cls=getattr(gateway,"GatewayTurnMixin",None)
    for name in ("_hmwa_shape_agent_response","_hmwa_deliver_turn_response","_run_agent",
                 "_hmwa_run_session_hygiene"):
        code=getattr(getattr(cls,name,None),"__code__",None)
        if code is None or NAME not in code.co_consts:return False
    return True


def shape(source,result):
    key=key_from_source(source)
    route=_routes.get(key)
    if route:route["agent_done"]=time.perf_counter()
    entry=lookup(key)
    if entry is None and route and not result.get("failed") and not result.get("interrupted"):
        text=result.get("final_response")
        if isinstance(text,str) and text.strip():
            route["view"]=route["owner"].automatic_view(route,text)
            result["final_response"]=route["owner"].decorate(route["view"],text)
        return
    if not entry or entry["state"]=="native_sent" or result.get("failed") or result.get("interrupted"):return
    result["final_response"]=entry["plan"]["text"]


async def deliver(event,source,result):
    if getattr(event,"internal",False):return False
    key=key_from_source(source)
    with _lock:
        entry=lookup(key)
        route=_routes.get(key)
        if not entry and route:
            if result.get("failed") or result.get("interrupted"):return False
            if route.get("sent"):return True
            text=result.get("final_response","")
            if not isinstance(text,str) or not text.strip() or len(text)>3500:return False
            route["sent"]=True
        elif not entry:return False
    if not entry:
        started=time.perf_counter()
        try:
            sent=await route["owner"].automatic_deliver(source,route.get("view","continuar"),text)
            if not sent:
                route["sent"]=False
                return False
            event._streamed_final_response=text
        except Exception as exc:
            logger.warning("Automatic presentation unconfirmed (%s)",type(exc).__name__)
        await route["owner"].record_timing(route,result,(time.perf_counter()-started)*1000)
        return True
    with _lock:
        if entry["state"]=="native_sent":return True
        if result.get("failed") or result.get("interrupted"):
            _entries.pop(key,None)
            return False
        if entry["state"]!="prepared":return True
        entry["state"]="sending"
    try:
        started=time.perf_counter()
        sent=await entry["owner"].deliver_plan(entry["plan"])
        entry["state"]="sent" if sent.get("menu_entregado") else "not_delivered"
        if sent.get("menu_entregado"):
            event._streamed_final_response=entry["plan"]["text"]
    except Exception as exc:
        # A transport timeout may already have delivered; never send a second copy.
        entry["state"]="delivery_unconfirmed"
        logger.warning("Menu delivery unconfirmed (%s)",type(exc).__name__)
    if route:
        await route["owner"].record_timing(route,result,(time.perf_counter()-started)*1000)
    return True


def native_delivered(key):
    if key is None:return
    with _lock:
        if len(_entries)>=2048:
            for k in list(_entries):
                if time.monotonic()-_entries[k]["created"]>900:
                    _entries.pop(k,None)
        # Native confirmation is authoritative if it happened alongside a staged menu.
        _entries[key]={"state":"native_sent","created":time.monotonic()}


def bind_route(source,owner,choice,greeting=False):
    key=key_from_source(source)
    if key is None:return
    with _lock:
        for k in list(_routes):
            if time.monotonic()-_routes[k]["created"]>900:_routes.pop(k,None)
        if len(_routes)>=2048:return
        _routes[key]={"owner":owner,"source":source,"choice":choice,"greeting":greeting,
            "created":time.monotonic(),"ingress":time.perf_counter(),"kind":"agent"}


def bypass_hygiene(source):
    from .fast import fixed_screen
    route=_routes.get(key_from_source(source))
    return bool(route and fixed_screen(route))


async def fast_run(source,message,history,kwargs):
    from .fast import FastUI
    route=_routes.get(key_from_source(source))
    if not route or kwargs.get("scheduled_heartbeat"):return None
    try:
        return await FastUI().try_fast(route,message,history,kwargs)
    except Exception as exc:
        logger.warning("Native routing unavailable (%s)",type(exc).__name__)
        route["kind"]="agent_fallback"
        return None


def route_for_key(key):
    return _routes.get(key)
