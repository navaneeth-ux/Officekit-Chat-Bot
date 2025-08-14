from fastapi import FastAPI, UploadFile, File,Form
from pydantic import BaseModel
from rasa.core.agent import Agent
from rasa.model import get_latest_model
import whisper
import tempfile
import shutil
import os
import httpx
import json
import asyncio



os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"


app = FastAPI()

agent = None
whisper_model = None

class InputText(BaseModel):
    text: str
    OfficeContent: dict
    Commonparam: dict


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
        whisper_model = whisper.load_model("tiny")
       # whisper_model = whisper.load_model("small")

    except Exception as e:
        print(f"❌ Failed to load Whisper model: {e}")
        whisper_model = None


# 🔹 Helper to fetch leave data
async def fetch_leave_summary(OfficeContent: dict, Commonparam: dict):
    url = (
        "https://m2h.officekithr.net/api/AjaxAPI/Leavecompilation"
        f"?OfficeContent={OfficeContent}"
        f"&Commonparam={Commonparam}"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)
            filtered = [
                {"LeaveCode": item.get("LeaveCode"), "LeaveBalance": item.get("LeaveBalance")}
                for item in data if isinstance(item, dict)
            ]
            return {
                "responseCode": "0000",
                "responseData": "Completed successfully",
                "leave_summary": filtered
            }
        except Exception as e:
            return {
                "responseCode": "1002",
                "responseData": f"Failed to parse JSON: {e}"
            }
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to fetch leave compilation",
            "details": response.text
        }


# 🔹 Format leave response uniformly
def format_leave_response(leave_data, code, leave_name):
    leave = next((item for item in leave_data.get("leave_summary", []) if item.get("LeaveCode") == code), None)
    if leave:
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": f"You have {leave['LeaveBalance']} {leave_name} leaves left"
        }
    else:
        return {
            "responseCode": "0001",
            "responseData": "Something went wrong",
            "message": f"{leave_name} leave not found"
        }


# 🔹 Handle all leave-related intents
async def handle_leave_intent(intent, OfficeContent, Commonparam):
    leave_data = await fetch_leave_summary(OfficeContent, Commonparam)

    leave_map = {
        "available_casual_leaves": ("CL", "Casual"),
        "available_com_leaves": ("COM", "Compensatory"),
        "available_sl_leaves": ("SL", "Sick"),
        "available_lop_leaves": ("LOP", "Loss Of Pay")
    }

    if intent in leave_map:
        code, name = leave_map[intent]
        return format_leave_response(leave_data, code, name)

    elif intent == "apply_leave":
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": "Leave request initiated"
        }

    elif intent == "available_leaves":
        return leave_data

    else:
        
         {
            "responseCode": "0001",
            "responseData": "Something went wrong",
            "message": "Sorry, I didn't understand that."
        }


# 🔹 Text endpoint
@app.post("/analyze/")
async def analyze_text(input: InputText):

    result = await agent.parse_message(input.text)
    intent = result.get("intent", {}).get("name")
    return await handle_leave_intent(intent, input.OfficeContent, input.Commonparam)


# 🔹 Audio endpoint
@app.post("/analyze_audio/")
async def analyze_audio(
    file: UploadFile = File(...),
    OfficeContent: str = Form(...),
    Commonparam: str = Form(...)
):
    # Convert JSON strings to Python dicts
    OfficeContent = json.loads(OfficeContent)
    Commonparam = json.loads(Commonparam)

    ext = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    transcription = whisper_model.transcribe(tmp_path)
    text = transcription["text"]
    print(f"🎤 Transcribed audio text: {text}")

    result = await agent.parse_message(text)
    intent = result.get("intent", {}).get("name")
    print(f"🎤 intend ========== {intent}")

    return await handle_leave_intent(intent, OfficeContent, Commonparam)
