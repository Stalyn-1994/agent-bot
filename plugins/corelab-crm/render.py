"""Portable vector PDF + self-contained HTML, with no runtime PDF dependencies."""
from datetime import datetime, timedelta
import html
import re
import textwrap

NAVY = (0.06, 0.15, 0.23)
TEAL = (0.00, 0.47, 0.48)
GRAY = (0.35, 0.40, 0.44)
LIGHT = (0.94, 0.97, 0.97)
WHITE = (1, 1, 1)


def spanish(text):
    words = {"atencion":"atención", "conversacion":"conversación",
        "preparacion":"preparación", "ubicacion":"ubicación", "diagnosticos":"diagnósticos",
        "informacion":"información", "cotizacion":"cotización",
        "interpretacion":"interpretación", "revision":"revisión", "clasificacion":"clasificación",
        "operacion":"operación", "operativa":"operativa",
        "graficos":"gráficos", "cedulas":"cédulas", "telefonos":"teléfonos", "clinicos":"clínicos",
        "clinica":"clínica", "seccion":"sección", "senales":"señales", "accion":"acción",
        "variacion":"variación", "insatisfaccion":"insatisfacción", "asignacion":"asignación",
        "mas":"más", "dia":"día", "dias":"días", "codigo":"código", "codigos":"códigos",
        "documentacion":"documentación", "confirmacion":"confirmación", "economico":"económico",
        "economicos":"económicos", "autentificacion":"autentificación", "automaticas":"automáticas",
        "automatica":"automática", "autorizacion":"autorización", "generacion":"generación",
        "friccion":"fricción", "interes":"interés", "imagenes":"imágenes", "envio":"envío",
        "periodo":"período", "periodos":"períodos", "estadisticos":"estadísticos", "demostracion":"demostración",
        "examenes":"exámenes", "sabado":"sábado", "miercoles":"miércoles", "ficticios":"ficticios",
        "analisis":"análisis", "publico":"público", "validacion":"validación", "causalidad":"causalidad"}
    def replace(match):
        raw=match.group(0); result=words.get(raw.lower(),raw)
        return result.upper() if raw.isupper() else result.capitalize() if raw.istitle() else result
    return re.sub(r"[A-Za-z]+", replace, str(text))


def value(n):
    return "N/D" if n is None else str(n)


def period(r):
    a = datetime.fromisoformat(r["period_start"])
    b = datetime.fromisoformat(r["period_end_exclusive"]) - timedelta(days=1)
    return a.strftime("%d/%m/%Y") + " al " + b.strftime("%d/%m/%Y")


def estimate(text, size):
    return sum(0.87 if c in "WM@%" else 0.28 if c in "ilI.,:;!|' " else 0.56 for c in text) * size


def lines(text, width, size):
    result, current = [], ""
    for word in str(text).split():
        if estimate((current + " " + word).strip(), size) > width and current:
            result.append(current)
            current = ""
        if estimate(word, size) > width:
            # Fixed IDs and other long tokens may not wrap at whitespace.
            for char in word:
                if estimate(current + char, size) > width:
                    result.append(current)
                    current = ""
                current += char
        else:
            current = (current + " " + word).strip()
    if current:
        result.append(current)
    return result


