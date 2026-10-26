"""Patient data format checks; they do not verify identity or phone ownership."""
import re


def patient_details(contact, cedula, telefono):
    if (not isinstance(contact, str) or not 2 <= len(contact.strip()) <= 100 or
            any(ord(c) < 32 for c in contact)):
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
    return contact.strip(), cedula.strip(), phone


def masked_cedula(value):
    return "******" + value[-4:] if value else "No registrada"
