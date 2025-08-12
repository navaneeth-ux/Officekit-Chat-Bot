from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
from rasa.core.agent import Agent
from rasa.model import get_latest_model
import whisper
import tempfile
import shutil
import os

os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"


app = FastAPI()

agent = None
whisper_model = None

class InputText(BaseModel):
    text: str

@app.on_event("startup")
def load_model():
    global agent, whisper_model
    try:
        model_path = get_latest_model()
        print(f"📦 Loading Rasa model from {model_path}")
        agent = Agent.load(model_path)
    except Exception as e:
        print(f"❌ Failed to load Rasa model: {e}")
        agent = None

    try:
        print("🎙 Loading Whisper model...")
        whisper_model = whisper.load_model("small")
    except Exception as e:
        print(f"❌ Failed to load Whisper model: {e}")
        whisper_model = None


@app.post("/analyze/")
async def analyze_text(input: InputText):
    print("🔵 analyze_text endpoint hit")

    result = await agent.parse_message(input.text)
    intent = result.get("intent", {}).get("name")

    if intent == "apply_leave":
        return {"message": "Leave request initiated"}
    elif intent == "available_leaves":
        return {"message": "alailable leaves request"}
    else:
        return{"message": "Sorry, I didn't understand that."}


@app.post("/analyze_audio/")
async def analyze_audio(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1]  # keep original extension (.mp3, .wav, etc.)
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    transcription = whisper_model.transcribe(tmp_path)
    text = transcription["text"]

    result = await agent.parse_message(text)
    intent = result.get("intent", {}).get("name")

    if intent == "apply_leave":
        return {"transcription": text, "message": "Leave request initiated"}
    elif intent == "available_leaves":
        return {"transcription": text, "message": "Available leaves request"}
    else:
        return {"transcription": text, "message": "Sorry, I didn't understand that."}

