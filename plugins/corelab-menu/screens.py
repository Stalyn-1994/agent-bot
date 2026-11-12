"""Finite presentation screens. No business data, bookings or consent are managed here."""
import re
import unicodedata
from .request_fields import patient_data_prompt

BACK = "⬅️ Volver"
HOME = "🏠 Menú principal"
NAV = ((BACK, HOME),)
SCREENS = {
    "principal": ("¡Hola! 👋 Soy CoreBot, el asistente virtual de Corelab. ¿En qué te ayudo hoy? Puedes tocar un botón o escribir directamente.",
        (("ℹ️ Información de Corelab",), ("💰 Cotización de exámenes",),
         ("📅 Agendamiento de citas",), ("👤 Contacta con un asesor",))),
    "informacion": ("¿Qué te gustaría conocer de Corelab?",
        (("🕐 Horarios", "📍 Ubicación"), ("🏢 Identidad y calidad", "🌐 Sitio web"))),
    "respuesta_info": (None, (("ℹ️ Más información",),)),
    "cotizacion": ("¿Qué exámenes necesitas cotizar? Puedes escribir los nombres o enviar una foto de tu orden médica.", ()),
    "agregar_examen": ("¿Deseas agregar otro examen?", (("➕ Agregar otro examen", "✅ Continuar cotización"),)),
    "otro_examen": ("¿Qué otro examen deseas agregar?", ()),
    "pago": ("Los precios con tarjeta y en efectivo pueden ser diferentes. ¿Cómo deseas pagar?",
        (("💳 Tarjeta", "💵 Efectivo"),)),
    "confirmar_examen": (None, (("✅ Sí, ese examen", "✏️ Corregir examen"),)),
    "pdf": (None, (("📄 Sí, generar PDF", "Sin PDF"),)),
    "fin_cotizacion": ("¿Cómo deseas continuar?",
        (("📅 Agendar cita con estos exámenes",), ("👤 Hablar con un asesor",))),
    "citas": ("¿Qué deseas hacer?",
        (("📅 Agendar", "🔄 Reagendar", "🗑 Cancelar"),)),
    "modalidad": ("¿Prefieres atenderte en el laboratorio o solicitar una visita a domicilio?",
        (("🏥 En el laboratorio", "🏠 A domicilio"),)),
    "datos_cita_laboratorio": (patient_data_prompt("laboratorio"), ()),
    "datos_cita_domicilio": (patient_data_prompt("domicilio"), ()),
    "asesor": ("¿Sobre qué tema necesitas ayuda?",
        (("Resultados", "Cotización"), ("Citas", "Otro"))),
    "detalle_asesor": ("Cuéntame brevemente en qué necesitas ayuda. No envíes resultados clínicos ni datos sensibles aquí.", ()),
    "continuar": (None, ()),
    "no_entendido": ("Quiero asegurarme de entenderte. ¿Buscas información, una cotización, una cita o ayuda de un asesor?",
        (("ℹ️ Información de Corelab", "💰 Cotización de exámenes"),
         ("📅 Agendamiento de citas", "👤 Contacta con un asesor"))),
}


def normal(text):
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def render(screen, message=None):
    if screen not in SCREENS:
        raise ValueError("Pantalla desconocida")
    default, rows = SCREENS[screen]
    if message is not None and (not isinstance(message, str) or not message.strip() or len(message)>3500):
        raise ValueError("Mensaje invalido")
    if default is not None and message is not None:
        raise ValueError("Esta pantalla usa su pregunta predefinida")
    text = message if message is not None else default
    if text is None:
        raise ValueError("Esta pantalla necesita un mensaje")
    if screen=="respuesta_info":
        text=text.rstrip()+"\n\n¿Quieres saber algo más?"
    if screen=="pdf":
        text=text.rstrip()+"\n\n¿Deseas que te genere esta cotización en PDF?"
    return text, rows+(NAV if screen!="principal" else ())


def intent(text, screen):
    """Exact normalized button/alias only; free text remains an ordinary agent request."""
    n=normal(text)
    global_routes={
        normal(HOME): "principal", normal(BACK): "volver",
        normal("ℹ️ Información de Corelab"): "informacion",
        normal("ℹ️ Información general"): "informacion",
        normal("ℹ️ Más información"): "informacion",
        normal("💰 Cotización de exámenes"): "cotizacion",
        normal("📅 Agendamiento de citas"): "citas",
        normal("👤 Contacta con un asesor"): "asesor",
        normal("👤 Hablar con un asesor"): "asesor",
    }
    if n in global_routes:
        return global_routes[n]
    if screen in {"informacion","respuesta_info"}:
        topics={"horarios":"informacion_horarios","ubicacion":"informacion_ubicacion",
                "identidad y calidad":"informacion_identidad","sitio web":"informacion_web"}
        if n in topics:return topics[n]
    if screen=="asesor" and n in {"resultados","cotizacion","citas","otro"}:
        return "tema_asesor_"+n
    choices={
        "citas":{"agendar":"agendar","reagendar":"reagendar","cancelar":"cancelar"},
        "modalidad":{"en el laboratorio":"modalidad_laboratorio","a domicilio":"modalidad_domicilio"},
        "pago":{"tarjeta":"pago_tarjeta","efectivo":"pago_efectivo"},
        "agregar_examen":{"agregar otro examen":"otro_examen","continuar cotizacion":"lista_completa"},
        "pdf":{"si generar pdf":"enviar_pdf","sin pdf":"descartar_pdf"},
        "fin_cotizacion":{"agendar cita con estos examenes":"agendar_desde_cotizacion"},
        "confirmar_examen":{"si ese examen":"confirmar_examen","corregir examen":"corregir_examen"},
    }
    if n in choices.get(screen,{}):
        return choices[screen][n]
    return None
