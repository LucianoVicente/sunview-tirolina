"""Junta el país elegido en el Sheets con el teléfono que escribe el cliente.

En la hoja, el cliente elige su país en un desplegable ("🇬🇧 United Kingdom
(+44)") y escribe el número a su manera: con o sin prefijo, con el 0 nacional
delante, con espacios... Aquí se convierte todo al formato internacional en
dígitos que espera WhatsApp (ej. "447911123456").

La validación la hace `phonenumbers` (la librería de Google). Si no está
instalada en el PC, se aplican reglas simples para no dejar la app sin
WhatsApp.
"""

import re

# Sin país elegido (hojas antiguas o celda vacía) se asume España: la mayoría
# de números sin prefijo de las hojas son móviles españoles de 9 dígitos.
REGION_DEFECTO = "ES"

# "(+44)" dentro del texto de la opción del desplegable.
_RE_PREFIJO = re.compile(r"\+\s*(\d{1,4})")


def prefijo_de_opcion(pais):
    """Prefijo en dígitos de una opción del desplegable ("" si no lleva)."""
    m = _RE_PREFIJO.search(pais or "")
    return m.group(1) if m else ""


def _validar(digitos, prefijo):
    """Número internacional en dígitos si phonenumbers lo da por válido; si no, ""."""
    try:
        import phonenumbers
    except ImportError:
        return ""
    region = (phonenumbers.region_code_for_country_code(int(prefijo))
              if prefijo else REGION_DEFECTO)
    try:
        numero = phonenumbers.parse(digitos, region)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(numero):
        return ""
    e164 = phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)
    return e164.lstrip("+")


def componer_telefono(pais, telefono):
    """Teléfono en formato internacional (solo dígitos) o "" si no hay número.

    Un número escrito con "+" o "00" delante manda sobre el país elegido: el
    cliente ya puso el prefijo correcto. Si no, se usa el prefijo del país
    (o España si no eligió ninguno), quitando el 0 nacional y sin duplicar
    el prefijo si ya lo escribió.
    """
    crudo = (telefono or "").strip()
    digitos = "".join(ch for ch in crudo if ch.isdigit())
    if not digitos:
        return ""
    if crudo.startswith("+"):
        return digitos
    if digitos.startswith("00"):
        return digitos[2:]

    prefijo = prefijo_de_opcion(pais)
    validado = _validar(digitos, prefijo)
    if validado:
        return validado
    # Número que la librería no reconoce: lo más probable sigue siendo
    # prefijo + número nacional sin su 0 inicial.
    if prefijo and not digitos.startswith(prefijo):
        return prefijo + digitos.lstrip("0")
    return digitos
