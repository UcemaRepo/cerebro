import os
import json
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import openpyxl

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DATA_FILE        = "conversations.json"
PERSONALITIES_FILE = "personalities.json"
EVENTS_FILE      = "events.json"       # feed de eventos en tiempo real

user_presence    = {}
last_uploaded_text  = {}
last_uploaded_image = {}

AWAY_THRESHOLD = 300  # 5 minutos


# ── Historial ──────────────────────────────────────────────────────────
def load_history():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_history(history):
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ── Eventos en tiempo real ─────────────────────────────────────────────
# Estructura: { "owner": [ {type, actor, text, ts}, ... ] }
def load_events():
    if not os.path.exists(EVENTS_FILE):
        return {}
    with open(EVENTS_FILE, "r") as f:
        return json.load(f)

def save_events(events):
    with open(EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2)

def push_event(owner, event_type, actor, text=""):
    events = load_events()
    if owner not in events:
        events[owner] = []
    events[owner].append({
        "type":  event_type,   # "join" | "leave" | "message"
        "actor": actor,
        "text":  text,
        "ts":    time.time()
    })
    # Mantener solo los últimos 50 eventos por chat
    events[owner] = events[owner][-50:]
    save_events(events)


# ── Personalidades ─────────────────────────────────────────────────────
PRESET_PERSONALITIES = {
    "normal":   "Eres un asistente útil, claro y amable.",
    "analyst":  "Eres un analista experto. Respondés con datos, métricas y razonamiento estructurado. Usás tablas comparativas cuando es útil.",
    "creative": "Eres un asistente creativo y disruptivo. Das ideas originales, fuera de lo convencional, con entusiasmo.",
    "strict":   "Eres un asistente estricto y conciso. Respondés solo lo necesario, sin rodeos.",
    "dev":      "Eres un desarrollador senior. Respondés con código limpio, explicaciones técnicas y buenas prácticas.",
    "coach":    "Eres un coach ejecutivo. Hacés preguntas poderosas, motivás y ayudás a estructurar objetivos.",
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

Widget informativo:
<widget title="Título">Contenido del widget</widget>

Archivo descargable:
<download filename="archivo.txt">Contenido del archivo</download>

Usá estas etiquetas solo cuando aporten valor concreto.
"""


# ── Presencia ──────────────────────────────────────────────────────────
def get_status(entry):
    if entry.get("status") == "offline":
        return "offline"
    elapsed = time.time() - entry.get("last_seen", 0)
    return "away" if elapsed > AWAY_THRESHOLD else "online"


# ── Contexto de todos los usuarios (para consultas naturales) ──────────
def build_users_context(history, known_users):
    """Construye un resumen del historial de todos los usuarios para que el bot
    pueda responder preguntas sobre ellos de forma natural, sin endpoint especial."""
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
            # Incluir referencia a archivos si los hay
            if h.get("file"):
                lines.append(f"  [Archivo subido]: {h['file']}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Endpoint: chat principal ────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data          = request.json
    message       = data.get("message")
    user          = data.get("user")
    preset        = data.get("personality", "normal")
    # chat_owner: si está seteado, el usuario está dentro del chat de otra persona
    chat_owner    = data.get("chat_owner")

    user_presence[user] = {"last_seen": time.time(), "status": "online"}

    history          = load_history()
    personality_text = get_personality_text(user, preset)
    all_users        = [u for u in history.keys() if u != user]

    # El dueño del chat donde se escribe (puede ser el propio usuario u otro)
    owner = chat_owner if chat_owner else user

    if owner not in history:
        history[owner] = []

    owner_history = history[owner]

    # Construir mensajes para el modelo
    messages = [{"role": "system", "content": personality_text + "\n\n" + SYSTEM_RICH}]

    if chat_owner:
        messages.append({
            "role": "system",
            "content": (
                f"'{user}' se unió al chat de '{chat_owner}' y está escribiendo en él. "
                f"Continuá la conversación con pleno contexto del historial."
            )
        })

    # Historial del chat donde se está escribiendo
    for h in owner_history[-10:]:
        actor = h.get("actor", owner)
        msg_text = f"[{actor}]: {h['message']}" if chat_owner else h["message"]
        messages.append({"role": "user",      "content": msg_text})
        messages.append({"role": "assistant", "content": h["reply"]})

    # Contexto de otros usuarios para consultas naturales
    users_ctx = build_users_context(history, all_users)
    if users_ctx:
        messages.append({"role": "system", "content": users_ctx})

    # Archivo subido
    uploaded_text = last_uploaded_text.get(user, "")
    if uploaded_text:
        messages.append({
            "role": "system",
            "content": "El usuario subió el siguiente documento:\n\n" + uploaded_text
        })

    uploaded_image = last_uploaded_image.get(user)
    if uploaded_image:
        import base64
        with open(uploaded_image, "rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")
        messages.append({
            "role": "user",
            "content": [
                {"type": "text",      "text": message},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        })
    else:
        display_msg = f"[{user}]: {message}" if chat_owner else message
        messages.append({"role": "user", "content": display_msg})

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=messages
    )
    reply = response.choices[0].message.content

    # Guardar siempre en el historial del dueño del chat
    entry = {"message": message, "reply": reply, "actor": user}
    if uploaded_text:
        entry["file"] = last_uploaded_text.get(user + "_filename", "archivo")
    history[owner].append(entry)
    save_history(history)

    # Emitir evento para que los demás vean el mensaje en tiempo real
    if chat_owner:
        push_event(owner, "message", user, message)

    return jsonify({"reply": reply, "saved_to": owner})


# ── Endpoint: entrar/salir de un chat ──────────────────────────────────
@app.route("/join-chat", methods=["POST"])
def join_chat_endpoint():
    data  = request.json
    user  = data.get("user")
    owner = data.get("owner")
    if not user or not owner or user == owner:
        return jsonify({"error": "datos inválidos"}), 400
    push_event(owner, "join", user)
    return jsonify({"status": "joined"})

@app.route("/leave-chat", methods=["POST"])
def leave_chat_endpoint():
    data  = request.json
    user  = data.get("user")
    owner = data.get("owner")
    if user and owner:
        push_event(owner, "leave", user)
    return jsonify({"status": "left"})


# ── Endpoint: polling de eventos ───────────────────────────────────────
@app.route("/events/<owner>")
def get_events(owner):
    """El frontend hace polling cada 3s para ver si hay nuevos mensajes/eventos."""
    since = float(request.args.get("since", 0))
    events = load_events()
    owner_events = [e for e in events.get(owner, []) if e["ts"] > since]
    return jsonify(owner_events)


# ── Endpoint: personalidad ─────────────────────────────────────────────
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


# ── Endpoint: upload ───────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    user = request.form.get("user", "default")

    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", file.filename)
    file.save(path)

    last_uploaded_text[user]              = ""
    last_uploaded_image[user]             = None
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


# ── Endpoint: historial ────────────────────────────────────────────────
@app.route("/history")
def get_all_history():
    return jsonify(load_history())

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


# ── Endpoint: presencia ────────────────────────────────────────────────
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
    return jsonify({"status": "ok"})

@app.route("/users")
def users():
    result = []
    for user, entry in user_presence.items():
        if not user:
            continue
        result.append({"name": user, "status": get_status(entry)})
    return jsonify(result)


# ── Home ───────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "AI Backend Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
