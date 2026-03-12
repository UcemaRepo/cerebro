import os
import json
import time
import uuid
import re
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from openai import OpenAI
import openpyxl

# Google Sheets via Service Account
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# PDF
try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_AVAILABLE = True
    except ImportError:
        PDF_AVAILABLE = False

app = Flask(__name__)
CORS(app)
import os as _os_env
_os_env.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

HISTORY_FILE      = "conversations.json"
PERSONALITIES_FILE = "personalities.json"
EVENTS_FILE       = "events.json"
CHANNELS_FILE     = "channels.json"
DMS_FILE          = "dms.json"

user_presence       = {}
last_uploaded_text  = {}
last_uploaded_image = {}
AWAY_THRESHOLD      = 300

SHEETS_FILE   = "sheets.json"   # { username: { sheet_id, sheet_name } }
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT", "")  # JSON completo de la SA


# ── Persistencia genérica ───────────────────────────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_history():    return load_json(HISTORY_FILE, {})
def save_history(d):   save_json(HISTORY_FILE, d)
def load_channels():   return load_json(CHANNELS_FILE, {})
def save_channels(d):  save_json(CHANNELS_FILE, d)
def load_dms():        return load_json(DMS_FILE, {})
def save_dms(d):       save_json(DMS_FILE, d)
def load_personalities(): return load_json(PERSONALITIES_FILE, {})
def save_personalities(d): save_json(PERSONALITIES_FILE, d)
def load_sheets():   return load_json(SHEETS_FILE, {})
def save_sheets(d):  save_json(SHEETS_FILE, d)


# ── Eventos ──────────────────────────────────────────────────────────────
def load_events():   return load_json(EVENTS_FILE, {})
def save_events(d):  save_json(EVENTS_FILE, d)

def push_event(target_user, event_type, actor, **kwargs):
    events = load_events()
    if target_user not in events:
        events[target_user] = []
    entry = {"type": event_type, "actor": actor, "ts": time.time()}
    entry.update(kwargs)
    events[target_user].append(entry)
    events[target_user] = events[target_user][-100:]
    save_events(events)

def broadcast(users, event_type, actor, **kwargs):
    for u in users:
        push_event(u, event_type, actor, **kwargs)


# ── Personalidades ────────────────────────────────────────────────────────
PRESET_PERSONALITIES = {
    "normal":   "Eres un asistente útil, claro y amable.",
    "analyst":  "Eres un analista experto. Respondés con datos y razonamiento estructurado. Usás tablas comparativas cuando es útil.",
    "creative": "Eres un asistente creativo y disruptivo. Das ideas originales, fuera de lo convencional.",
    "strict":   "Eres un asistente estricto y conciso. Respondés solo lo necesario, sin rodeos.",
    "dev":      "Eres un desarrollador senior. Respondés con código limpio y buenas prácticas.",
    "coach":    "Eres un coach ejecutivo. Hacés preguntas poderosas y ayudás a estructurar objetivos.",
    "carga":    """Eres una IA especializada en completar hojas de cálculo de gestión universitaria. Seguís un flujo de trabajo paso a paso, nunca intentás hacer todo a la vez.

REGLAS GENERALES:
- Porcentaje de beca otorgado y pedido son lo mismo.
- El porcentaje de beca se resta a la cápita: 25% de beca → 0,75 de cápita.
- Si no tiene beca: 0% en porcentaje de beca y 1 en cápita.
- Si la carrera se cursa en el primer semestre: formato 2026SEM1 (año+semestre, sin espacios).
- Poner fecha de beca solo si tiene beca. Siempre poner fecha de admisión.
- En la columna de responsable de pago: colocar el MAIL del responsable, no el nombre.
- Descuento de matrícula: siempre 0% salvo que el usuario indique lo contrario.

SIGLAS DE CARRERAS:
Economía Empresarial → LIEM
Marketing → LIMA N
Abogacía → ABOG N
Ingeniería en Informática → ININF
Analítica de Negocios → LIAN
Ciencias Políticas → LICP N
Contador Público → CCP N
Relaciones Internacionales → LIRI N
Administración de Empresas → LIA
Finanzas → LIFI
Economía → LIE N
Negocios Digitales → LIND
Artes Liberales y Ciencias → BA
Actuario → ACTU
Ingeniería en Inteligencia Artificial → INIA

ARANCELES (hoja de facturación):
- Todas las carreras: $167.000
- ININF (Ingeniería en Informática): $135.000

FLUJO DE TRABAJO OBLIGATORIO — seguí estos pasos en orden, uno por vez:

PASO 1 — ENTENDER EL DOCUMENTO:
Leé el documento adjunto. Hacé un resumen breve de los datos del alumno que encontraste: nombre, carrera, beca, fecha de admisión, mail del responsable. Preguntá si falta algo antes de continuar. Esperá confirmación del usuario.

PASO 2 — COMPLETAR HOJA BASE:
Con los datos confirmados, completá la primera hoja vinculada (base de admisiones). Indicá exactamente qué valores vas a escribir en qué campos. Esperá confirmación antes de escribir.

PASO 3 — COMPLETAR HOJA DE FACTURACIÓN:
Tomá los datos necesarios del paso anterior y completá la hoja de facturación. Calculá cápita, arancel y demás campos según las reglas. Indicá los valores antes de escribir. Esperá confirmación.

IMPORTANTE: Nunca saltes pasos. Siempre esperá un "ok", "sí" o confirmación del usuario antes de pasar al siguiente paso o escribir en una hoja.""",
}