class PDF:
    def __init__(self, report):
        self.report, self.pages, self.ops = report, [], []

    def rect(self, x, y, w, h, color):
        self.ops.append("%s rg %.2f %.2f %.2f %.2f re f" % (" ".join(map(str, color)), x, 842-y-h, w, h))

    def text(self, x, y, text, size=10, color=NAVY, bold=False):
        escaped = spanish(text).encode("cp1252", "replace").decode("latin1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append("BT /%s %.2f Tf %s rg 1 0 0 1 %.2f %.2f Tm (%s) Tj ET" % (
            "F2" if bold else "F1", size, " ".join(map(str, color)), x, 842-y-size, escaped))

    def para(self, x, y, text, width=510, size=10, color=GRAY, leading=None, maximum=None):
        rows = lines(text, width, size)
        if maximum and len(rows) > maximum:
            rows = rows[:maximum]
            rows[-1] = rows[-1].rstrip(".") + "..."
        leading = leading or size * 1.4
        for row in rows:
            self.text(x, y, row, size, color)
            y += leading
        return y

    def new_page(self, section):
        if self.ops:
            self.pages.append("\n".join(self.ops).encode("latin1"))
        self.ops = []
        self.rect(0, 0, 595, 99, NAVY)
        self.text(38, 23, "CORELAB", 14, WHITE, True)
        self.text(38, 47, section, 22, WHITE, True)
        self.text(38, 77, "Reporte gerencial semanal | " + period(self.report), 9, (0.72, 0.85, 0.86))
        self.rect(38, 807, 519, 1, (0.8, 0.86, 0.87))
        self.text(38, 816, "Uso interno | Indicadores, no diagnosticos | Ecuador UTC-5", 8, GRAY)
        self.text(512, 816, str(len(self.pages) + 1) + " / 4", 8, GRAY)
        if self.report.get("demo"):
            self.text(400, 25, "DATOS FICTICIOS", 10, (0.99, 0.78, 0.45), True)

    def bars(self, x, y, pairs, width=510, height=20, size=9):
        maximum = max([n for _, n in pairs] + [1])
        label_width = min(205, width * 0.44)
        for name, count in pairs:
            self.text(x, y+2, name, size)
            self.rect(x+label_width, y+2, width-label_width-40, 12, LIGHT)
            self.rect(x+label_width, y+2, (width-label_width-40)*count/maximum, 12, TEAL)
            self.text(x+width-27, y, count, size, NAVY, True)
            y += height
        return y

    def build(self):
        r, m, o = self.report, self.report["metrics"], self.report["operations"]
        self.new_page("Resumen de atencion")
        cards = [("Clientes identificados", m["customers"]), ("Mensajes de clientes", m["messages"]),
                 ("Citas confirmadas", o["bookings_confirmed"]), ("Casos humanos creados", o["cases_created"])]
        for i, (label, number) in enumerate(cards):
            x = 38 + i*132
            self.rect(x, 122, 123, 80, LIGHT)
            self.text(x+11, 133, value(number), 27, TEAL, True)
            self.para(x+11, 173, label, 105, 8, maximum=2)
        self.text(38, 222, "Lectura ejecutiva", 14, NAVY, True)
        prior = r["previous_week"]["messages"]
        comparison = (f"Semana anterior: {prior} mensajes; variacion {(m['messages']-prior)*100/prior:+.1f}%." if prior else
                      "La semana anterior no ofrece una base distinta de cero para calcular variacion porcentual.")
        self.para(38, 246, f"Se registraron {m['conversations']} episodios de conversacion y {m['repeat_customers_in_week']} clientes con actividad en mas de un dia. " + comparison, 519, 10)
        self.text(38, 306, "Mensajes por dia", 13, NAVY, True)
        ymax = max(m["daily_messages"] + [1])
        for i, (label, count) in enumerate(zip(["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"], m["daily_messages"])):
            x = 47 + i*73
            self.rect(x, 345, 45, 102, LIGHT)
            self.rect(x, 447 - count/ymax*102, 45, count/ymax*102, TEAL)
            self.text(x+12, 324, str(count), 10, TEAL, True)
            self.text(x+10, 456, label, 9)
        self.text(38, 496, "Ocho temas principales de los mensajes", 13, NAVY, True)
        top = list(m["topics"].items())[:8]
        if top:
            self.bars(38, 523, top, height=24)
        else:
            self.para(38, 530, "No hay mensajes clasificables en el periodo.")
        self.para(38, 741, "Un mensaje puede tener varios temas. Clasificacion por palabras y expresiones: requiere revision humana. Las citas se cuentan por fecha de confirmacion, aunque luego se hayan cancelado.", 519, 8)

        self.new_page("En qué poner énfasis")
        y = 121
        for index, item in enumerate(r["priorities"]):
            self.rect(38, y, 519, 96, LIGHT)
            self.text(49, y+10, item["priority"].upper() + " | " + item["focus"], 10, TEAL, True)
            self.para(49, y+29, "Evidencia: " + item["evidence"], 494, 9, maximum=2)
            self.para(49, y+56, "Accion: " + item["action"], 494, 9, maximum=3)
            y += 106
        self.para(38, 767, "Interpretacion automatica basada en reglas y conteos. No prueba causalidad, satisfaccion, gravedad clinica, ventas o abandono.", 519, 8)

        self.new_page("Seguimiento de clientes")
        self.para(38, 121, "Vista seudonimizada de las primeras 16 cuentas, priorizando casos abiertos y expresiones de insatisfaccion. El HTML y el CSV locales incluyen la lista completa. Los codigos no permiten contactar al cliente por si solos.", 519, 10)
        self.rect(38, 187, 519, 25, NAVY)
        for x, label in [(46, "Cliente"), (151, "Msgs"), (190, "Temas / senales"), (451, "Citas"), (498, "Caso")]:
            self.text(x, 194, label, 9, WHITE, True)
        y = 216
        for i, client in enumerate(r["clients"][:16]):
            self.rect(38, y, 519, 30, LIGHT if i % 2 == 0 else WHITE)
            self.text(46, y+8, client["client"], 8)
            self.text(156, y+8, client["messages"], 8)
            label = ", ".join(client["topics"][:2])
            if "insatisfaccion" in client["signals"]:
                label = "Revisar posible queja; " + label
            self.para(190, y+3, label, 250, 8, leading=10, maximum=2)
            self.text(460, y+8, value(client["confirmed_bookings"]), 8)
            self.text(498, y+8, "Abierto" if client["open_case"] else "N/D" if client["open_case"] is None else "-", 8)
            y += 31
        if not r["clients"]:
            self.para(46, 240, "Sin clientes identificables en el periodo.")
        self.para(38, 747, "Los casos abiertos son el estado al generar el documento. No se adjuntan mensajes literales, nombres, IDs de Telegram, cedulas, telefonos ni resultados clinicos.", 519, 9)

        self.new_page("Fuentes y alcance del informe")
        y = 123
        sections = [
            ("Qué se mide", "Mensajes privados de cuentas de clientes; comandos y asesores excluidos. Un cliente equivale a una cuenta, no a una persona verificada. Un episodio nuevo comienza tras mas de 30 minutos sin mensajes del cliente. Las respuestas IA son textos finales registrados en Hermes, no acuses de entrega de Telegram."),
            ("Qué aportan los plugins", "Las citas confirmadas provienen del registro de citas, por fecha de confirmacion; no significan asistencia ni ingreso economico. Los casos humanos provienen del registro de derivaciones. N/D significa fuente no disponible o esquema no reconocido; no equivale a cero."),
            ("Lectura del comportamiento", "Los temas, quejas, urgencia e interes comercial son coincidencias textuales explicables. Una expresion puede ser una negacion o una cita. No se aplican diagnosticos emocionales, inferencias medicas ni un modelo externo a las conversaciones. Los consejos son acciones de revision respaldadas por los conteos."),
            ("Cobertura y limites", "state.db es el historial de Hermes. El nuevo registro CRM agrega indicadores de mensajes entrantes, incluidos los recibidos durante atencion humana; no copia su texto ni garantiza registrar respuestas manuales, botones, ediciones o mensajes borrados. La cobertura historica depende de los registros conservados. No se calcula un tiempo real de respuesta a partir de fechas de persistencia."),
        ]
        for title, text in sections:
            self.text(38, y, title, 12, TEAL, True)
            y = self.para(38, y+24, text, 519, 10) + 21
        self.text(38, y, "Calidad de datos", 12, TEAL, True)
        warnings = r["warnings"] or ["No se detectaron advertencias automaticas; esto no certifica que el historial sea completo."]
        y = self.para(38, y+24, " ".join(warnings), 519, 9, maximum=8) + 18
        self.para(38, y, "Generado: " + r["generated_at"] + " | Clasificador: " + r["classifier"] + ". Uso interno de Corelab; revisar tratamiento de datos y accesos antes de produccion.", 519, 8)
        self.pages.append("\n".join(self.ops).encode("latin1"))
        return self.encode()

    def encode(self):
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
        page_ids = []
        for stream in self.pages:
            page_id, stream_id = len(objects)+1, len(objects)+2
            page_ids.append(page_id)
            objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {stream_id} 0 R >>").encode())
            objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        objects[1] = ("<< /Type /Pages /Kids [" + " ".join(str(x)+" 0 R" for x in page_ids) + "] /Count " + str(len(page_ids)) + " >>").encode()
        data, offsets = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(i).encode()+b" 0 obj\n"+obj+b"\nendobj\n"
        xref = len(data)
        data += ("xref\n0 " + str(len(objects)+1)+"\n0000000000 65535 f \n").encode()
        data += b"".join((f"{x:010} 00000 n \n").encode() for x in offsets[1:])
        return data + ("trailer\n<< /Size "+str(len(objects)+1)+" /Root 1 0 R >>\nstartxref\n"+str(xref)+"\n%%EOF\n").encode()


