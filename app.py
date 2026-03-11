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

DATA_FILE = "conversations.json"
PERSONALITIES_FILE = "personalities.json"

# Estados de presencia por usuario
user_presence = {}

# Archivos subidos por usuario (en lugar de global)
last_uploaded_text = {}
last_uploaded_image = {}

AWAY_THRESHOLD = 300  # 5 minutos


# ── Historial ──────────────────────────────────────────
def load_history():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_history(history):
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ── Personalidades ─────────────────────────────────────
PRESET_PERSONALITIES = {
    "normal":   "Eres un asistente util, claro y amable.",
    "analyst":  "Eres un analista experto. Respondés con datos, métricas y razonamiento estructurado. Usás tablas comparativas cuando es útil.",
    "creative": "Eres un asistente creativo y disruptivo. Das ideas originales, fuera de lo convencional, con entusiasmo.",
    "strict":   "Eres un asistente estricto y conciso. Respondés solo lo necesario, sin rodeos.",
    "dev":      "Eres un desarrollador senior. Respondés con código limpio, explicaciones técnicas precisas y buenas prácticas.",
    "coach":    "Eres un coach ejecutivo. Hacés preguntas poderosas, motivás y ayudás a estructurar objetivos y planes de acción.",
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


# ── Sistema de respuestas enriquecidas ─────────────────
SYSTEM_RICH = """
Podés enriquecer tus respuestas con estas etiquetas especiales cuando aporten valor real:

Tabla comparativa:
<table>
Columna1 | Columna2 | Columna3
Valor1   | Valor2   | Valor3
</table>

Widget informativo (resumen, dato clave, alerta):
<widget title="Título">Contenido del widget</widget>

Archivo descargable (código, texto, CSV generado, etc.):
<download filename="archivo.txt">Contenido del archivo</download>

Usá estas etiquetas solo cuando aporten valor concreto. El texto normal va sin etiquetas.
"""


# ── Presencia ──────────────────────────────────────────
def get_status(entry):
    if entry.get("status") == "offline":
        return "offline"
    elapsed = time.time() - entry.get("last_seen", 0)
    if elapsed > AWAY_THRESHOLD:
        return "away"
    return "online"


# ── Endpoint: chat ──────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    message = data.get("message")
    user = data.get("user")
    preset = data.get("personality", "normal")
    inherited_from = data.get("inherited_from")  # usuario cuyo historial se hereda

    # Actualizar presencia
    user_presence[user] = {"last_seen": time.time(), "status": "online"}

    history = load_history()
    personality_text = get_personality_text(user, preset)

    if user not in history:
        history[user] = []

    user_history = history[user]

    # Contexto: si hay herencia, usar historial del usuario original
    context_user = inherited_from if inherited_from else user
    context_history = history.get(context_user, [])

    messages = [
        {"role": "system", "content": personality_text + "\n\n" + SYSTEM_RICH}
    ]

    if inherited_from:
        messages.append({
            "role": "system",
            "content": f"Estás siendo consultado por '{user}', que está continuando la conversación iniciada por '{inherited_from}'. Tenés acceso a todo el historial anterior y debés mantener el contexto completo."
        })

    for h in context_history[-10:]:
        messages.append({"role": "user", "content": h["message"]})
        messages.append({"role": "assistant", "content": h["reply"]})

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
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        })
    else:
        messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=messages
    )

    reply = response.choices[0].message.content

    user_history.append({"message": message, "reply": reply})
    history[user] = user_history
    save_history(history)

    return jsonify({"reply": reply})


# ── Endpoint: personalidad ──────────────────────────────
@app.route("/personality/<user>", methods=["GET"])
def get_user_personality(user):
    personalities = load_personalities()
    entry = personalities.get(user, {})
    return jsonify({
        "custom": entry.get("custom", ""),
        "presets": PRESET_PERSONALITIES
    })

@app.route("/personality/<user>", methods=["POST"])
def set_user_personality(user):
    data = request.json
    custom_text = data.get("custom", "").strip()
    personalities = load_personalities()
    if user not in personalities:
        personalities[user] = {}
    personalities[user]["custom"] = custom_text
    save_personalities(personalities)
    return jsonify({"status": "ok"})

@app.route("/personality/<user>", methods=["DELETE"])
def clear_user_personality(user):
    personalities = load_personalities()
    if user in personalities:
        personalities[user]["custom"] = ""
    save_personalities(personalities)
    return jsonify({"status": "cleared"})


# ── Endpoint: upload ────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    user = request.form.get("user", "default")

    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", file.filename)
    file.save(path)

    last_uploaded_text[user] = ""
    last_uploaded_image[user] = None

    filename = file.filename.lower()

    if filename.endswith(".txt") or filename.endswith(".csv"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            last_uploaded_text[user] = f.read()

    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
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

    return jsonify({"status": "uploaded"})


# ── Endpoint: historial ─────────────────────────────────
@app.route("/history")
def get_all_history():
    return jsonify(load_history())

@app.route("/history/<user>")
def get_user_history(user):
    history = load_history()
    return jsonify(history.get(user, []))

@app.route("/delete/<user>", methods=["DELETE"])
def delete_user(user):
    history = load_history()
    if user in history:
        del history[user]
    save_history(history)
    return jsonify({"status": "deleted"})


# ── Endpoint: presencia ─────────────────────────────────
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Llamado periódicamente por el frontend. active=False cuando la pestaña está en segundo plano sin actividad."""
    data = request.json
    user = data.get("user")
    active = data.get("active", True)

    if not user:
        return jsonify({"error": "missing user"}), 400

    user_presence[user] = {
        "last_seen": time.time(),
        "status": "online" if active else "away"
    }
    return jsonify({"status": "ok"})

@app.route("/offline", methods=["POST"])
def set_offline():
    """Llamado en beforeunload para marcar desconexión inmediata."""
    data = request.json
    user = data.get("user")
    if user:
        user_presence[user] = {"last_seen": time.time(), "status": "offline"}
    return jsonify({"status": "ok"})

@app.route("/users")
def users():
    result = []
    for user, entry in user_presence.items():
        if not user:   # ignorar entradas fantasma
            continue
        result.append({"name": user, "status": get_status(entry)})
    return jsonify(result)


# ── Home ────────────────────────────────────────────────
@app.route("/")
def home():
    return "AI Backend Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
