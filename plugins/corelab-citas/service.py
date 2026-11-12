"""Appointment rules. Google calendar IDs, identity and staff targets never come from the model."""
from datetime import date, datetime, time as clock, timedelta
import html
import json
import re
import sqlite3
import time

from .common import TZ, now
from .google_api import ApiError
from .patient import patient_details, masked_cedula, visit_details, appointment_description
from .privacy import ConsentRequired


def dt(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("La hora debe incluir la zona horaria")
    return result.astimezone(TZ)


def event_range(event):
    def endpoint(value):
        if "dateTime" in value:
            return dt(value["dateTime"])
        if "date" in value:
            return datetime.combine(date.fromisoformat(value["date"]), clock(), TZ)
        raise ValueError("Evento con horario no verificable")
    return endpoint(event.get("start", {})), endpoint(event.get("end", {}))


def slots(settings, day):
    result = []
    for start, end in settings["business_hours"].get(str(day.weekday()), []):
        cursor = datetime.combine(day, clock.fromisoformat(start), TZ)
        stop = datetime.combine(day, clock.fromisoformat(end), TZ)
        while cursor + timedelta(minutes=30) <= stop:
            result.append(cursor)
            cursor += timedelta(minutes=30)
    return result


def advisor(home, bot, user):
    """Resolve against the existing, verified private advisor registry; never invent recipients."""
    path = home / "plugin-data/corelab-handoff/handoff.db"
    if not path.is_file():
        raise PermissionError("Primero registra al asesor privado con corelab-handoff")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        settings = dict(conn.execute("SELECT key,value FROM settings"))
        routing = json.loads(settings.get("routing", "{}"))
        if (json.loads(settings.get("handoff_mode", '"group"')) != "private_topics" or
                routing.get("bot_id") != bot or str(user) not in routing.get("operators", {})):
            raise PermissionError("Asesor privado no autorizado para este bot")
        row = conn.execute("SELECT chat_id FROM private_operators WHERE user_id=? AND bot_id=? "
                           "AND chat_id=? AND verified>0", (user,bot,user)).fetchone()
        if not row:
            raise PermissionError("El asesor no esta verificado")
        return int(row[0])


class Service:
    def __init__(self, settings, store, google, bot, *, clock_now=now):
        self.settings, self.store, self.google, self.bot = settings, store, google, bot
        self.clock = clock_now

    def day(self, text):
        value = date.fromisoformat(text)
        today = self.clock().date()
        if not today <= value <= today + timedelta(days=int(self.settings.get("horizon_days", 30))):
            raise ValueError("La fecha debe estar entre hoy y los proximos 30 dias")
        return value

    @staticmethod
    def patients(value):
        if type(value) is not int or value != 1:
            raise ValueError("Cada cita corresponde a una sola persona. Solicita una cita distinta para cada paciente")
        return value

    def validate_start(self, text):
        value = dt(text)
        self.day(value.date().isoformat())
        if value not in slots(self.settings, value.date()) or value <= self.clock():
            raise ValueError("Selecciona una franja futura del horario de citas")
        return value

    def used(self, start, events, rows, *, exclude=None):
        end = start + timedelta(minutes=30)
        known = {row["event_id"]: row for row in rows}
        counted, total = set(), 0
        for event in events:
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            a, b = event_range(event)
            if a >= end or b <= start:
                continue
            row = known.get(event.get("id"))
            if row and row["id"] == exclude:
                continue
            if (row and row["state"] in {"confirmed","creating","uncertain"} and
                    dt(row["start"]) == a and b == a + timedelta(minutes=30) and
                    event.get("extendedProperties", {}).get("private", {}).get("corelab_booking") == row["id"]):
                total += row["patients"]
                counted.add(row["id"])
            else:
                # A manual or modified Calendar event blocks the whole overlapping slot.
                total += 2
        for row in rows:
            if row["id"] != exclude and row["id"] not in counted and row["state"] in {"creating","uncertain"}:
                if dt(row["start"]) == start:
                    total += row["patients"]
        return total

    def availability(self, day, patients=1):
        day, patients = self.day(day), self.patients(patients)
        start = datetime.combine(day, clock(), TZ)
        events = self.google.events(start, start + timedelta(days=1))
        rows = self.store.rows()
        return [{"inicio": point.isoformat(), "cupos": max(0,2-self.used(point,events,rows))}
                for point in slots(self.settings,day) if point > self.clock() and
                self.used(point,events,rows) + patients <= 2]

    def proposal(self, identity, start, patients, contact, cedula, telefono, correo=None, *, modalidad="laboratorio", direccion=None, descripcion=None):
        self.store.privacy.require(identity,self.bot)
        start, patients = self.validate_start(start), self.patients(patients)
        contact, cedula, telefono, correo = patient_details(contact, cedula, telefono, correo)
        modalidad, direccion = visit_details(modalidad, direccion)
        descripcion = appointment_description(descripcion)
        previous = self.store.reusable(identity,self.bot,start.isoformat(),contact,cedula,telefono,correo,modalidad=modalidad,direccion=direccion,descripcion=descripcion)
        if previous:
            return previous
        self.store.enforce_limit(identity,self.bot,self.clock())
        available = self.availability(start.date().isoformat(),patients)
        if start.isoformat() not in {row["inicio"] for row in available}:
            raise ValueError("La franja ya no tiene cupos suficientes")
        return self.store.proposal(identity,self.bot,start.isoformat(),patients,contact,cedula,telefono,correo,
                                   instant=self.clock(),modalidad=modalidad,direccion=direccion,descripcion=descripcion)

    @staticmethod
    def description(row):
        generic = "Reserva gestionada por Corelab. Cambios y cancelaciones desde el bot."
        if row.get("calendar_details_version", 0) != 1:
            return generic
        contact, cedula, telefono, correo = patient_details(row["contact"],row["cedula"],row["telefono"],row["correo"])
        # Calendar descriptions support HTML; escape customer input so names/emails remain plain data.
        fields = [("Paciente", contact), ("Cédula", cedula), ("Correo", correo), ("Celular / teléfono", telefono)]
        if row.get("modalidad")=="domicilio":
            fields.append(("Dirección", row["direccion"]))
            generic="Visita a domicilio pendiente de confirmación. El asesor debe contactar al cliente."
        if row.get("descripcion") is not None:
            fields.append(("Descripción de la cita", appointment_description(row["descripcion"])))
        return "\n".join(label+": "+html.escape(value, quote=True) for label,value in fields)+"\n\n"+generic

    @staticmethod
    def visible_description(value):
        # Google clients can normalize line breaks; escaped user HTML is decoded only after that.
        value = re.sub(r"<br\s*/?>", "\n", str(value), flags=re.I).replace("\r\n", "\n")
        return html.unescape(value).strip()

    @staticmethod
    def body(row):
        Service.patients(row["patients"])
        start = dt(row["start"])
        mode=row.get("modalidad")
        label={"laboratorio":"Laboratorio", "domicilio":"Domicilio · Pendiente"}.get(mode)
        return {"id": row["event_id"], "summary": (label+" · Corelab " if label else "Cita Corelab ")+row["id"][:8].upper()+
                    " · 1 paciente",
                **({"colorId":"6","status":"tentative"} if mode=="domicilio" else
                   {"colorId":"9"} if mode=="laboratorio" else {}),
                "description": Service.description(row),
                "start": {"dateTime":start.isoformat(),"timeZone":"America/Guayaquil"},
                "end": {"dateTime":(start+timedelta(minutes=30)).isoformat(),"timeZone":"America/Guayaquil"},
                "visibility":"private", "transparency":"opaque",
                "extendedProperties":{"private":{"corelab_booking":row["id"],
                    **({"corelab_details_version":"1"} if row.get("calendar_details_version",0)==1 else {})}}}

    def verify_event(self, event, row):
        a, b = event_range(event)
        if (event.get("id") != row["event_id"] or event.get("status") == "cancelled" or
                event.get("extendedProperties",{}).get("private",{}).get("corelab_booking") != row["id"] or
                a != dt(row["start"]) or b != a+timedelta(minutes=30)):
            raise ValueError("El evento de la reserva cambio; requiere revision del personal")
        if row.get("modalidad")=="domicilio" and event.get("status")!="tentative":
            raise ValueError("La visita no quedó registrada como pendiente en Calendar")
        if row.get("calendar_details_version",0)==1:
            if (event.get("visibility")!="private" or
                    event.get("extendedProperties",{}).get("private",{}).get("corelab_details_version")!="1" or
                    self.visible_description(event.get("description","")) != self.visible_description(self.description(row))):
                raise ValueError("Los datos de contacto no quedaron verificados en Calendar; requiere revision del personal")

    def finish_create(self, row):
        wrote=False
        try:
            if row.get("calendar_details_version",0)==1:
                identity={"user_id":row["user"],"chat_id":row["chat"]}
                try:
                    consent=self.store.privacy.require(identity,self.bot)
                    if consent["id"] != row["consent_event_id"]:
                        raise ConsentRequired("La autorizacion de la solicitud cambio")
                except ConsentRequired:
                    # Read an event that may already exist, but never transmit details after withdrawal/reset.
                    try:
                        existing=self.google.event(row["event_id"])
                        self.verify_event(existing,row)
                        self.store.confirm(row["id"])
                    except ApiError as exc:
                        self.store.create_error(row["id"],exc.status not in {404,410})
                    except Exception:
                        self.store.create_error(row["id"],True)
                    return self.store.one(row["id"])
            try:
                self.google.create(self.body(row))
            except ApiError as exc:
                if exc.status != 409:
                    raise
            wrote=True
            # Real read-back, including on a retry after a lost create response.
            self.verify_event(self.google.event(row["event_id"]), row)
            self.store.confirm(row["id"])
        except ApiError as exc:
            uncertain = wrote or exc.status in {0,408,409,429} or exc.status >= 500
            self.store.create_error(row["id"],uncertain)
        except Exception:
            self.store.create_error(row["id"],True)
        return self.store.one(row["id"])

    def book(self, booking, identity, *, anchor=None, thread=None):
        with self.store.transaction() as conn:
            row = self.store.one(booking,conn)
            self.store.owner(row,identity,self.bot)
            if anchor is not None and (row["anchor"] != anchor or row["thread"] != thread):
                raise PermissionError("Boton de reserva no valido en esta conversacion")
            if row["state"] != "proposed":
                return row
            authorization=self.store.privacy.require(identity,self.bot,conn)
            if row["consent_event_id"]!=authorization["id"]:
                raise ConsentRequired("La solicitud es anterior a la autorizacion vigente. Solicita nuevamente la cita")
            self.patients(row["patients"])
            patient_details(row["contact"],row["cedula"],row["telefono"],row["correo"])
            if row["expires"] < time.time():
                raise ValueError("La confirmacion vencio; solicita nuevamente la cita")
            self.store.enforce_limit(identity,self.bot,self.clock(),conn)
            point = self.validate_start(row["start"])
            events = self.google.events(point,point+timedelta(minutes=30))
            rows = [dict(r) for r in conn.execute("SELECT * FROM bookings")]
            if self.used(point,events,rows,exclude=row["id"]) + row["patients"] > 2:
                conn.execute("UPDATE bookings SET state='conflict' WHERE id=?",(booking,))
                return self.store.one(booking,conn)
            # Persist the intent BEFORE calling events.insert, so process failure cannot lose it.
            conn.execute("UPDATE bookings SET state='creating',submitted=? WHERE id=?",(time.time(),booking))
        return self.finish_create(row)

    def recover(self):
        for row in self.store.rows(bot=self.bot):
            if row["state"] == "canceling":
                self.finish_cancel(row)
                continue
            if row["state"] not in {"creating","uncertain"} or time.time()-(row["submitted"] or row["created"]) < 60:
                continue
            try:
                event = self.google.event(row["event_id"])
                self.verify_event(event,row)
                self.store.confirm(row["id"])
            except ApiError as exc:
                if exc.status == 404 and dt(row["start"]) > self.clock():
                    self.finish_create(row)  # Stable event ID makes retries idempotent.
                elif exc.status in {404,410}:
                    self.store.create_error(row["id"],False)
            except Exception:
                pass  # Keep the hold. Never claim a successful booking without read-back.

    def own(self, identity):
        result=[]
        for row in self.store.rows(bot=self.bot):
            if (row["user"],row["chat"]) == (identity["user_id"],identity["chat_id"]) and row["state"] in {
                    "creating","uncertain","confirmed","canceling"}:
                state,start=row["state"],row["start"]
                if state=="confirmed":
                    try:
                        event=self.google.event(row["event_id"])
                        if event.get("status")=="cancelled":
                            state="cancelada_en_calendar"
                        else:
                            a,b=event_range(event)
                            if a!=dt(start) or b!=a+timedelta(minutes=30):
                                state,start="modificada_en_calendar",a.isoformat()
                    except ApiError as exc:
                        state="cancelada_en_calendar" if exc.status in {404,410} else "verificacion_no_disponible"
                result.append({"codigo":row["id"],"inicio":start,"paciente":row["contact"],
                               "cedula":masked_cedula(row["cedula"]),"modalidad":row["modalidad"] or "laboratorio",
                               "estado":"pendiente_confirmacion_asesor" if state in {"confirmed","modificada_en_calendar"} and row["modalidad"]=="domicilio" else state})
        return result[-10:]

    def cancel_proposal(self, identity, booking):
        row=self.store.one(booking)
        self.store.owner(row,identity,self.bot)
        if row["state"] != "confirmed":
            raise ValueError("Solo se pueden cancelar citas confirmadas por este flujo")
        return row,self.store.cancellation(booking,identity.get("thread_id"))

    def cancel(self, token, identity, anchor, thread):
        with self.store.transaction() as conn:
            intent=conn.execute("SELECT * FROM cancellations WHERE token=?",(token,)).fetchone()
            if not intent:
                raise ValueError("Confirmacion no encontrada")
            row=self.store.one(intent["booking"],conn)
            self.store.owner(row,identity,self.bot)
            if intent["anchor"] != anchor or intent["thread"] != thread:
                raise PermissionError("Boton de cancelacion no valido")
            if intent["state"] == "done":
                return row
            if intent["state"] != "proposed":
                raise ValueError("La solicitud fue cerrada")
            if intent["expires"] < time.time():
                raise ValueError("La confirmacion vencio")
            if row["state"] not in {"confirmed","canceling"}:
                raise ValueError("La cita no esta confirmada")
            conn.execute("UPDATE bookings SET state='canceling' WHERE id=?",(row["id"],))
        return self.finish_cancel(row)

    def finish_cancel(self, row):
        try:
            try:
                existing=self.google.event(row["event_id"])
            except ApiError as exc:
                if exc.status not in {404,410}:
                    raise
                existing=None
            if existing and existing.get("status")!="cancelled":
                if existing.get("extendedProperties",{}).get("private",{}).get("corelab_booking")!=row["id"]:
                    return self.store.one(row["id"])  # External replacement requires staff review.
                self.google.delete(row["event_id"])
            try:
                event=self.google.event(row["event_id"])
                if event.get("status") != "cancelled":
                    return self.store.one(row["id"])
            except ApiError as exc:
                if exc.status not in {404,410}:
                    raise
            with self.store.transaction() as conn:
                conn.execute("UPDATE bookings SET state='cancelled',notify='pending',customer_notice='pending' "
                             "WHERE id=? AND state='canceling'",(row["id"],))
                conn.execute("UPDATE cancellations SET state='done' WHERE booking=?",(row["id"],))
        except ApiError:
            pass  # Cancellation remains pending, capacity is still blocked by the remote event.
        return self.store.one(row["id"])