def get_personality_text(user, preset_key):
    custom = load_personalities().get(user, {}).get("custom", "").strip()
    return custom if custom else PRESET_PERSONALITIES.get(preset_key, PRESET_PERSONALITIES["normal"])


# ── Sistema rich ──────────────────────────────────────────────────────────
SYSTEM_RICH = """
Podés enriquecer tus respuestas con estas etiquetas cuando aporten valor:

Tabla:
<table>
Col1 | Col2 | Col3
Val1 | Val2 | Val3
</table>

Widget:
<widget title="Título">Contenido</widget>

Archivo descargable (solo .txt .csv .md .json .html — NUNCA .docx .xlsx .pptx):
<download filename="datos.csv">col1,col2
val1,val2</download>
"""


# ── Presencia ─────────────────────────────────────────────────────────────
def get_status(entry):
    if entry.get("status") == "offline":
        return "offline"
    return "away" if time.time() - entry.get("last_seen", 0) > AWAY_THRESHOLD else "online"


# ── Contexto cruzado de usuarios ──────────────────────────────────────────
def build_users_context(history, known_users):
    lines = ["=== Historial de otros usuarios ==="]
    for u in known_users:
        h = history.get(u, [])
        if not h: continue
        lines.append(f"\n--- {u} ---")
        for m in h[-3:]:
            lines.append(f"  [{u}]: {m['message']}")
            lines.append(f"  [Bot]: {m['reply']}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Chat personal (historial permanente) ─────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data    = request.json
    message = data.get("message")
    user    = data.get("user")
    preset  = data.get("personality", "normal")

    user_presence[user] = {"last_seen": time.time(), "status": "online"}
    history = load_history()
    if user not in history:
        history[user] = []

    msgs = [{"role": "system", "content": get_personality_text(user, preset) + "\n\n" + SYSTEM_RICH}]

    for h in history[user][-6:]:
        msgs.append({"role": "user",      "content": h["message"]})
        msgs.append({"role": "assistant", "content": h["reply"]})

    ctx = build_users_context(history, [u for u in history if u != user])
    if ctx:
        msgs.append({"role": "system", "content": ctx})

    files       = data.get("files", [])       # solo imágenes (base64)
    doc_context = data.get("doc_context", "")  # texto extraído de documentos

    # Contexto de documentos
    if doc_context:
        if len(doc_context) > 8000:
            doc_context = doc_context[:8000] + "\n\n[... documento truncado para ahorrar memoria ...]"
        msgs.append({"role": "system", "content": "El usuario adjuntó estos documentos:\n\n" + doc_context})

    # Imágenes
    image_files = [f for f in files if f.get("type", "").startswith("image/")]

    if image_files:
        content_parts = [{"type": "text", "text": message or " "}]
        for img in image_files:
            data_url = img.get("dataURL", "")
            if data_url:
                content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        msgs.append({"role": "user", "content": content_parts})
    else:
        msgs.append({"role": "user", "content": message or " "})

    # En modo carga siempre leer sheets; en otros modos solo si hay keywords relevantes
    is_carga = (preset == "carga")
    if not is_carga:
        SHEET_KEYWORDS = ("hoja", "sheet", "tabla", "fila", "columna", "celda", "agreg", "actualiz",
                          "modific", "escrib", "datos", "registro", "base", "excel", "planilla")
        msg_lower   = (message or "").lower()
        needs_sheet = any(kw in msg_lower for kw in SHEET_KEYWORDS)
    else:
        needs_sheet = True
    sheet_data = read_sheet(user) if needs_sheet else None
    if sheet_data:
        if len(sheet_data) > 4000:
            sheet_data = sheet_data[:4000] + "\n[... hoja truncada ...]"
        msgs.append({"role": "system", "content":
            f"El usuario tiene esta hoja de cálculo de Google Sheets conectada:\n{sheet_data}\n\n"
            "Si el usuario pide modificar la hoja, usá la herramienta 'edit_sheet'. "
            "Para escribir en un rango usá range_a1 (ej: 'Sheet1!A2:C2') y values (lista de listas). "
            "Para agregar una fila al final usá append=true."
        })

    # Herramientas de Sheets (solo si hay hoja conectada)
    tools = []
    if sheet_data:
        tools = [{
            "type": "function",
            "function": {
                "name": "edit_sheet",
                "description": "Modifica una hoja de Google Sheets del usuario. Usá esto cuando el usuario pida escribir, actualizar o agregar datos.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["write", "append"],
                            "description": "write: sobreescribe un rango. append: agrega fila al final."
                        },
                        "sheet_name": {
                            "type": "string",
                            "description": "Nombre de la hoja a modificar (tal como aparece en el contexto). Si hay una sola hoja activa, podés omitirlo."
                        },
                        "range_a1": {
                            "type": "string",
                            "description": "Rango A1 para 'write', ej: 'Sheet1!A2:D2'. No requerido para append."
                        },
                        "values": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                            "description": "Lista de filas, cada fila es lista de celdas. Ej: [['Nico', '100', 'Pendiente']]"
                        },
                        "summary": {
                            "type": "string",
                            "description": "Descripción breve de qué cambio estás haciendo."
                        }
                    },
                    "required": ["action", "values", "summary"]
                }
            }
        }]

    kwargs = {"model": "gpt-5.4", "messages": msgs, "max_tokens": 600 if is_carga else 1500}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    resp = client.chat.completions.create(**kwargs)
    msg_obj = resp.choices[0].message

    # Procesar tool call si el bot quiso editar la hoja
    tool_result_text = ""
    if hasattr(msg_obj, "tool_calls") and msg_obj.tool_calls:
        for tc in msg_obj.tool_calls:
            if tc.function.name == "edit_sheet":
                args = json.loads(tc.function.arguments)
                action  = args.get("action", "write")
                values  = args.get("values", [])
                summary = args.get("summary", "")

                sheet_target = args.get("sheet_name", "")
                if action == "append":
                    ok, result_msg = append_to_sheet(user, sheet_target, values)
                else:
                    range_a1 = args.get("range_a1", "Sheet1!A1")
                    ok, result_msg = write_to_sheet(user, sheet_target, range_a1, values)

                tool_result_text = result_msg

        # Segunda llamada con resultado del tool para que el bot responda
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg_obj.tool_calls
        ]})
        msgs.append({"role": "tool", "tool_call_id": msg_obj.tool_calls[0].id, "content": tool_result_text})
        resp2 = client.chat.completions.create(model="gpt-5.4", messages=msgs)
        reply = resp2.choices[0].message.content
    else:
        reply = msg_obj.content

    entry = {"message": message, "reply": reply, "actor": user}
    history[user].append(entry)
    save_history(history)

    push_event(user, "mirror_update", user, message=message, reply=reply)
    return jsonify({"reply": reply})


