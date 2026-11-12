"""Patient data format checks; they do not verify identity or phone ownership."""
import re
import unicodedata


def appointment_description(value):
    if (not isinstance(value, str) or not 3 <= len(value.strip()) <= 300 or
            any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in value) or
            not any(c.isalnum() for c in value)):
        raise ValueError("Indica una breve descripcion de la cita en una linea (3 a 300 caracteres), sin resultados clinicos ni diagnosticos")
    return value.strip()


def email_address(value):
    if (not isinstance(value, str) or len(value) > 254 or
            any(unicodedata.category(c) in {"Cc", "Cf"} for c in value)):
        raise ValueError("Indica un correo electronico valido del paciente, sin nombres ni saltos de linea")
    value = value.strip()
    if value.count("@") != 1:
        raise ValueError("Indica el correo electronico del paciente, por ejemplo paciente@example.com")
    local, domain = value.split("@")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Revisa el dominio del correo electronico") from None
    if (not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]{1,64}", local) or
            local.startswith(".") or local.endswith(".") or ".." in local or
            len(local+"@"+domain) > 254 or "." not in domain or
            any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in domain.split(".")) or domain.rsplit(".", 1)[-1].isdigit()):
        raise ValueError("Indica un correo electronico valido del paciente, sin nombres ni espacios")
    return local+"@"+domain


def patient_details(contact, cedula, telefono, correo=None):
    if (not isinstance(contact, str) or not 2 <= len(contact.strip()) <= 100 or
            any(unicodedata.category(c) in {"Cc", "Cf"} for c in contact)):
        raise ValueError("Indica el nombre del unico paciente, de 2 a 100 caracteres")
    if (not isinstance(cedula, str) or any(ord(c) < 32 for c in cedula) or
            not re.fullmatch(r"[0-9]{10}", cedula.strip()) or
            len(set(cedula.strip())) == 1):
        raise ValueError("Indica la cedula del paciente con 10 digitos. No inventes ni completes numeros")
    if (not isinstance(telefono, str) or len(telefono) > 40 or any(ord(c) < 32 for c in telefono) or
            not re.fullmatch(r"[+0-9 ()-]+", telefono.strip())):
        raise ValueError("Indica un telefono de contacto valido con codigo de pais o formato ecuatoriano")
    phone = re.sub(r"[ ()-]", "", telefono.strip())
    if re.fullmatch(r"09[0-9]{8}|0[2-7][0-9]{7}", phone):
        phone = "+593" + phone[1:]
    elif re.fullmatch(r"593(?:9[0-9]{8}|[2-7][0-9]{7})", phone):
        phone = "+" + phone
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ValueError("Indica un telefono de contacto valido con codigo de pais o formato ecuatoriano")
    if phone.startswith("+593") and not re.fullmatch(r"\+593(?:9[0-9]{8}|[2-7][0-9]{7})", phone):
        raise ValueError("Revisa la longitud y el prefijo del telefono ecuatoriano")
    return contact.strip(), cedula.strip(), phone, email_address(correo)


def masked_cedula(value):
    return "******" + value[-4:] if value else "No registrada"


def visit_details(modalidad, direccion=None):
    if modalidad not in ("laboratorio", "domicilio"):
        raise ValueError("Indica si la cita es en el laboratorio o a domicilio")
    if modalidad == "laboratorio":
        if direccion is not None:
            raise ValueError("La direccion solo se solicita para una visita a domicilio")
        return modalidad, None
    if (not isinstance(direccion, str) or not 5 <= len(direccion.strip()) <= 300 or
            any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in direccion) or
            not any(c.isalnum() for c in direccion)):
        raise ValueError("Indica la direccion de la visita en una linea (5 a 300 caracteres)")
    return modalidad, direccion.strip()
