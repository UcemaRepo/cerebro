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

DATA_FILE          = "conversations.json"
PERSONALITIES_FILE = "personalities.json"
EVENTS_FILE        = "events.json"

user_presence       = {}
last_uploaded_text  = {}
last_uploaded_image = {}

AWAY_THRESHOLD = 300

# ── Salas efímeras ─────────────────────────────────────────────────────
# { room_id: { name, creator, participants: set(), messages: [], created_at } }
rooms = {}


# ── Historial permanente ────────────────────────────────────────────────
def load_history():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_history(history):
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ── Eventos en tiempo real ──────────────────────────────────────────────
def load_events():
    if not os.path.exists(EVENTS_FILE):
        return {}
    with open(EVENTS_FILE, "r") as f:
        return json.load(f)

def save_events(events):
    with open(EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2)

def push_event(target_user, event_type, actor, **kwargs):
    """Push an event to a specific user's event queue."""
    events = load_events()
    if target_user not in events:
        events[target_user] = []
    entry = {"type": event_type, "actor": actor, "ts": time.time()}
    entry.update(kwargs)
    events[target_user].append(entry)
    events[target_user] = events[target_user][-100:]
    save_events(events)

def broadcast_to_room(room_id, event_type, actor, **kwargs):
    """Push an event to every participant of a room."""
    if room_id not in rooms:
        return
    for participant in rooms[room_id]["participants"]:
        push_event(participant, event_type, actor, room_id=room_id, **kwargs)


# ── Personalidades ──────────────────────────────────────────────────────
PRESET_PERSONALITIES = {
    "normal":   "Eres un asistente útil, claro y amable.",
    "analyst":  "Eres un analista experto. Respondés con datos, métricas y razonamiento estructurado. Usás tablas comparativas cuando es útil.",
    "creative": "Eres un asistente creativo y disruptivo. Das ideas originales, fuera de lo convencional.",
    "strict":   "Eres un asistente estricto y conciso. Respondés solo lo necesario, sin rodeos.",
    "dev":      "Eres un desarrollador senior. Respondés con código limpio y buenas prácticas.",
    "coach":    "Eres un coach ejecutivo. Hacés preguntas poderosas y ayudás a estructurar objetivos.",
}

def load_personalities():
    if not os.path.exists(PERSONALITIES_FILE):
        return {}
    with open(PERSONALITIES_FILE, "r") as f:
        return json.load(f)

def save_personalities(p):
    with open(PERSONALITIES_FILE, "w") as f:
        json.dump(p, f, indent=2)

def get_personality_text(user, preset_key):
    personalities = load_personalities()
    custom = personalities.get(user, {}).get("custom", "").strip()
    if custom:
        return custom
    return PRESET_PERSONALITIES.get(preset_key, PRESET_PERSONALITIES["normal"])


# ── Respuestas enriquecidas ─────────────────────────────────────────────
SYSTEM_RICH = """
Podés enriquecer tus respuestas con estas etiquetas especiales cuando aporten valor real:

Tabla comparativa:
<table>
Columna1 | Columna2 | Columna3
Valor1   | Valor2   | Valor3
</table>

Widget informativo (dato clave, resumen, alerta):
<widget title="Título">Contenido del widget</widget>

Archivo descargable — REGLAS:
- Usá SOLO: .txt, .csv, .md, .json, .html
- NUNCA: .docx, .xlsx, .pptx (son binarios)
- Para tablas → .csv | Para documentos → .md o .html

<download filename="datos.csv">col1,col2
valor1,valor2</download>

Usá estas etiquetas solo cuando aporten valor concreto.
"""


# ── Presencia ───────────────────────────────────────────────────────────
def get_status(entry):
    if entry.get("status") == "offline":
        return "offline"
    elapsed = time.time() - entry.get("last_seen", 0)
    return "away" if elapsed > AWAY_THRESHOLD else "online"