# ── Canales (permanentes, guardados en disco) ─────────────────────────────
@app.route("/channels", methods=["GET"])
def list_channels():
    return jsonify(load_channels())

@app.route("/channels", methods=["POST"])
def create_channel():
    data    = request.json
    user    = data.get("user")
    name    = data.get("name", "").strip() or f"canal-de-{user}"
    # Limpiar nombre: minúsculas, sin espacios
    slug    = name.lower().replace(" ", "-")
    channels = load_channels()

    # No duplicar slugs
    if slug in channels:
        return jsonify({"error": "Ya existe un canal con ese nombre"}), 400

    ai_enabled = data.get("ai_enabled", True)
    channels[slug] = {
        "id":         slug,
        "name":       name,
        "creator":    user,
        "created":    time.time(),
        "ai_enabled": ai_enabled,
        "messages":   [],
    }
    save_channels(channels)

    # Notificar a todos los usuarios online
    for u in user_presence:
        push_event(u, "channel_created", user, channel_id=slug, channel_name=name)

    return jsonify({"id": slug, "name": name})

@app.route("/channels/<channel_id>", methods=["GET"])
def get_channel(channel_id):
    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        return jsonify({"error": "Canal no existe"}), 404
    return jsonify(ch)

@app.route("/channels/<channel_id>", methods=["DELETE"])
def delete_channel(channel_id):
    data     = request.json or {}
    user     = data.get("user")
    channels = load_channels()
    ch       = channels.get(channel_id)

    if not ch:
        return jsonify({"error": "Canal no existe"}), 404
    if ch["creator"] != user:
        return jsonify({"error": "Solo el creador puede borrar el canal"}), 403

    del channels[channel_id]
    save_channels(channels)

    for u in user_presence:
        push_event(u, "channel_deleted", user, channel_id=channel_id)

    return jsonify({"status": "deleted"})

