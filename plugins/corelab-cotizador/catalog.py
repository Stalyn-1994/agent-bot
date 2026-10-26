"""Deterministic catalogue parsing and quotation rules for Corelab."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
import re
import unicodedata


def normalized(value: str) -> str:
    """Compare only cosmetic spelling differences; never infer clinical equivalence."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    # Dots only format abbreviations in the supplied price list (for example C.P.K.).
    text = text.upper().replace(".", "")
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split())


def money(value: str) -> Decimal | None:
    value = value.strip()
    if not value.startswith("$"):
        return None
    return Decimal(value[1:].replace(".", "").replace(",", "."))


@dataclass(frozen=True)
class Exam:
    name: str
    credit_price: Decimal | None
    delivery: str
    discount_price: Decimal | None
    page: str

    @property
    def key(self) -> str:
        return normalized(self.name)


class Catalog:
    def __init__(self, root: Path | None = None):
        root = root or Path(__file__).resolve().parent / "data"
        required = [root / f"catalogo_{index}.md" for index in range(1, 5)]
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("Faltan catalogos en " + str(root) + ": " + ", ".join(missing))
        rows: list[Exam] = []
        for path in required:
            parsed = self._parse(path)
            if not parsed:
                raise RuntimeError("El archivo no contiene una tabla de pruebas reconocible: " + str(path))
            rows.extend(parsed)
        if not rows:
            raise RuntimeError("No se encontro el catalogo de cotizacion de Corelab en " + str(root))
        self.rows = tuple(rows)
        self.by_key: dict[str, list[Exam]] = {}
        for row in self.rows:
            self.by_key.setdefault(row.key, []).append(row)

    @staticmethod
    def _parse(path: Path) -> list[Exam]:
        records = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [part.strip() for part in line.strip().strip("|").split("|")]
            if len(cells) != 5 or cells[0] == "Prueba (nombre en cartera)":
                continue
            records.append(Exam(cells[0], money(cells[1]), cells[2], money(cells[3]), cells[4]))
        return records

    @staticmethod
    def _same_service(candidates: list[Exam]) -> bool:
        return len({(item.credit_price, item.delivery, item.discount_price) for item in candidates}) == 1

    def exact(self, text: str) -> Exam | None:
        candidates = self.by_key.get(normalized(text), [])
        if not candidates or not self._same_service(candidates):
            return None
        return candidates[0]

    def fuzzy(self, text: str) -> tuple[Exam | None, float | None, list[str]]:
        """Return a textual >=80% match only if a near competitor does not exist."""
        query = normalized(text)
        if not query:
            return None, None, []
        by_key = {}
        for row in self.rows:
            score = SequenceMatcher(None, query, row.key).ratio()
            current = by_key.get(row.key)
            if current is None or score > current[0]:
                by_key[row.key] = (score, row)
        ranked = sorted(by_key.values(), key=lambda item: (item[0], item[1].name), reverse=True)
        eligible = [(score, row) for score, row in ranked if score >= 0.80]
        if not eligible:
            return None, None, self.suggestions(text)
        score, item = eligible[0]
        # A high score does not resolve a clinical ambiguity when another distinct
        # name is almost as similar. Keep it for a person to validate instead.
        if len(eligible) > 1 and eligible[1][0] >= score - 0.04:
            return None, None, [row.name for _score, row in eligible[:3]]
        return item, score, []

    def suggestions(self, text: str, limit: int = 3) -> list[str]:
        query = normalized(text)
        if not query:
            return []
        query_words = set(query.split())
        ranked = []
        seen = set()
        for row in self.rows:
            if row.name in seen:
                continue
            score = SequenceMatcher(None, query, row.key).ratio()
            overlap = len(query_words & set(row.key.split()))
            if score >= 0.56 or (overlap and score >= 0.42):
                ranked.append((score, overlap, row.name))
                seen.add(row.name)
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [item[2] for item in ranked[:limit]]

    @staticmethod
    def _display_amount(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"

    def quote(self, exams: list[str]) -> dict:
        quoted, pending, seen = [], [], set()
        subtotal = Decimal("0")
        for source in exams:
            raw = source.strip()
            item = self.exact(raw)
            match = "coincidencia_exacta"
            alternatives = []
            if item is None:
                item, score, alternatives = self.fuzzy(raw)
                if item is not None:
                    match = "similitud_textual_80_por_ciento"
            if item is None:
                pending.append({"solicitado": raw, "motivo": "no_encontrado_o_no_inequivoco",
                                "sugerencias": alternatives})
                continue
            if item.key in seen:
                pending.append({"solicitado": raw, "motivo": "posible_repetido",
                                "sugerencias": [item.name]})
                continue
            seen.add(item.key)
            if item.credit_price is None:
                pending.append({"solicitado": raw, "motivo": "precio_requiere_validacion",
                                "sugerencias": [item.name]})
                continue
            subtotal += item.credit_price
            quoted.append({"solicitado": raw, "prueba_en_cartera": item.name,
                           "precio_tarjeta_usd": self._display_amount(item.credit_price),
                           "tiempo_entrega_indicado": item.delivery, "pagina_origen": item.page,
                           "tipo_coincidencia": match})
        complete = not pending
        return {
            "success": True,
            "estado": "cotizacion_completa" if complete else "cotizacion_parcial_requiere_validacion",
            "moneda": "USD",
            "lineas_cotizadas": quoted,
            "subtotal_tarjeta_usd": self._display_amount(subtotal),
            "pendientes_validacion": pending,
            "mensaje_para_cliente": (
                "Los valores indicados corresponden al precio con tarjeta de credito de la cartera."
                if complete else
                "Hay examenes pendientes de validar con una persona de Corelab; no se incluyeron en el subtotal."
            ),
            "nota": "El precio con descuento no se cotiza automaticamente porque sus condiciones no constan en la cartera.",
        }
