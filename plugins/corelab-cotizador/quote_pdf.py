"""Generate a quotation PDF in memory with standard PDF fonts."""
NAVY = (0.06, 0.15, 0.23)
TEAL = (0.00, 0.47, 0.48)
GRAY = (0.35, 0.40, 0.44)
LIGHT = (0.94, 0.97, 0.97)
WHITE = (1, 1, 1)


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
    def __init__(self):
        self.pages, self.ops = [], []

    def rect(self, x, y, w, h, color):
        self.ops.append("%s rg %.2f %.2f %.2f %.2f re f" % (" ".join(map(str, color)), x, 842-y-h, w, h))

    def text(self, x, y, text, size=10, color=NAVY, bold=False):
        escaped = " ".join(str(text).split()).encode("cp1252", "replace").decode("latin1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
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


def render_quote(result, code, date):
    doc = PDF()
    y = 0

    def page():
        nonlocal y
        if doc.ops:
            doc.pages.append("\n".join(doc.ops).encode("latin1"))
            doc.ops = []
        doc.rect(0, 0, 595, 116, NAVY)
        doc.text(40, 23, "CORELAB", 13, WHITE, True)
        doc.text(40, 47, "Cotización de exámenes", 23, WHITE, True)
        doc.text(40, 85, f"{date}  |  Referencia {code}  |  Tarjeta de crédito · USD", 10, WHITE)
        doc.rect(40, 801, 515, 1, LIGHT)
        doc.text(40, 813, "Corelab Laboratorio Clínico", 9, GRAY)
        doc.text(494, 813, f"Página {len(doc.pages)+1}", 9, GRAY)
        y = 140

    def block(title, details):
        nonlocal y
        height = 17 + 16*len(lines(title, 477, 11)) + 14*len(lines(details, 477, 10))
        if y+height > 783:
            page()
        doc.rect(40, y, 515, height, LIGHT)
        next_y=doc.para(53, y+8, title, 477, 11, NAVY, 16)
        doc.para(53, next_y+1, details, 477, 10, GRAY, 14)
        y += height+8

    page()
    for row in result["lineas_cotizadas"]:
        block(row["prueba_en_cartera"],
              f'USD {row["precio_tarjeta_usd"]}  |  Plazo indicado: {row["tiempo_entrega_indicado"]}')
    pending=result["pendientes_validacion"]
    block(("Subtotal de exámenes cotizados" if pending else "Total con tarjeta") +
          f' · USD {result["subtotal_tarjeta_usd"]}',
          "Los plazos se reproducen tal como figuran en la cartera.")
    for item in pending:
        block(item["solicitado"], "Pendiente de validación con Corelab. No incluido en el subtotal.")
    doc.pages.append("\n".join(doc.ops).encode("latin1"))
    return doc.encode()
