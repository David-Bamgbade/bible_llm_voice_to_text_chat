import eventlet
eventlet.monkey_patch()
from flask import Flask, request, jsonify, send_file
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
import json
from io import BytesIO
from gtts import gTTS
from flask_pymongo import PyMongo
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required,
    get_jwt_identity, verify_jwt_in_request
)
from flask_socketio import SocketIO, emit, disconnect
import os, datetime
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import logging

app = Flask(__name__)

# --- Configuration ---
app.config["SECRET_KEY"] = "super-secret-key"  # Change in production!
app.config["JWT_SECRET_KEY"] = "another-super-secret-key"  # Change in production!
app.config["MONGO_URI"] = "mongodb://localhost:27017/BibleLlmDb"

# Uncomment and configure CORS for production
# CORS(app, origins=["https://your-production-frontend.com"])

# Initialize extensions
mongo = PyMongo(app)
jwt = JWTManager(app)
# socketio = SocketIO(app, cors_allowed_origins=["http://localhost:8501"], async_mode="eventlet")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# --- WebSocket Event Handlers ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bible Data and FAISS Setup ---
# Path to your JSON dataset
filePath = "genesis.json"

# Initialize your SentenceTransformer model.
embedder = SentenceTransformer("all-MiniLM-L6-v2")


def load_bible_data(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        data = json.load(file)
    return data


# Load the Bible data
bible_data = load_bible_data(filePath)

# Create embeddings for each record's "text" and convert them to float32.
texts = [record["text"] for record in bible_data]
embeddings = embedder.encode(texts, convert_to_numpy=True).astype("float32")

# Create a FAISS index for fast similarity search.
index = faiss.IndexFlatL2(embeddings.shape[1])
index.add(embeddings)


def retrieve_bible_verse(question):
    """
    Given a question, encode it and search for the most similar Bible verse in the dataset.
    Returns the matching record (a dict with keys "book", "chapter", "verse", "text")
    or None if no match is found.
    """
    query_embedding = embedder.encode([question], convert_to_numpy=True).astype("float32")
    _, indices = index.search(query_embedding, 1)
    if indices is not None and len(indices[0]) > 0 and indices[0][0] != -1:
        return bible_data[indices[0][0]]
    else:
        return None


def answer_question(question):
    """
    Retrieves the Bible verse record that best matches the query and formats the reply.
    """
    record = retrieve_bible_verse(question)
    if record:
        reply = f"{record['book']} {record['chapter']}:{record['verse']} - {record['text']}"
        return reply
    else:
        return "No relevant verse found."


@app.route('/ask', methods=['POST'])
def query():
    data = request.json
    question = data.get("question")
    response_type = data.get("response_type", "text")

    if not question:
        return jsonify({"error": "Question is required"}), 400

    response = answer_question(question)

    if response_type == "voice":
        tts = gTTS(response, lang="en")
        audio_file = BytesIO()
        tts.write_to_fp(audio_file)
        audio_file.seek(0)
        return send_file(audio_file, mimetype="audio/mpeg", as_attachment=False, download_name="answer.mp3")
    else:
        return jsonify({"answer": response})


# --- User Registration & Login Endpoints ---
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    # Use "usersDetails" collection
    if mongo.db.usersDetails.find_one({"username": username}):
        return jsonify({"error": "Username already exists"}), 400

    hashed_password = generate_password_hash(password)
    mongo.db.usersDetails.insert_one({
        "username": username,
        "password": hashed_password
    })
    return jsonify({"message": "User registered successfully"}), 201


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    # Use "usersDetails" collection
    user = mongo.db.usersDetails.find_one({"username": username})
    if user and check_password_hash(user["password"], password):
        access_token = create_access_token(
            identity=str(user["_id"]),
            expires_delta=datetime.timedelta(hours=1)
        )
        return jsonify({
            "message": "Login successful",
            "access_token": access_token,
            "user_id": str(user["_id"]),
            "username": username
        }), 200

    return jsonify({"error": "Invalid credentials"}), 401


@app.route("/protected", methods=["GET"])
@jwt_required()
def protected():
    current_user = get_jwt_identity()
    return jsonify({"message": f"Hello user {current_user}, you have access!"}), 200


@socketio.on("connect", namespace="/")
def handle_connect():
    # Get the token and a fallback username from query parameters.
    token = request.args.get("token")
    query_username = request.args.get("username", "Guest")

    if not token:
        print("No token provided; connecting as Guest.")
        request.environ["user_id"] = None
        request.environ["username"] = query_username
        emit("message", {"msg": f"Connected as Guest ({query_username})."}, namespace="/")
        return

    try:
        # Place the token in the request header for verification.
        request.headers.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        verify_jwt_in_request(optional=True)
        # get_jwt_identity() may return a string or a dictionary.
        user_info = get_jwt_identity()
        if isinstance(user_info, str):
            # If it's a string, treat it as the user_id.
            username = query_username  # fallback to the username from query
            user_id = user_info
        elif isinstance(user_info, dict):
            # If it's a dict, try to get username and user_id.
            username = user_info.get("username", query_username)
            user_id = user_info.get("user_id", user_info.get("sub"))
        else:
            raise Exception("Invalid token payload")

        if not user_id:
            raise Exception("Invalid token: no user id")

        # Save the user information for later use.
        request.environ["user_id"] = user_id
        request.environ["username"] = username
        print(f"User {username} (ID: {user_id}) connected via WebSocket.")
        emit("message", {"msg": f"Connected as {username}."}, namespace="/")
    except Exception as e:
        print(f"WebSocket connection rejected: {e}")
        disconnect(namespace="/")
        return None


@socketio.on("chat", namespace="/")
def handle_chat(data):
    message = data.get("message")
    user_id = request.environ.get("user_id", "Guest")
    timestamp = datetime.datetime.utcnow().isoformat()
    # Optionally store the message in MongoDB.
    emit("chat", {"user_id": user_id, "message": message, "timestamp": timestamp}, broadcast=True, namespace="/")


@socketio.on("disconnect", namespace="/")
def handle_disconnect():
    print("Client disconnected.")


# if __name__ == '__main__':
#     socketio.run(app, debug=False)

if __name__ == '__main__':
    socketio.run(app, host="127.0.0.1", port=5000, debug=False)

