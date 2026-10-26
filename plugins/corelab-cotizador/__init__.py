"""Native, deterministic quotation tools for Corelab's published catalogue."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from .catalog import Catalog

plugin_name = "corelab-cotizador"
plugin_version = "1.0.1"


def _request(args):
    if not isinstance(args, dict) or set(args) != {"examenes", "modalidad_pago"}:
        raise ValueError("La solicitud requiere la lista de examenes y la modalidad de pago")
    values = args["examenes"]
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError("Envia entre 1 y 20 examenes")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 160 for value in values):
        raise ValueError("Cada examen debe ser texto no vacio de hasta 160 caracteres")
    payment = args["modalidad_pago"]
    if payment not in {"tarjeta", "efectivo"}:
        raise ValueError("La modalidad de pago debe ser tarjeta o efectivo")
    return values, payment


def register(ctx):
    if not callable(getattr(ctx, "register_tool", None)):
        raise RuntimeError("Esta version de Hermes no admite herramientas de plugin")
    # The same Markdown catalogues indexed by RAG remain the sole runtime source.
    # The bundled copies exist for verification and reproducible package tests.
    # Doctor supplies a disposable HERMES_HOME containing only the plugin copy.
    # Register schemas without requiring live profile data. Resolve this profile
    # once and load its catalogue only when an actual quotation is requested.
    directory = Path(os.environ["HERMES_HOME"]).resolve() / "agent-knowledge"
    catalog = None
    lock = Lock()

    def get_catalog():
        nonlocal catalog
        with lock:
            if catalog is None:
                catalog = Catalog(directory)
            return catalog

    def quote(args, **_kwargs):
        try:
            exams, payment = _request(args)
            if payment == "efectivo":
                return json.dumps({
                    "success": True,
                    "estado": "cotizacion_requiere_validacion_efectivo",
                    "moneda": "USD",
                    "lineas_cotizadas": [],
                    "subtotal_tarjeta_usd": None,
                    "pendientes_validacion": [{"solicitado": item,
                                                "motivo": "precio_efectivo_no_publicado_en_cartera",
                                                "sugerencias": []} for item in exams],
                    "mensaje_para_cliente": "El valor en efectivo debe validarse con una persona de Corelab antes de cotizarlo.",
                    "nota": "La cartera recibida identifica precio de tarjeta y precio con descuento, pero no confirma que el descuento corresponda a efectivo.",
                }, ensure_ascii=False)
            return json.dumps(get_catalog().quote(exams), ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
        except (OSError, RuntimeError) as exc:
            # Customer tools must not expose Windows paths or internal exception text.
            return json.dumps({"success": False, "error": "No puedo verificar los precios del catalogo en este momento. Es mejor validarlo con una persona de Corelab."}, ensure_ascii=False)

    ctx.register_tool(
        name="cotizar_examenes",
        toolset="corelab_cotizador",
        handler=quote,
        schema={
            "name": "cotizar_examenes",
            "description": "Cotiza una lista de examenes con precio de tarjeta USD. Acepta coincidencia exacta o similitud textual de al menos 80%, salvo que dos nombres clinicamente distintos compitan de forma cercana. Para efectivo siempre deriva a validacion humana porque la cartera no publica ese precio.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"examenes": {"type": "array", "minItems": 1, "maxItems": 20,
                                                        "items": {"type": "string", "minLength": 1, "maxLength": 160},
                                                        "description": "Nombres de examenes tal como los indico el cliente"},
                                          "modalidad_pago": {"type": "string", "enum": ["tarjeta", "efectivo"],
                                                              "description": "Modalidad confirmada por el cliente"}},
                           "required": ["examenes", "modalidad_pago"]},
        },
    )
