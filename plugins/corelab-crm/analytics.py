"""Read existing stores without instantiating Hermes writers or changing their schema."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from .common import TZ, alias, columns, directory, readonly, settings, secret_key, read_json
from .collector import staff
from .taxonomy import classify, SCHEMA


def text_content(raw):
    if not isinstance(raw, str):
        return ""
    if raw.startswith("\x00json:"):
        try:
            raw = json.loads(raw[6:])
        except ValueError:
            return ""
    if isinstance(raw, list):
        return "\n".join(str(x.get("text", "")) for x in raw if isinstance(x, dict) and x.get("type") in {"text", "input_text", "output_text"})
    return raw if isinstance(raw, str) else ""


def identity(session):
    origin = {}
    if session.get("origin_json"):
        try:
            origin = json.loads(session["origin_json"])
        except (ValueError, TypeError):
            pass
    if not isinstance(origin, dict):
        origin = {}
    chat = str(session.get("chat_id") or origin.get("chat_id") or "")
    user = str(session.get("user_id") or origin.get("user_id") or "")
    kind = session.get("chat_type") or origin.get("chat_type")
    # Native Telegram private chat IDs are positive and equal to their human user ID.
    if not chat:
        match = re.search(r"(?:^|:)telegram:dm:([0-9]+)(?::|$)", session.get("session_key") or "")
        if match:
            chat = match[1]
            kind = "dm"
    if kind not in {None, "", "dm", "private"}:
        return None
    if not chat and user.isdigit():
        # Explicit user alone does not prove that a session is a private chat.
        return None
    if chat.isdigit() and int(chat) > 0 and (not user or user == chat):
        return chat
    return None


def schema_check(home):
    cfg = settings(home)
    path = Path(home) / cfg["state_db"]
    with readonly(path) as conn:
        sc, mc = columns(conn, "sessions"), columns(conn, "messages")
        if not {"id", "source"} <= sc or not {"id", "session_id", "role", "content", "timestamp"} <= mc:
            raise RuntimeError("state.db tiene un esquema no compatible; no se genero un reporte vacio")
        if not {"user_id", "chat_id", "origin_json", "session_key"}.intersection(sc):
            raise RuntimeError("No hay campos de identidad de Telegram en sessions; se necesita adaptar el lector")
        return {"path": str(path.resolve()), "sessions_columns": sorted(sc), "messages_columns": sorted(mc),
                "telegram_sessions": conn.execute("SELECT COUNT(*) FROM sessions WHERE lower(source)='telegram'").fetchone()[0]}


def routing(home, conn):
    result = {}
    if columns(conn, "gateway_routing"):
        for row in conn.execute("SELECT entry_json FROM gateway_routing"):
            try:
                item = json.loads(row[0])
                result[item["session_id"]] = item
            except (ValueError, KeyError, TypeError):
                continue
    # Legacy index is supplementary only, not the archive of all sessions.
    legacy = read_json(Path(home) / "sessions/sessions.json", {})
    if isinstance(legacy, dict):
        entries = legacy.get("sessions", legacy)
        if isinstance(entries, dict):
            for key, item in entries.items():
                if isinstance(item, dict) and item.get("session_id"):
                    result.setdefault(item["session_id"], dict(item, session_key=key))
    return result


def messages(home, start, end, cfg, excluded, warnings):
    native, assistant = [], []
    seen = set()
    quality = Counter()
    with readonly(Path(home) / cfg["state_db"]) as conn:
        schema_check(home)
        mc = columns(conn, "messages")
        routes = routing(home, conn)
        sessions = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sessions WHERE lower(source)='telegram'")}
        for sid, session in sessions.items():
            item = routes.get(sid, {})
            if item:
                origin = item.get("origin", {})
                for name in ("session_key", "chat_type"):
                    if not session.get(name):
                        session[name] = item.get(name)
                for name in ("chat_id", "user_id"):
                    if not session.get(name):
                        session[name] = origin.get(name) if isinstance(origin, dict) else None
        sql = "SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id WHERE lower(s.source)='telegram' AND m.timestamp>=? AND m.timestamp<? ORDER BY m.timestamp,m.id"
        for raw in conn.execute(sql, (start.timestamp(), end.timestamp())):
            row = dict(raw)
            session = sessions.get(row["session_id"], {})
            who = identity(session)
            if not who:
                quality["rows_without_private_identity"] += 1
                continue
            if who in excluded:
                quality["staff_rows_excluded"] += 1
                continue
            if row["role"] not in {"user", "assistant"}:
                continue
            if row.get("display_kind") == "hidden" or row.get("_compressed_summary"):
                continue
            if row["role"] == "assistant" and row.get("tool_calls") not in {None, "", "[]", "null"}:
                continue
            content = text_content(row["content"]).strip()
            if not content or content.startswith(("[CONTEXT SUMMARY", "[Context summary", "[System note:", "<context_summary>")):
                continue
            if row["role"] == "user" and content.startswith("/"):
                continue
            mid = str(row.get("platform_message_id") or "")
            # Telegram IDs may be serialized as 'chat:message' by some builds.
            if ":" in mid:
                mid = mid.rsplit(":", 1)[1]
            reliable = mid.isdigit() and int(mid) > 0
            dedupe = (who, row["role"], mid) if reliable else (who, row["role"], row["timestamp"], hashlib.sha256(content.encode()).hexdigest())
            if dedupe in seen:
                quality["duplicate_rows_excluded"] += 1
                continue
            seen.add(dedupe)
            event = {"user": who, "at": float(row["timestamp"]), "message": mid if reliable else None,
                     "session": row["session_id"], **classify(content), "kind": "text"}
            # The content is classified in memory and never exported or duplicated to CRM.
            if row["role"] == "user":
                native.append(event)
            else:
                assistant.append(event)
    observed = []
    observer_path = directory(home) / "crm.db"
    if observer_path.is_file():
        with readonly(observer_path) as conn:
            if columns(conn, "events"):
                for r in conn.execute("SELECT * FROM events WHERE timestamp>=? AND timestamp<? AND bot=? ORDER BY timestamp,message",
                                      (start.timestamp(), end.timestamp(), cfg["bot_id"])):
                    if str(r["user"]) in excluded:
                        continue
                    observed.append({"user": str(r["user"]), "at": r["timestamp"], "message": str(r["message"]),
                        "topics": json.loads(r["topics"]), "signals": json.loads(r["signals"]),
                        "knowledge_gap": False, "kind": r["kind"]})
    observed_keys = {(r["user"], r["message"]) for r in observed}
    merged = list(observed)
    for event in native:
        if event["message"]:
            if (event["user"], event["message"]) not in observed_keys:
                merged.append(event)
        elif any(o["user"] == event["user"] and abs(o["at"] - event["at"]) <= 5 for o in observed):
            # Cannot safely merge a timestamp-only old Hermes row with a native Telegram event.
            # Prefer the observed message and disclose omitted rows rather than double-count it.
            quality["ambiguous_timestamp_rows_omitted"] += 1
        else:
            merged.append(event)
    if quality["rows_without_private_identity"]:
        warnings.append("Se omitieron " + str(quality["rows_without_private_identity"]) + " filas sin identidad privada verificable; pueden incluir grupos o historiales antiguos.")
    if quality["ambiguous_timestamp_rows_omitted"]:
        warnings.append("Se prefirio el registro nativo para " + str(quality["ambiguous_timestamp_rows_omitted"]) + " filas sin ID de Telegram cercanas en tiempo; el conteo combinado es aproximado.")
    return sorted(merged, key=lambda x: x["at"]), assistant, dict(quality)


def operations(home, start, end, bot_id, excluded):
    result = {"bookings_available": False, "bookings_confirmed": None, "booking_customers": [],
              "handoff_available": False, "cases_created": None, "open_cases_at_generation": None,
              "case_customers": [], "open_case_customers": []}
    path = Path(home) / "plugin-data/corelab-citas/appointments.db"
    if path.is_file():
        with readonly(path) as conn:
            if {"bot", "user", "confirmed"} <= columns(conn, "bookings"):
                rows = conn.execute("SELECT user FROM bookings WHERE bot=? AND confirmed>=? AND confirmed<?",
                                    (bot_id, start.timestamp(), end.timestamp())).fetchall()
                users = [str(r[0]) for r in rows if str(r[0]) not in excluded]
                result.update(bookings_available=True, bookings_confirmed=len(users), booking_customers=users)
    path = Path(home) / "plugin-data/corelab-handoff/handoff.db"
    if path.is_file():
        with readonly(path) as conn:
            if {"bot_id", "user_id", "created", "state"} <= columns(conn, "cases"):
                rows = [dict(r) for r in conn.execute("SELECT user_id,created,state FROM cases WHERE bot_id=?", (bot_id,))
                        if str(r["user_id"]) not in excluded]
                current = [r for r in rows if r["state"] in {"notifying", "waiting", "active", "closing"}]
                created = [r for r in rows if start.timestamp() <= r["created"] < end.timestamp()]
                result.update(handoff_available=True, cases_created=len(created), open_cases_at_generation=len(current),
                              case_customers=[str(r["user_id"]) for r in created],
                              open_case_customers=[str(r["user_id"]) for r in current])
    return result


def summarize(events, replies, start, end):
    events = [e for e in events if start.timestamp() <= e["at"] < end.timestamp()]
    replies = [e for e in replies if start.timestamp() <= e["at"] < end.timestamp()]
    by_user = defaultdict(list)
    daily = [0] * 7
    hours = [0] * 24
    topics, signals = Counter(), Counter()
    for event in events:
        by_user[event["user"]].append(event)
        local = datetime.fromtimestamp(event["at"], TZ)
        daily[(local.date() - start.date()).days] += 1
        hours[local.hour] += 1
        topics.update(event["topics"])
        signals.update(event["signals"])
    episodes = sum(1 for group in by_user.values() for i, e in enumerate(group)
                   if not i or e["at"] - group[i-1]["at"] > 1800)
    gaps = sum(bool(e["knowledge_gap"]) for e in replies)
    return {"customers": len(by_user), "messages": len(events), "conversations": episodes,
            "ai_replies_recorded": len(replies), "knowledge_gap_replies": gaps,
            "repeat_customers_in_week": sum(len({datetime.fromtimestamp(e['at'], TZ).date() for e in group}) > 1 for group in by_user.values()),
            "daily_messages": daily, "hourly_messages": hours, "topics": dict(topics.most_common()),
            "signals": dict(signals), "by_user": by_user}


def priorities(metrics, ops):
    items = []
    def add(level, focus, evidence, action):
        items.append({"priority": level, "focus": focus, "evidence": evidence, "action": action})
    opened = ops["open_cases_at_generation"]
    if opened:
        add("Alta", "Casos humanos abiertos", f"{opened} caso(s) abiertos al generar el informe.",
            "Revisar la bandeja de asesores, asignacion y cierre de cada caso. Un caso abierto no demuestra incumplimiento de plazo.")
    complaints = metrics["signals"].get("insatisfaccion", 0)
    if complaints:
        add("Alta", "Posible friccion de servicio", f"{complaints} mensaje(s) coinciden con expresiones de queja o espera.",
            "Revisar las conversaciones indicadas antes de concluir que hubo una queja; las coincidencias pueden ser ambiguas.")
    if metrics["knowledge_gap_replies"]:
        add("Media", "Cobertura documental", f"{metrics['knowledge_gap_replies']} respuesta(s) registradas expresan falta de informacion verificable.",
            "Revisar esas consultas en Hermes y actualizar las fuentes oficiales si falta informacion; luego volver a indexar.")
    topics = metrics["topics"]
    if topics:
        top, count = next(iter(topics.items()))
        add("Media", "Demanda principal: " + top, f"{count} mensaje(s) etiquetados en este tema; un mensaje puede tener varios temas.",
            "Priorizar la claridad y disponibilidad de la informacion de este tema y verificar los resultados del flujo correspondiente.")
    interest = metrics["signals"].get("interes_compra", 0)
    if interest:
        add("Media", "Interes comercial expresado", f"{interest} mensaje(s) coinciden con expresiones de cotizacion, examen o cita.",
            "Revisar barreras entre consulta, cotizacion y reserva. Estos mensajes no equivalen a ventas ni permiten inferir una tasa de abandono.")
    if metrics["messages"]:
        hour = max(range(24), key=lambda h: metrics["hourly_messages"][h])
        add("Operativa", "Cobertura horaria", f"La hora con mas mensajes fue {hour:02d}:00-{hour:02d}:59 ({metrics['hourly_messages'][hour]} mensajes).",
            "Comparar esta franja con la disponibilidad de asesores. Un pico de una semana no establece una tendencia permanente.")
    if not items:
        add("Verificacion", "Sin actividad registrada", "No hay mensajes de clientes identificables en el periodo ni casos abiertos consultables.",
            "Comprobar cobertura, cuentas excluidas y funcionamiento del canal. La falta de registros no confirma ausencia de clientes.")
    return items[:6]


def build_report(home, start, end, *, generated=None):
    home = Path(home)
    if (end - start) != timedelta(days=7) or start.weekday() != 0:
        raise ValueError("El periodo debe ser una semana completa de lunes a domingo")
    cfg = settings(home)
    key = secret_key(home)
    warnings = []
    excluded = staff(home, cfg["excluded_users"])
    events, replies, quality = messages(home, start - timedelta(days=7), end, cfg, excluded, warnings)
    current = summarize(events, replies, start, end)
    previous = summarize(events, replies, start - timedelta(days=7), start)
    ops = operations(home, start, end, cfg["bot_id"], excluded)
    clients = []
    for user, group in current.pop("by_user").items():
        themes = Counter(t for e in group for t in e["topics"])
        signals = Counter(s for e in group for s in e["signals"])
        clients.append({"client": alias(key, user), "messages": len(group),
            "topics": [t for t, _ in themes.most_common(3)], "signals": list(signals),
            "days_active": len({datetime.fromtimestamp(e["at"], TZ).date() for e in group}),
            "confirmed_bookings": ops["booking_customers"].count(user) if ops["bookings_available"] else None,
            "open_case": user in ops["open_case_customers"] if ops["handoff_available"] else None})
    clients.sort(key=lambda r: (-bool(r["open_case"]), -int("insatisfaccion" in r["signals"]), -r["messages"], r["client"]))
    previous.pop("by_user")
    for k in ("booking_customers", "case_customers", "open_case_customers"):
        ops.pop(k)
    report = {"version": "1.0.0", "classifier": SCHEMA, "company": "Corelab",
        "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
        "generated_at": (generated or datetime.now(TZ)).isoformat(), "timezone": "America/Guayaquil",
        "metrics": current, "previous_week": previous, "operations": ops, "clients": clients,
        "priorities": priorities(current, ops), "quality": quality, "warnings": warnings,
        "sources": {"hermes_history": "state.db / sessions + messages (solo Telegram privado)",
            "new_indicators": "plugin-data/corelab-crm/crm.db (sin texto de mensajes)",
            "appointments": "appointments.db: fechas de confirmacion, sin datos del paciente",
            "human_cases": "handoff.db: fechas y estados de casos, sin resumen ni mensajes"},
        "collection_started": cfg.get("collection_started"), "excluded_staff_count": len(excluded)}
    if not cfg.get("collection_started") or float(cfg["collection_started"]) > start.timestamp():
        report["warnings"].append("El registro CRM nativo no cubre toda la semana. El historial anterior puede omitir mensajes atendidos directamente por asesores o botones.")
    error = read_json(home / "logs/corelab-crm-collection-error.json", {})
    if error.get("time", 0) >= start.timestamp() and error.get("time", 0) < end.timestamp():
        report["warnings"].append("Se registro al menos un error de captura durante el periodo. Revisar el estado local antes de comparar volumenes.")
    return report