@app.route("/channels/<channel_id>/chat", methods=["POST"])
def channel_chat(channel_id):
    data    = request.json
    message = data.get("message")
    user    = data.get("user")
    preset  = data.get("personality", "normal")

    channels = load_channels()
    ch = channels.get(channel_id)
    if not ch:
        return jsonify({"error": "Canal no existe"}), 404

    user_presence[user] = {"last_seen": time.time(), "status": "online"}

    ai_enabled = ch.get("ai_enabled", True)

    if ai_enabled:
        personality_text = get_personality_text(user, preset)
        msgs = [
            {"role": "system", "content": personality_text + "\n\n" + SYSTEM_RICH},
            {"role": "system", "content": (
                f"Estás en el canal '#{ch['name']}'. "
                f"Es un canal compartido donde varios usuarios pueden participar. "
                f"Respondé al mensaje de '{user}' con contexto del historial del canal."
            )}
        ]
        for h in ch["messages"][-12:]:
            msgs.append({"role": "user",      "content": f"[{h['actor']}]: {h['message']}"})
            msgs.append({"role": "assistant", "content": h["reply"]})
        msgs.append({"role": "user", "content": f"[{user}]: {message}"})
        resp  = client.chat.completions.create(model="gpt-5.4", messages=msgs)
        reply = resp.choices[0].message.content
    else:
        reply = ""

    files = data.get("files", [])
    entry = {"actor": user, "message": message, "reply": reply, "files": files, "ts": time.time()}
    ch["messages"].append(entry)
    save_channels(channels)

    for u in user_presence:
        push_event(u, "channel_message", user,
                   channel_id=channel_id, message=message, reply=reply, files=files)

    return jsonify({"reply": reply})


# ── Polling de eventos ────────────────────────────────────────────────────
@app.route("/events/<user>")
def get_events(user):
    since  = float(request.args.get("since", 0))
    events = load_events()
    return jsonify([e for e in events.get(user, []) if e["ts"] > since])


# ── Personalidad ──────────────────────────────────────────────────────────
@app.route("/personality/<user>", methods=["GET"])
def get_personality(user):
    entry = load_personalities().get(user, {})
    return jsonify({"custom": entry.get("custom", ""), "presets": PRESET_PERSONALITIES})

@app.route("/personality/<user>", methods=["POST"])
def set_personality(user):
    data = request.json
    p = load_personalities()
    if user not in p: p[user] = {}
    p[user]["custom"] = data.get("custom", "").strip()
    save_personalities(p)
    return jsonify({"status": "ok"})

@app.route("/personality/<user>", methods=["DELETE"])
def clear_personality(user):
    p = load_personalities()
    if user in p: p[user]["custom"] = ""
    save_personalities(p)
    return jsonify({"status": "cleared"})


