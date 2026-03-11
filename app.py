import os
import json
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DATA_FILE = "conversations.json"
PERSONALITY_FILE = "personality.txt"


user_last_seen = {}
last_uploaded_text = ""
last_uploaded_image = None



# cargar historial
def load_history():

    if not os.path.exists(DATA_FILE):
        return {}

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
    global last_uploaded_image

    data = request.json

    message = data.get("message")
    user = data.get("user")
    user_last_seen[user] = time.time()

    history = load_history()
    personality = load_personality()

    # crear historial si no existe
    if user not in history:
        history[user] = []

    user_history = history[user]

    messages = [
        {"role":"system","content":personality}
    ]

    # historial reciente
    for h in user_history[-10:]:
        messages.append({"role":"user","content":h["message"]})
        messages.append({"role":"assistant","content":h["reply"]})

    # documento subido
    if last_uploaded_text:
        messages.append({
            "role":"system",
            "content":"El usuario subió el siguiente documento:\n\n"+last_uploaded_text
        })

    # imagen subida
    if last_uploaded_image:

        import base64

        with open(last_uploaded_image,"rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")

        messages.append({
            "role":"user",
            "content":[
                {"type":"text","text":message},
                {
                    "type":"image_url",
                    "image_url":{
                        "url":f"data:image/jpeg;base64,{b64}"
                    }
                }
            ]
        })

    else:

        messages.append({"role":"user","content":message})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages
    )

    reply = response.choices[0].message.content

    # guardar conversación
    user_history.append({
        "message":message,
        "reply":reply
    })

    history[user] = user_history

    save_history(history)

    return jsonify({"reply":reply})




# guardar personalidad
@app.route("/personality", methods=["POST"])
def personality():

    data = request.json
    text = data.get("personality")

    save_personality(text)

    return jsonify({"status":"ok"})

@app.route("/personality", methods=["GET"])
def get_personality():

    return jsonify({
        "personality": load_personality()
    })


# subir archivos
@app.route("/upload", methods=["POST"])
def upload():

    global last_uploaded_text
    global last_uploaded_image

    file = request.files["file"]

    os.makedirs("uploads", exist_ok=True)

    path = os.path.join("uploads", file.filename)

    file.save(path)

    last_uploaded_text = ""
    last_uploaded_image = None

    filename = file.filename.lower()

    if filename.endswith(".txt") or filename.endswith(".csv"):

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            last_uploaded_text = f.read()

    elif filename.endswith(".jpg") or filename.endswith(".jpeg") or filename.endswith(".png"):

        last_uploaded_image = path

    else:

        return jsonify({"error": "Tipo de archivo no soportado"}), 400

    return jsonify({"status":"uploaded"})




# historial
@app.route("/history")
def get_all_history():
    return jsonify(load_history())


# BUG FIX: antes iteraba el dict como lista de objetos, nunca retornaba nada
@app.route("/history/<user>")
def get_user_history(user):

    history = load_history()

    # El historial está guardado como history[username] = [{message, reply}, ...]
    user_history = history.get(user, [])

    return jsonify(user_history)


@app.route("/users")
def users():

    now = time.time()

    result = []

    for user, last in user_last_seen.items():

        online = (now - last) < 30

        result.append({
            "name": user,
            "online": online
        })

    return jsonify(result)



@app.route("/delete/<user>", methods=["DELETE"])
def delete_user(user):

    history = load_history()

    if user in history:
        del history[user]

    save_history(history)

    return jsonify({"status":"deleted"})


@app.route("/")
def home():
    return "AI Backend Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)













