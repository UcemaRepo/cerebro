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
        for m in h[-10:]:
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

    for h in history[user][-10:]:
        msgs.append({"role": "user",      "content": h["message"]})
        msgs.append({"role": "assistant", "content": h["reply"]})

    ctx = build_users_context(history, [u for u in history if u != user])
    if ctx:
        msgs.append({"role": "system", "content": ctx})

    uploaded_text = last_uploaded_text.get(user, "")
    if uploaded_text:
        msgs.append({"role": "system", "content": "Documento:\n\n" + uploaded_text})

    uploaded_image = last_uploaded_image.get(user)
    if uploaded_image:
        import base64
        with open(uploaded_image, "rb") as img:
            b64 = base64.b64encode(img.read()).decode()
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": message},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]})
    else:
        msgs.append({"role": "user", "content": message})

    # Inyectar contexto de la hoja si el usuario tiene una conectada
    sheet_data = read_sheet(user)
    if sheet_data:
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
                "description": "Modifica la hoja de Google Sheets conectada del usuario. Usá esto cuando el usuario pida escribir, actualizar, agregar o borrar datos en su hoja.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["write", "append"],
                            "description": "write: sobreescribe un rango. append: agrega fila al final."
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
                            "description": "Descripción breve de qué cambio estás haciendo, para mostrarle al usuario."
                        }
                    },
                    "required": ["action", "values", "summary"]
                }
            }
        }]

    kwargs = {"model": "gpt-5.4", "messages": msgs}
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

                if action == "append":
                    ok, result_msg = append_to_sheet(user, values)
                else:
                    range_a1 = args.get("range_a1", "Sheet1!A1")
                    ok, result_msg = write_to_sheet(user, range_a1, values)

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
    if uploaded_text:
        entry["file"] = last_uploaded_text.get(user + "_filename", "archivo")
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
    elif fn.endswith((".jpg", ".jpeg", ".png")):
        last_uploaded_image[user] = path
    else:
        return jsonify({"error": "Tipo no soportado"}), 400

    return jsonify({"status": "uploaded", "filename": file.filename})


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

def read_sheet(user):
    """Lee las primeras 50 filas de la hoja conectada. Devuelve string o None."""
    sheets_cfg = load_sheets()
    cfg = sheets_cfg.get(user)
    if not cfg:
        return None
    svc = get_sheets_service(user)
    if not svc:
        return None
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=cfg["sheet_id"],
            range=f"{cfg.get('tab', 'Sheet1')}!A1:Z50"
        ).execute()
        rows = result.get("values", [])
        lines = [" | ".join(row) for row in rows]
        return f"[Hoja: {cfg['sheet_name']}]\n" + "\n".join(lines)
    except Exception as e:
        return f"[Error leyendo hoja: {e}]"

def write_to_sheet(user, range_a1, values):
    """Escribe values (lista de listas) en el rango dado. Devuelve (ok, msg)."""
    sheets_cfg = load_sheets()
    cfg = sheets_cfg.get(user)
    if not cfg:
        return False, "No hay hoja conectada para este usuario."
    svc = get_sheets_service(user)
    if not svc:
        return False, "No se pudo autenticar con Google."
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=cfg["sheet_id"],
            range=range_a1,
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        return True, f"Actualicé el rango {range_a1} en {cfg['sheet_name']}."
    except Exception as e:
        return False, f"Error al escribir: {e}"

def append_to_sheet(user, values):
    """Agrega una fila al final de la hoja."""
    sheets_cfg = load_sheets()
    cfg = sheets_cfg.get(user)
    if not cfg:
        return False, "No hay hoja conectada."
    svc = get_sheets_service(user)
    if not svc:
        return False, "No se pudo autenticar con Google."
    try:
        tab = cfg.get("tab", "Sheet1")
        svc.spreadsheets().values().append(
            spreadsheetId=cfg["sheet_id"],
            range=f"{tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values}
        ).execute()
        return True, f"Agregué una fila en {cfg['sheet_name']}."
    except Exception as e:
        return False, f"Error al agregar fila: {e}"


# ── Sheets endpoints (Service Account) ────────────────────────────────────


@app.route("/sheets/connect-sheet", methods=["POST"])
def sheets_connect_sheet():
    """Después del OAuth, el usuario pega la URL o ID de su hoja."""
    data     = request.json
    user     = data.get("user")
    sheet_input = data.get("sheet_url_or_id", "").strip()

    # Extraer sheet_id de URL o usar directo
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_input)
    sheet_id = m.group(1) if m else sheet_input

    if not GOOGLE_SA_JSON:
        return jsonify({"error": "GOOGLE_SERVICE_ACCOUNT no configurado en Render"}), 400
    sheets_cfg = load_sheets()

    # Leer nombre real de la hoja
    try:
        svc = get_sheets_service(user)
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_name = meta["properties"]["title"]
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        first_tab = tabs[0] if tabs else "Sheet1"
    except Exception as e:
        return jsonify({"error": f"No se pudo acceder a la hoja: {e}"}), 400

    sheets_cfg[user]["sheet_id"]   = sheet_id
    sheets_cfg[user]["sheet_name"] = sheet_name
    sheets_cfg[user]["tab"]        = first_tab
    save_sheets(sheets_cfg)

    return jsonify({"sheet_name": sheet_name, "tab": first_tab, "tabs": tabs})


@app.route("/sheets/disconnect", methods=["POST"])
def sheets_disconnect():
    data = request.json
    user = data.get("user")
    sheets_cfg = load_sheets()
    if user in sheets_cfg:
        del sheets_cfg[user]
        save_sheets(sheets_cfg)
    return jsonify({"status": "disconnected"})


@app.route("/sheets/status", methods=["GET"])
def sheets_status():
    user = request.args.get("user")
    sheets_cfg = load_sheets()
    cfg = sheets_cfg.get(user, {})
    if cfg.get("sheet_id"):
        return jsonify({
            "connected": True,
            "sheet_name": cfg.get("sheet_name"),
            "tab": cfg.get("tab"),
            "sheet_id": cfg.get("sheet_id"),
        })
    return jsonify({"connected": False})


@app.route("/sheets/read", methods=["GET"])
def sheets_read():
    user = request.args.get("user")
    data = read_sheet(user)
    if data is None:
        return jsonify({"error": "No hay hoja conectada"}), 404
    return jsonify({"data": data})


@app.route("/sheets/write", methods=["POST"])
def sheets_write():
    data   = request.json
    user   = data.get("user")
    range_ = data.get("range")
    values = data.get("values")   # [[row1col1, row1col2], [row2col1, ...]]
    ok, msg = write_to_sheet(user, range_, values)
    if ok:
        return jsonify({"status": "ok", "message": msg})
    return jsonify({"error": msg}), 400


@app.route("/sheets/append", methods=["POST"])
def sheets_append():
    data   = request.json
    user   = data.get("user")
    values = data.get("values")
    ok, msg = append_to_sheet(user, values)
    if ok:
        return jsonify({"status": "ok", "message": msg})
    return jsonify({"error": msg}), 400


@app.route("/")
def home():
    return "AI Backend Running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