# ── Contexto de usuarios para consultas naturales ──────────────────────
def build_users_context(history, known_users):
    if not known_users:
        return ""
    lines = ["=== Historial de otros usuarios (contexto de referencia) ==="]
    for u in known_users:
        u_history = history.get(u, [])
        if not u_history:
            continue
        lines.append(f"\n--- {u} ---")
        for h in u_history[-15:]:
            lines.append(f"  [{u}]: {h['message']}")
            lines.append(f"  [Bot]: {h['reply']}")
            if h.get("file"):
                lines.append(f"  [Archivo]: {h['file']}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Endpoint: chat propio (historial permanente) ────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data    = request.json
    message = data.get("message")
    user    = data.get("user")
    preset  = data.get("personality", "normal")

    user_presence[user] = {"last_seen": time.time(), "status": "online"}

    history          = load_history()
    personality_text = get_personality_text(user, preset)
    all_users        = [u for u in history.keys() if u != user]

    if user not in history:
        history[user] = []

    messages = [{"role": "system", "content": personality_text + "\n\n" + SYSTEM_RICH}]

    for h in history[user][-10:]:
        messages.append({"role": "user",      "content": h["message"]})
        messages.append({"role": "assistant", "content": h["reply"]})

    users_ctx = build_users_context(history, all_users)
    if users_ctx:
        messages.append({"role": "system", "content": users_ctx})

    uploaded_text = last_uploaded_text.get(user, "")
    if uploaded_text:
        messages.append({"role": "system", "content": "Documento subido:\n\n" + uploaded_text})

    uploaded_image = last_uploaded_image.get(user)
    if uploaded_image:
        import base64
        with open(uploaded_image, "rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")
        messages.append({"role": "user", "content": [
            {"type": "text", "text": message},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]})
    else:
        messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(model="gpt-5.4", messages=messages)
    reply = response.choices[0].message.content

    entry = {"message": message, "reply": reply, "actor": user}
    if uploaded_text:
        entry["file"] = last_uploaded_text.get(user + "_filename", "archivo")
    history[user].append(entry)
    save_history(history)

    # Notificar espejo pasivo
    push_event(user, "mirror_update", user, message=message, reply=reply)

    return jsonify({"reply": reply})


# ── Endpoint: chat de sala efímera ─────────────────────────────────────
@app.route("/room-chat", methods=["POST"])
def room_chat():
    data    = request.json
    message = data.get("message")
    user    = data.get("user")
    room_id = data.get("room_id")
    preset  = data.get("personality", "normal")

    if room_id not in rooms:
        return jsonify({"error": "sala no existe"}), 404

    room             = rooms[room_id]
    personality_text = get_personality_text(user, preset)

    messages = [
        {"role": "system", "content": personality_text + "\n\n" + SYSTEM_RICH},
        {"role": "system", "content": (
            f"Estás en la sala compartida '{room['name']}'. "
            f"Participantes actuales: {', '.join(room['participants'])}. "
            f"Esta sesión es efímera: no se guarda permanentemente. "
            f"Respondé a quien escribe manteniendo contexto de la sesión."
        )}
    ]

    for h in room["messages"][-10:]:
        messages.append({"role": "user",      "content": f"[{h['actor']}]: {h['message']}"})
        messages.append({"role": "assistant", "content": h["reply"]})

    messages.append({"role": "user", "content": f"[{user}]: {message}"})

    response = client.chat.completions.create(model="gpt-5.4", messages=messages)
    reply = response.choices[0].message.content

    # Guardar en sala (memoria, no disco)
    room["messages"].append({
        "actor": user, "message": message, "reply": reply, "ts": time.time()
    })

    # Broadcast a todos en la sala
    broadcast_to_room(room_id, "room_message", user,
                      message=message, reply=reply, room_name=room["name"])

    return jsonify({"reply": reply, "room_id": room_id})


# ── Endpoints: gestión de salas ─────────────────────────────────────────
@app.route("/rooms", methods=["GET"])
def list_rooms():
    result = []
    for rid, room in rooms.items():
        result.append({
            "id":           rid,
            "name":         room["name"],
            "creator":      room["creator"],
            "participants": list(room["participants"]),
            "msg_count":    len(room["messages"]),
            "created_at":   room["created_at"],
        })
    return jsonify(result)

@app.route("/rooms", methods=["POST"])
def create_room():
    data    = request.json
    user    = data.get("user")
    name    = data.get("name", f"Sala de {user}").strip()
    room_id = str(uuid.uuid4())[:8]

    rooms[room_id] = {
        "name":        name,
        "creator":     user,
        "participants": {user},
        "messages":    [],
        "created_at":  time.time(),
    }

    # Broadcast a todos los usuarios online que hay una sala nueva
    for u in user_presence:
        push_event(u, "room_created", user, room_id=room_id, room_name=name)

    return jsonify({"room_id": room_id, "name": name})

@app.route("/rooms/<room_id>/join", methods=["POST"])
def join_room(room_id):
    data = request.json
    user = data.get("user")

    if room_id not in rooms:
        return jsonify({"error": "sala no existe"}), 404

    rooms[room_id]["participants"].add(user)
    broadcast_to_room(room_id, "room_join", user, room_name=rooms[room_id]["name"])

    return jsonify({
        "status":   "joined",
        "name":     rooms[room_id]["name"],
        "history":  rooms[room_id]["messages"],
        "participants": list(rooms[room_id]["participants"]),
    })

@app.route("/rooms/<room_id>/leave", methods=["POST"])
def leave_room(room_id):
    data = request.json
    user = data.get("user")

    if room_id not in rooms:
        return jsonify({"status": "ok"})

    rooms[room_id]["participants"].discard(user)
    broadcast_to_room(room_id, "room_leave", user, room_name=rooms[room_id]["name"])

    # Si la sala queda vacía, destruirla
    if not rooms[room_id]["participants"]:
        del rooms[room_id]
        # Notificar a todos que la sala desapareció
        for u in user_presence:
            push_event(u, "room_deleted", user, room_id=room_id)

    return jsonify({"status": "left"})

@app.route("/rooms/<room_id>", methods=["GET"])
def get_room(room_id):
    if room_id not in rooms:
        return jsonify({"error": "sala no existe"}), 404
    room = rooms[room_id]
    return jsonify({
        "id":           room_id,
        "name":         room["name"],
        "participants": list(room["participants"]),
        "messages":     room["messages"],
    })


# ── Endpoint: polling de eventos ───────────────────────────────────────
@app.route("/events/<user>")
def get_events(user):
    since = float(request.args.get("since", 0))
    events = load_events()
    user_events = [e for e in events.get(user, []) if e["ts"] > since]
    return jsonify(user_events)


# ── Endpoint: personalidad ──────────────────────────────────────────────
@app.route("/personality/<user>", methods=["GET"])
def get_user_personality(user):
    personalities = load_personalities()
    entry = personalities.get(user, {})
    return jsonify({"custom": entry.get("custom", ""), "presets": PRESET_PERSONALITIES})

@app.route("/personality/<user>", methods=["POST"])
def set_user_personality(user):
    data = request.json
    personalities = load_personalities()
    if user not in personalities:
        personalities[user] = {}
    personalities[user]["custom"] = data.get("custom", "").strip()
    save_personalities(personalities)
    return jsonify({"status": "ok"})

@app.route("/personality/<user>", methods=["DELETE"])
def clear_user_personality(user):
    personalities = load_personalities()
    if user in personalities:
        personalities[user]["custom"] = ""
    save_personalities(personalities)
    return jsonify({"status": "cleared"})


# ── Endpoint: upload ────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    user = request.form.get("user", "default")

    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", file.filename)
    file.save(path)

    last_uploaded_text[user]               = ""
    last_uploaded_image[user]              = None
    last_uploaded_text[user + "_filename"] = file.filename
    filename = file.filename.lower()

    if filename.endswith((".txt", ".csv")):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            last_uploaded_text[user] = f.read()
    elif filename.endswith((".xlsx", ".xls")):
        wb = openpyxl.load_workbook(path, data_only=True)
        sheets_text = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    rows.append("\t".join(str(c) if c is not None else "" for c in row))
            if rows:
                sheets_text.append(f"[Hoja: {sheet_name}]\n" + "\n".join(rows))
        last_uploaded_text[user] = "\n\n".join(sheets_text)
    elif filename.endswith((".jpg", ".jpeg", ".png")):
        last_uploaded_image[user] = path
    else:
        return jsonify({"error": "Tipo de archivo no soportado"}), 400

    return jsonify({"status": "uploaded", "filename": file.filename})


# ── Endpoint: historial permanente ─────────────────────────────────────
@app.route("/history/<user>")
def get_user_history(user):
    return jsonify(load_history().get(user, []))

@app.route("/delete/<user>", methods=["DELETE"])
def delete_user(user):
    history = load_history()
    if user in history:
        del history[user]
    save_history(history)
    return jsonify({"status": "deleted"})


# ── Endpoint: presencia ─────────────────────────────────────────────────
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data   = request.json
    user   = data.get("user")
    active = data.get("active", True)
    if not user:
        return jsonify({"error": "missing user"}), 400
    user_presence[user] = {"last_seen": time.time(), "status": "online" if active else "away"}
    return jsonify({"status": "ok"})

@app.route("/offline", methods=["POST"])
def set_offline():
    data = request.json
    user = data.get("user")
    if user:
        user_presence[user] = {"last_seen": time.time(), "status": "offline"}
        # Salir de todas las salas donde estaba
        for room_id in list(rooms.keys()):
            if user in rooms[room_id]["participants"]:
                rooms[room_id]["participants"].discard(user)
                broadcast_to_room(room_id, "room_leave", user, room_name=rooms[room_id]["name"])
                if not rooms[room_id]["participants"]:
                    del rooms[room_id]
    return jsonify({"status": "ok"})

@app.route("/users")
def users():
    result = []
    for user, entry in user_presence.items():
        if not user:
            continue
        result.append({"name": user, "status": get_status(entry)})
    return jsonify(result)


# ── Endpoint: historial de sala (mirror pasivo) ─────────────────────────
@app.route("/history-mirror/<user>")
def get_user_history_mirror(user):
    """Para el panel espejo pasivo: devuelve historial permanente."""
    return jsonify(load_history().get(user, []))


# ── Home ────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "AI Backend Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