# ── Upload ────────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    user = request.form.get("user", "default")
    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", file.filename)
    file.save(path)

    last_uploaded_text[user] = ""
    last_uploaded_image[user] = None
    last_uploaded_text[user + "_filename"] = file.filename
    fn = file.filename.lower()

    if fn.endswith((".txt", ".csv")):
        with open(path, encoding="utf-8", errors="ignore") as f:
            last_uploaded_text[user] = f.read()
    elif fn.endswith((".xlsx", ".xls")):
        wb = openpyxl.load_workbook(path, data_only=True)
        sheets = []
        for sn in wb.sheetnames:
            ws  = wb[sn]
            rows = ["\t".join(str(c) if c is not None else "" for c in r)
                    for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
            if rows: sheets.append(f"[{sn}]\n" + "\n".join(rows))
        last_uploaded_text[user] = "\n\n".join(sheets)
    elif fn.endswith(".pdf"):
        if not PDF_AVAILABLE:
            return jsonify({"error": "PDF no soportado — instalá pypdf"}), 400
        try:
            reader = PdfReader(path)
            pages  = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"[Página {i+1}]\n{text.strip()}")
            last_uploaded_text[user] = "\n\n".join(pages) if pages else "[PDF sin texto extraíble]"
        except Exception as e:
            return jsonify({"error": f"Error leyendo PDF: {e}"}), 400
    elif fn.endswith((".jpg", ".jpeg", ".png")):
        last_uploaded_image[user] = path
    else:
        return jsonify({"error": "Tipo no soportado"}), 400

    return jsonify({"status": "uploaded", "filename": file.filename})


