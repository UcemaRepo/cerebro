import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DATA_FILE = "conversations.json"
PERSONALITY_FILE = "personality.txt"

last_uploaded_text = ""


# cargar historial
def load_history():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE,"r") as f:
        return json.load(f)

# guardar historial
def save_history(history):
    with open(DATA_FILE,"w") as f:
        json.dump(history,f,indent=2)

# cargar personalidad
def load_personality():
    if not os.path.exists(PERSONALITY_FILE):
        return "Eres un asistente útil."
    with open(PERSONALITY_FILE,"r") as f:
        return f.read()

# guardar personalidad
def save_personality(text):
    with open(PERSONALITY_FILE,"w") as f:
        f.write(text)

# endpoint chat
@app.route("/chat", methods=["POST"])
def chat():

    global last_uploaded_text

    data = request.json
    message = data.get("message")

    history = load_history()
    personality = load_personality()

    messages = [
        {"role":"system","content":personality}
    ]

    # historial
    for h in history[-10:]:
        messages.append({"role":"user","content":h["message"]})
        messages.append({"role":"assistant","content":h["reply"]})

    # 👇 AQUI VA EL CAMBIO
    if last_uploaded_text:
        messages.append({
            "role":"system",
            "content": "El usuario subió el siguiente documento para analizar:\n\n" + last_uploaded_text
        })

    # mensaje del usuario
    messages.append({"role":"user","content":message})

    response = client.chat.completions.create(
        model="gpt-5",
        messages=messages
    )

    reply = response.choices[0].message.content

    history.append({
        "message":message,
        "reply":reply
    })

    save_history(history)

    return jsonify({"reply":reply})


    save_history(history)

    return jsonify({"reply":reply})


# guardar personalidad
@app.route("/personality", methods=["POST"])
def personality():

    data = request.json
    text = data.get("personality")

    save_personality(text)

    return jsonify({"status":"ok"})


# subir archivos
@app.route("/upload", methods=["POST"])
def upload():

    global last_uploaded_text

    file = request.files["file"]

    os.makedirs("uploads", exist_ok=True)

    path = os.path.join("uploads", file.filename)

    file.save(path)

    # leer archivo
    with open(path, "r", encoding="utf-8") as f:
        last_uploaded_text = f.read()

    return jsonify({"status":"uploaded"})



# historial
@app.route("/history", methods=["GET"])
def history():

    history = load_history()

    return jsonify(history)


@app.route("/")
def home():
    return "AI Backend Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


