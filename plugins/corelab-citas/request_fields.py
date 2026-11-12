"""Presentation only: mandatory patient fields are still validated by the appointment service."""


def patient_data_prompt(modality=None):
    if modality not in {None, "laboratorio", "domicilio"}:
        raise ValueError("Modalidad no valida")
    text = ("Para registrar la cita, escríbeme en un solo mensaje:\n"
            "Nombre completo:\n"
            "Cédula:\n"
            "Correo:\n"
            "Celular:")
    if modality == "domicilio":
        text += "\nDirección (incluye ciudad y sector):"
    elif modality is None:
        text += "\nDirección (solo si la cita es a domicilio):"
    return text + "\nBreve descripción de la cita:\nNo incluyas resultados clínicos ni diagnósticos."