@app.route("/extract-file", methods=["POST"])
def extract_file():
    """Recibe un archivo en base64 y devuelve su texto extraído."""
    import base64 as _b64
    import tempfile
    data     = request.json
    name     = data.get("name", "archivo")
    dataURL  = data.get("dataURL", "")
    fn       = name.lower()

    # Decodificar base64
    try:
        if "," in dataURL:
            raw = _b64.b64decode(dataURL.split(",", 1)[1])
        else:
            raw = _b64.b64decode(dataURL)
    except Exception as e:
        return jsonify({"error": f"Error decodificando archivo: {e}"}), 400

    # Guardar en temp
    suffix = os.path.splitext(fn)[1] or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        text = ""
        if fn.endswith((".txt", ".csv", ".md", ".json", ".html")):
            with open(tmp_path, encoding="utf-8", errors="ignore") as f:
                text = f.read()

        elif fn.endswith((".xlsx", ".xls")):
            wb = openpyxl.load_workbook(tmp_path, data_only=True)
            sheets = []
            for sn in wb.sheetnames:
                ws = wb[sn]
                rows = [" | ".join(str(c) if c is not None else "" for c in r)
                        for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
                if rows:
                    sheets.append(f"[Hoja: {sn}]\n" + "\n".join(rows))
            text = "\n\n".join(sheets)

        elif fn.endswith(".pdf"):
            if not PDF_AVAILABLE:
                return jsonify({"error": "PDF no soportado"}), 400
            reader = PdfReader(tmp_path)
            pages = []
            for i, page in enumerate(reader.pages):
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(f"[Página {i+1}]\n{t.strip()}")
            text = "\n\n".join(pages) if pages else "[PDF sin texto extraíble]"

        else:
            return jsonify({"error": f"Tipo no soportado: {suffix}"}), 400

        return jsonify({"text": text, "name": name})

    except Exception as e:
        import traceback
        print("EXTRACT ERROR:", traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500
    finally:
        try: os.unlink(tmp_path)
        except: pass


# ── Historial personal ────────────────────────────────────────────────────
@app.route("/history/<user>")
def get_history(user):
    return jsonify(load_history().get(user, []))

@app.route("/delete/<user>", methods=["DELETE"])
def delete_history(user):
    h = load_history()
    h.pop(user, None)
    save_history(h)
    return jsonify({"status": "deleted"})


# ── Presencia ─────────────────────────────────────────────────────────────
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json
    user = data.get("user")
    if not user: return jsonify({"error": "missing user"}), 400
    user_presence[user] = {"last_seen": time.time(), "status": "online" if data.get("active", True) else "away"}
    return jsonify({"status": "ok"})

@app.route("/offline", methods=["POST"])
def set_offline():
    data = request.json
    user = data.get("user")
    if user:
        user_presence[user] = {"last_seen": time.time(), "status": "offline"}
    return jsonify({"status": "ok"})

@app.route("/users")
def users():
    return jsonify([
        {"name": u, "status": get_status(e)}
        for u, e in user_presence.items() if u
    ])


# ── Mensajes directos (DM) ────────────────────────────────────────────────
def dm_key(a, b):
    """Clave de conversación ordenada alfabéticamente para que sea simétrica."""
    return "__".join(sorted([a, b]))

@app.route("/dm/<other_user>", methods=["GET"])
def get_dm(other_user):
    user = request.args.get("user")
    if not user:
        return jsonify({"error": "missing user"}), 400
    key  = dm_key(user, other_user)
    dms  = load_dms()
    return jsonify(dms.get(key, []))

@app.route("/dm/<other_user>", methods=["POST"])
def send_dm(other_user):
    data    = request.json
    sender  = data.get("from")
    message = data.get("message", "").strip()
    files   = data.get("files", [])   # [{name, type, dataURL}]

    if not sender or (not message and not files):
        return jsonify({"error": "faltan datos"}), 400

    key = dm_key(sender, other_user)
    dms = load_dms()
    if key not in dms:
        dms[key] = []

    entry = {"from": sender, "message": message, "files": files, "ts": time.time()}
    dms[key].append(entry)
    save_dms(dms)

    push_event(other_user, "dm", sender, message=message, files=files, conv_key=key)

    return jsonify({"status": "sent", "entry": entry})


# ══════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_sheets_service(user=None):
    """Devuelve cliente de Sheets autenticado via Service Account."""
    if not GOOGLE_AVAILABLE or not GOOGLE_SA_JSON:
        return None
    try:
        sa_info = json.loads(GOOGLE_SA_JSON)
        creds   = service_account.Credentials.from_service_account_info(
            sa_info, scopes=GOOGLE_SCOPES
        )
        return gapi_build("sheets", "v4", credentials=creds)
    except Exception as e:
        print("Error creando servicio Sheets:", e)
        return None

def get_user_sheets_cfg(user):
    """Devuelve (library, active_ids) para el usuario."""
    cfg = load_sheets().get(user, {})
    return cfg.get("library", []), cfg.get("active", [])

def read_sheet(user):
    """Lee todas las hojas activas. Devuelve string o None."""
    library, active_ids = get_user_sheets_cfg(user)
    active = [s for s in library if s["id"] in active_ids]
    if not active:
        return None
    svc = get_sheets_service()
    if not svc:
        return None
    parts = []
    for sheet in active:
        try:
            result = svc.spreadsheets().values().get(
                spreadsheetId=sheet["id"],
                range=f"{sheet.get('tab', 'Sheet1')}!A1:P30"
            ).execute()
            rows  = result.get("values", [])
            lines = [" | ".join(row) for row in rows]
            parts.append(f"[Hoja: {sheet['name']}]\n" + "\n".join(lines))
        except Exception as e:
            parts.append(f"[Hoja: {sheet['name']} — error: {e}]")
    return "\n\n".join(parts) if parts else None

def write_to_sheet(user, sheet_name_or_id, range_a1, values):
    """Escribe en una hoja activa específica."""
    library, active_ids = get_user_sheets_cfg(user)
    active = [s for s in library if s["id"] in active_ids]
    # Buscar por nombre o id
    target = next((s for s in active if s["name"] == sheet_name_or_id or s["id"] == sheet_name_or_id), None)
    if not target and active:
        target = active[0]  # fallback a la primera activa
    if not target:
        return False, "No hay hoja activa."
    svc = get_sheets_service()
    if not svc:
        return False, "No se pudo autenticar con Google."
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=target["id"],
            range=range_a1,
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        return True, f"Actualicé el rango {range_a1} en '{target['name']}'."
    except Exception as e:
        return False, f"Error al escribir: {e}"

def append_to_sheet(user, sheet_name_or_id, values):
    """Agrega una fila al final de una hoja activa."""
    library, active_ids = get_user_sheets_cfg(user)
    active = [s for s in library if s["id"] in active_ids]
    target = next((s for s in active if s["name"] == sheet_name_or_id or s["id"] == sheet_name_or_id), None)
    if not target and active:
        target = active[0]
    if not target:
        return False, "No hay hoja activa."
    svc = get_sheets_service()
    if not svc:
        return False, "No se pudo autenticar con Google."
    try:
        tab = target.get("tab", "Sheet1")
        svc.spreadsheets().values().append(
            spreadsheetId=target["id"],
            range=f"{tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values}
        ).execute()
        return True, f"Agregué una fila en '{target['name']}'."
    except Exception as e:
        return False, f"Error al agregar fila: {e}"


# ── Sheets endpoints (Service Account, multi-sheet) ───────────────────────

@app.route("/sheets/add", methods=["POST"])
def sheets_add():
    """Agrega una hoja a la biblioteca del usuario (o la activa si ya existe)."""
    data        = request.json
    user        = data.get("user")
    sheet_input = data.get("sheet_url_or_id", "").strip()

    if not sheet_input:
        return jsonify({"error": "URL o ID de hoja vacío"}), 400
    if not GOOGLE_SA_JSON:
        return jsonify({"error": "GOOGLE_SERVICE_ACCOUNT no configurado en Render"}), 400

    m        = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_input)
    sheet_id = m.group(1) if m else sheet_input

    svc = get_sheets_service()
    if not svc:
        return jsonify({"error": "No se pudo crear el servicio de Sheets"}), 500

    try:
        meta       = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_name = meta["properties"]["title"]
        tabs       = [s["properties"]["title"] for s in meta.get("sheets", [])]
        first_tab  = tabs[0] if tabs else "Sheet1"
    except Exception as e:
        import traceback
        print("ERROR accediendo a hoja:", traceback.format_exc(), flush=True)
        return jsonify({"error": f"No se pudo acceder: {str(e)}. ¿Compartiste la hoja con el email de la cuenta de servicio?"}), 400

    sheets_cfg = load_sheets()
    if user not in sheets_cfg:
        sheets_cfg[user] = {"library": [], "active": []}

    library = sheets_cfg[user].get("library", [])
    active  = sheets_cfg[user].get("active", [])

    # Si ya existe en biblioteca, solo activar
    existing = next((s for s in library if s["id"] == sheet_id), None)
    if not existing:
        library.append({"id": sheet_id, "name": sheet_name, "tab": first_tab})
    if sheet_id not in active:
        active.append(sheet_id)

    sheets_cfg[user]["library"] = library
    sheets_cfg[user]["active"]  = active
    save_sheets(sheets_cfg)

    return jsonify({"sheet_id": sheet_id, "sheet_name": sheet_name, "tab": first_tab, "tabs": tabs})


@app.route("/sheets/library", methods=["GET"])
def sheets_library():
    """Devuelve biblioteca y activas del usuario."""
    user = request.args.get("user")
    sheets_cfg = load_sheets()
    cfg = sheets_cfg.get(user, {})
    return jsonify({
        "library": cfg.get("library", []),
        "active":  cfg.get("active",  []),
    })


@app.route("/sheets/set-active", methods=["POST"])
def sheets_set_active():
    """Actualiza la lista de hojas activas."""
    data   = request.json
    user   = data.get("user")
    active = data.get("active", [])   # lista de sheet_ids
    sheets_cfg = load_sheets()
    if user not in sheets_cfg:
        sheets_cfg[user] = {"library": [], "active": []}
    sheets_cfg[user]["active"] = active
    save_sheets(sheets_cfg)
    return jsonify({"active": active})


@app.route("/sheets/remove", methods=["POST"])
def sheets_remove():
    """Elimina una hoja de la biblioteca."""
    data     = request.json
    user     = data.get("user")
    sheet_id = data.get("sheet_id")
    sheets_cfg = load_sheets()
    if user in sheets_cfg:
        sheets_cfg[user]["library"] = [s for s in sheets_cfg[user].get("library", []) if s["id"] != sheet_id]
        sheets_cfg[user]["active"]  = [i for i in sheets_cfg[user].get("active",  []) if i != sheet_id]
        save_sheets(sheets_cfg)
    return jsonify({"status": "removed"})


@app.route("/sheets/read", methods=["GET"])
def sheets_read():
    user = request.args.get("user")
    data = read_sheet(user)
    if data is None:
        return jsonify({"error": "No hay hojas activas"}), 404
    return jsonify({"data": data})


@app.route("/")
def home():
    return "AI Backend Running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
