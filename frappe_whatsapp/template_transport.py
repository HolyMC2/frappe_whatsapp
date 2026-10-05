"""Runtime inputs for template sends; business apps own token signing and routing."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

import frappe
from frappe import _


def quick_reply_payloads(raw, buttons, *, offset=0):
    """Map final Meta button indices to exact, caller-supplied opaque payloads.

    Empty/default JSON preserves legacy labels. Overrides are a JSON array of
    {"index": 0, "payload": "doc:<token>"}; partial overrides are allowed.
    """
    try:
        overrides = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        frappe.throw(_("Los botones del envío no tienen un formato válido. Vuelve a abrir Enviar y selecciona los botones."))
    if overrides is None or overrides == {} or overrides == []:
        return {}
    reason = _("Los botones no coinciden con la plantilla o tienen una respuesta vacía. Vuelve a abrir Enviar y selecciona los botones de esta plantilla.")
    if not isinstance(overrides, list):
        frappe.throw(reason)
    allowed = {str(index + offset) for index, button in enumerate(buttons) if button.button_type == "Quick Reply"}
    payloads = {}
    for override in overrides:
        if not isinstance(override, dict) or set(override) != {"index", "payload"}:
            frappe.throw(reason)
        index, payload = override["index"], override["payload"]
        if isinstance(index, bool) or not isinstance(index, (str, int)) or str(index) not in allowed or str(index) in payloads:
            frappe.throw(reason)
        if not isinstance(payload, str) or not payload.strip() or len(payload) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in payload):
            frappe.throw(_("La respuesta del botón está vacía o supera 128 caracteres. Vuelve a abrir Enviar para generar un botón válido."))
        payloads[str(index)] = payload
    return payloads


def document_source(attach, filename, maximum):
    """Validate a PDF header before upload; never expose a private file as a link.

    Site files upload by id. External HTTPS links are kept without fetching them
    from our server; their availability and MIME type are the caller's responsibility.
    """
    if not isinstance(attach, str) or not attach.strip():
        frappe.throw(_("Esta plantilla necesita un PDF adjunto. Adjunta el documento en Enviar y vuelve a intentar."))
    try:
        source = urlsplit(attach)
        site = urlsplit(frappe.utils.get_url())
        if source.scheme and (source.scheme != "https" or not source.hostname or source.username or source.password or source.port not in (None, 443)):
            raise ValueError
        if (source.fragment or (source.netloc and not source.scheme)
                or attach != attach.strip() or len(attach) > 4096
                or (source.scheme and any(c.isspace() for c in attach))
                or any(c.isspace() for c in source.query)
                or any(ord(c) < 32 or ord(c) == 127 for c in attach)):
            raise ValueError
    except ValueError:
        frappe.throw(_("El enlace del PDF no es válido. Adjunta el archivo o usa un enlace HTTPS accesible y vuelve a enviar."))

    path = unquote(source.path)
    same_site = not source.scheme or source.netloc == site.netloc
    private = same_site and path.startswith("/private/")
    local_attach = None
    local_path = None
    if same_site and (path.startswith("/files/") or path.startswith("/private/files/")):
        private = path.startswith("/private/files/")
        prefix = "/private/files/" if private else "/files/"
        base = Path(frappe.get_site_path("private" if private else "public", "files")).resolve()
        local_path = (base / path[len(prefix):]).resolve()
        if not local_path.is_relative_to(base) or not local_path.is_file():
            frappe.throw(_("El PDF adjunto ya no está disponible. Adjunta de nuevo el documento en Enviar."))
        local_attach = prefix + str(local_path.relative_to(base))
    elif not source.scheme:
        # Signed print endpoints remain link sends, like legacy notifications.
        if not path.startswith("/api/"):
            frappe.throw(_("No se reconoce el PDF adjunto. Adjunta de nuevo el documento en Enviar."))
    if private and not local_path:
        frappe.throw(_("El PDF privado no está disponible para cargarlo. Adjunta de nuevo el documento en Enviar."))

    if not filename and local_path:
        filename = frappe.db.get_value("File", {"file_url": ["in", [attach, local_attach]]}, "file_name")
    filename = filename or Path(path).name
    if not isinstance(filename, str) or not filename.lower().endswith(".pdf") or len(filename) > 255 or any(c in filename for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        frappe.throw(_("La plantilla de documento necesita un archivo PDF con nombre. Adjunta un PDF o indica su nombre terminado en .pdf y vuelve a enviar."))
    if local_path:
        try:
            size = local_path.stat().st_size
            with local_path.open("rb") as stream:
                is_pdf = stream.read(5) == b"%PDF-"
        except OSError:
            frappe.throw(_("No se pudo leer el PDF adjunto. Adjunta de nuevo el documento en Enviar."))
        if not is_pdf:
            frappe.throw(_("El archivo adjunto no es un PDF válido. Adjunta el documento en PDF y vuelve a enviar."))
        if size > maximum:
            frappe.throw(_("El PDF supera el máximo de 100 MB de WhatsApp. Adjunta una versión más pequeña y vuelve a enviar."))
    # Encode file paths without changing signed query parameters.
    if local_path:
        link = frappe.utils.get_url().rstrip("/") + quote(path, safe="/")
        if source.query:
            link += "?" + source.query
    else:
        link = attach if source.scheme else frappe.utils.get_url().rstrip("/") + attach
    return SimpleNamespace(local_attach=local_attach, filename=filename, private=private, link=link)
