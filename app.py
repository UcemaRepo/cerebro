import os
import json
import time
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import openpyxl

app = Flask(__name__)
CORS(app)

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

    resp  = client.chat.completions.create(model="gpt-5.4", messages=msgs)
    reply = resp.choices[0].message.content

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

    channels[slug] = {
        "id":       slug,
        "name":     name,
        "creator":  user,
        "created":  time.time(),
        "messages": [],
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

    entry = {"actor": user, "message": message, "reply": reply, "ts": time.time()}
    ch["messages"].append(entry)
    save_channels(channels)

    # Notificar a todos que hay mensaje nuevo en este canal
    for u in user_presence:
        push_event(u, "channel_message", user,
                   channel_id=channel_id, message=message, reply=reply)

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
    if not sender or not message:
        return jsonify({"error": "faltan datos"}), 400

    key  = dm_key(sender, other_user)
    dms  = load_dms()
    if key not in dms:
        dms[key] = []

    entry = {"from": sender, "message": message, "ts": time.time()}
    dms[key].append(entry)
    save_dms(dms)

    # Notificar al destinatario en tiempo real
    push_event(other_user, "dm", sender, message=message, conv_key=key)

    return jsonify({"status": "sent", "entry": entry})


@app.route("/")
def home():
    return "AI Backend Running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