def html_report(r):
    esc = lambda x: html.escape(spanish(x))
    m, o = r["metrics"], r["operations"]
    cards = "".join(f"<div class='card'><strong>{esc(value(n))}</strong><span>{esc(label)}</span></div>" for label, n in [
        ("Clientes", m["customers"]), ("Mensajes", m["messages"]), ("Citas confirmadas", o["bookings_confirmed"]), ("Casos humanos", o["cases_created"])])
    topmax = max(list(m["topics"].values())+[1])
    topics = "".join(f"<div class='bar'><span>{esc(name)}</span><meter min='0' max='{topmax}' value='{count}'></meter><b>{count}</b></div>" for name, count in m["topics"].items())
    priorities_html = "".join(f"<article><b>{esc(p['priority'])} | {esc(p['focus'])}</b><p>{esc(p['evidence'])}</p><p><strong>Accion:</strong> {esc(p['action'])}</p></article>" for p in r["priorities"])
    clients = "".join("<tr>"+"".join("<td>"+esc(v)+"</td>" for v in [c["client"], c["messages"], ", ".join(c["topics"]), ", ".join(c["signals"]) or "-", value(c["confirmed_bookings"]), "Abierto" if c["open_case"] else "N/D" if c["open_case"] is None else "-"])+"</tr>" for c in r["clients"])
    daymax = max(m["daily_messages"]+[1])
    days = "".join(f"<div class='bar'><span>{day}</span><meter min='0' max='{daymax}' value='{n}'></meter><b>{n}</b></div>" for day, n in zip(["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"], m["daily_messages"]))
    warnings = "".join("<li>"+esc(w)+"</li>" for w in r["warnings"])
    demo = "<p class='notice'>DEMOSTRACION - DATOS FICTICIOS</p>" if r.get("demo") else ""
    return ("""<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Corelab - Informe CRM</title><style>body{font:15px/1.5 Arial,sans-serif;background:#f3f7f7;color:#132b3b;margin:0}main{max-width:1050px;margin:auto;padding:32px}header{background:#10283b;color:white;padding:24px;border-radius:14px}h1{margin:4px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0}.card,article,section{background:white;padding:20px;border-radius:12px;margin:12px 0}.card strong{display:block;font-size:32px;color:#007c7d}.card span{display:block}.bar{display:grid;grid-template-columns:210px 1fr 55px;align-items:center;gap:14px;margin:12px 0}meter{width:100%;height:22px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #dce7e7}.scroll{overflow:auto}.notice{padding:12px;background:#fff0d0;color:#5e4100}footer{color:#536571;font-size:12px}@media(max-width:700px){main{padding:12px}.cards{grid-template-columns:repeat(2,1fr)}.bar{grid-template-columns:130px 1fr 35px}}</style><main>"""
        + "<header><small>CORELAB | Uso interno</small><h1>Informe gerencial de atencion</h1><div>"+esc(period(r))+" | America/Guayaquil</div></header>"+demo+"<div class='cards'>"+cards+"</div>"
        + "<section><h2>Mensajes por dia</h2>"+days+"</section><section><h2>Temas detectados</h2>"+topics+"<p>Etiquetas multiples por mensaje. Coincidencias textuales, sujetas a revision.</p></section>"
        + "<h2>Prioridades gerenciales</h2>"+priorities_html+"<section><h2>Seguimiento por cuenta</h2><div class='scroll'><table><thead><tr><th>Cliente</th><th>Mensajes</th><th>Temas</th><th>Senales</th><th>Citas</th><th>Caso</th></tr></thead><tbody>"+clients+"</tbody></table></div></section>"
        + "<section><h2>Cobertura y limites</h2><ul>"+warnings+"</ul><p>Un cliente es una cuenta de Telegram. Episodios: mas de 30 minutos entre mensajes. Citas por fecha de confirmacion; casos abiertos al generar. Respuestas IA registradas no acreditan entrega. N/D no es cero. No se infieren ventas, abandono, satisfaccion o tiempos de respuesta.</p><p>Sin texto literal ni datos identificativos en las exportaciones. Clasificador local por reglas; sin consultas a un LLM.</p></section><footer>Generado: "+esc(r["generated_at"])+" | "+esc(r["classifier"])+"</footer></main></html>")
