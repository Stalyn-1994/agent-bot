"""Explainable text indicators, not clinical NLP or an emotional diagnosis."""
import re
import unicodedata

TOPICS = {
    "Precios y cotizaciones": ("precio", "precios", "cuesta", "cuanto vale", "cotiza", "cotizacion", "cotizar", "tarifa", "efectivo", "tarjeta", "descuento"),
    "Citas": ("cita", "citas", "agendar", "agenda", "reservar", "reserva", "reservacion", "cancelar cita", "reagendar", "cupo", "cupos"),
    "Preparacion": ("ayuno", "ayunas", "preparacion", "prepararme", "preparar", "comer antes", "tomar agua", "muestra", "muestras"),
    "Horarios": ("horario", "horarios", "abren", "cierran", "abierto", "sabado", "domingo"),
    "Ubicacion y contacto": ("ubicacion", "direccion", "donde quedan", "como llego", "telefono del laboratorio", "contacto"),
    "Resultados": ("resultado", "resultados", "entrega", "informe", "cuanto tarda", "demora"),
    "Atencion humana": ("asesor", "asesora", "persona", "humano", "humana", "operador", "personal"),
    "Servicios y examenes": ("examen", "examenes", "analisis", "hemograma", "glucosa", "tiroides", "perfil", "laboratorio", "prueba", "pruebas"),
}
SIGNALS = {
    "insatisfaccion": ("no me responden", "no responden", "mal servicio", "mala atencion", "pesimo", "molesto", "molesta", "queja", "reclamo", "sigo esperando", "no funciona", "no sirve"),
    "urgencia": ("urgente", "urgencia", "lo antes posible", "cuanto antes"),
    "interes_compra": ("quiero cotizar", "necesito cotizar", "quiero hacerme", "necesito hacerme", "quiero agendar", "quisiera agendar", "reservar una cita"),
    "cancelacion": ("cancelar", "cancela", "cancelacion", "reagendar"),
}
GAPS = ("no tengo informacion confirmada", "no puedo verificar", "no pude verificar", "no esta confirmado", "validar con nuestro personal", "validarlo con una persona")
SCHEMA = "corelab-text-indicators-v1"


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value).lower())
    return re.sub(r"\s+", " ", "".join(c for c in value if not unicodedata.combining(c))).strip()


def contains(text, term):
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def classify(text):
    text = normalized(text)[:30000]
    return {
        "topics": [name for name, terms in TOPICS.items() if any(contains(text, term) for term in terms)] or ["Otros o sin clasificar"],
        "signals": [name for name, terms in SIGNALS.items() if any(contains(text, term) for term in terms)],
        "knowledge_gap": any(term in text for term in GAPS),
    }
