from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from rasa.core.agent import Agent
from rasa.model import get_latest_model
import whisper
import tempfile
import shutil
import os
import httpx
import json
from datetime import datetime, timedelta
import calendar
import re
from rasa.core.channels.channel import UserMessage

os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"

app = FastAPI()

agent = None
whisper_model = None

class InputText(BaseModel):
    text: str
    OfficeContent: dict
    Commonparam: dict

# In-memory multi-turn state for leave flow (keyed by user uid)
leave_requests = {}  # { uid: { step, LeaveID, Leavefrom, Leaveto, Reason } }

# -----------------------------
# Utilities
# -----------------------------

def build_base_url(commonparam: dict) -> str:
    """
    Returns a base URL that safely points to the AjaxAPI root.
    Accepts either:
      - http://host:port             -> expands to http://host:port/api/AjaxAPI
      - http://host:port/api/AjaxAPI -> used as-is
    """
    base = (commonparam or {}).get("Domain", "").rstrip("/")
    if not base:
        raise ValueError("Commonparam['Domain'] is required.")
    # Detect if already pointing to AjaxAPI
    lowered = base.lower()
    if lowered.endswith("/api/ajaxapi"):
        return base
    # If it doesn't look like an API root, append
    if "/api/" not in lowered:
        return base + "/api/AjaxAPI"
    return base  # assume caller passed a valid API root like /api/AjaxAPI

def api_url(commonparam: dict, endpoint: str) -> str:
    return f"{build_base_url(commonparam).rstrip('/')}/{endpoint.lstrip('/')}"

def fmt_date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")

def parse_date_token(tok: str):
    """
    Try to parse a single date token like 20/08/2025 or 20-08-2025
    Returns datetime or None.
    """
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(tok, fmt)
        except Exception:
            pass
    return None

def extract_dates_from_text(text: str):
    """
    Extract 0/1/2 dates (From, To) from free text.
    Supports dd/mm/yyyy or dd-mm-yyyy, plus 'today' / 'tomorrow'.
    If only 1 date found -> From=To=that date.
    Returns (from_dt, to_dt) or (None, None) if nothing found.
    """
    text_lower = text.lower()

    # today/tomorrow quick checks
    if "today" in text_lower:
        d = datetime.today()
        return d, d
    if "tomorrow" in text_lower:
        d = datetime.today() + timedelta(days=1)
        return d, d

    # regex for dd/mm/yyyy or dd-mm-yyyy
    raw_dates = re.findall(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", text)
    parsed = [parse_date_token(d) for d in raw_dates]
    parsed = [d for d in parsed if d is not None]

    if len(parsed) >= 2:
        return parsed[0], parsed[1]
    elif len(parsed) == 1:
        return parsed[0], parsed[0]
    else:
        return None, None

def parse_leave_type(text: str):
    """
    Very simple leave-type mapper. Adjust IDs to match your backend.
    """
    text_l = text.lower()
    mapping = [
        (["casual", "cl"], 1, "Casual Leave"),
        (["sick", "sl", "medical"], 2, "Sick Leave"),
        (["compensatory", "com"], 3, "Compensatory Leave"),
        (["lop", "loss of pay"], 4, "Loss of Pay"),
        (["earned", "el"], 5, "Earned Leave"),
    ]
    for keywords, leave_id, name in mapping:
        if any(k in text_l for k in keywords):
            return leave_id, name
    return None, None

def inclusive_days(from_dt: datetime, to_dt: datetime) -> int:
    return (to_dt.date() - from_dt.date()).days + 1

# -----------------------------
# Startup
# -----------------------------

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

# -----------------------------
# Backend API helpers
# -----------------------------

async def fetch_payroll_periods(OfficeContent: dict, Commonparam: dict):
    Commonparam = dict(Commonparam or {})
    Commonparam["AddNextYear"] = "2025"

    url = api_url(Commonparam, "FillPayRollPeriod")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(Commonparam)}"
    print("📤 Request URL:", url)

    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)
            return data
        except Exception as e:
            return {"error": f"Failed to parse JSON: {e}"}
    else:
        return {"error": f"Failed to fetch payroll periods: {response.text}"}

async def fetch_salary_slip(OfficeContent: dict, ProcessPayRollID: int, Commonparam: dict):
    # Only pass ProcessPayRollID to Commonparam for this API
    cp = {"ProcessPayRollID": ProcessPayRollID}
    url = api_url(Commonparam, "GetSalarySlip")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(cp)}"

    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)
            return data
        except Exception as e:
            return {"error": f"Failed to parse JSON: {e}"}
    else:
        return {"error": f"Failed to fetch salary slip: {response.text}"}

async def fetch_leave_summary(OfficeContent: dict, Commonparam: dict):
    url = api_url(Commonparam, "Leavecompilation")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(Commonparam)}"

    async with httpx.AsyncClient() as client:
        response = await client.post(url)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)
            filtered = [
                {"LeaveCode": item.get("Description"), "LeaveBalance": item.get("LeaveBalance")}
                for item in data if isinstance(item, dict)
            ]
            return {
                "responseCode": "0000",
                "responseData": "Completed successfully",
                "leave_summary": filtered,
            }
        except Exception as e:
            return {"responseCode": "1002", "responseData": f"Failed to parse JSON: {e}"}
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to fetch leave compilation",
            "details": response.text,
        }

async def fetch_upcoming_holidays(OfficeContent: dict, Commonparam: dict):
    cp = dict(Commonparam or {})
    cp["CurYear"] = str(datetime.now().year)

    url = api_url(Commonparam, "GetHolidayList")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(cp)}"
    print("📤 Request URL:", url)

    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)

            today = datetime.today().date()
            upcoming = []
            for item in data:
                try:
                    from_date = datetime.strptime(item.get("FromDate"), "%d/%m/%Y").date()
                    if from_date >= today:
                        upcoming.append({
                            "Holiday_Name": item.get("Holiday_Name"),
                            "FromDate": item.get("FromDate"),
                            "ToDate": item.get("ToDate"),
                            "RestrictedHoliday": item.get("RestrictedHoliday"),
                            "PayType": item.get("PayType"),
                            "Location": item.get("Location"),
                        })
                except Exception:
                    continue

            return {
                "responseCode": "0000",
                "responseData": "Completed successfully",
                "upcoming_holidays": upcoming,
            }
        except Exception as e:
            return {"responseCode": "1002", "responseData": f"Failed to parse JSON: {e}"}
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to fetch holiday list",
            "details": response.text,
        }

def format_leave_response(leave_data, code, leave_name):
    leave = next(
        (item for item in leave_data.get("leave_summary", []) if item.get("LeaveCode") == leave_name),
        None,
    )
    if leave:
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": f"You have {leave['LeaveBalance']} {leave_name} left",
        }
    else:
        return {
            "responseCode": "0001",
            "responseData": "Something went wrong",
            "message": f"{leave_name} not found",
        }

async def save_leave_application(OfficeContent: dict, Commonparam: dict, payload: dict):
    """
    Calls SaveLeaveApplication with payload merged into Commonparam.
    """
    cp = dict(Commonparam or {})
    cp.update(payload)
    url = api_url(Commonparam, "SaveLeaveApplication")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(cp)}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(url)
        print(f"apply leave request {url}")


    try:
        data = resp.json()
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        data = {"status_code": resp.status_code, "raw": resp.text}

    return data

# -----------------------------
# Intent handler
# -----------------------------

async def handle_intent(intent, OfficeContent, Commonparam, text: str):
    # If a leave flow is ongoing for this uid, continue it regardless of intent misclassifications
    uid = (OfficeContent or {}).get("uid") or "default"

    # --- Leave-related map used elsewhere ---
    leave_map = {
        "available_casual_leaves": ("CL", "Casual Leave"),
        "available_com_leaves": ("COM", "Compensatory Leave"),
        "available_sl_leaves": ("SL", "Sick Leave"),
        "available_lop_leaves": ("LOP", "Loss of Pay"),
        "available_ent_leaves": ("ENT", "Electricity And Network Trouble Leave"),
    }

    # ---------------- GREET ----------------
    if intent == "greet":
        return {"responseCode": "0000", "responseData": "Completed successfully", "message": "Hi, how can I help you?"}

    # ------------- UPCOMING HOLIDAYS -------------
    if intent == "upcoming_holidays":
        return await fetch_upcoming_holidays(OfficeContent, Commonparam)

    # ------------- LEAVE SUMMARY -------------
    if intent == "available_leaves":
        return await fetch_leave_summary(OfficeContent, Commonparam)

    if intent in leave_map:
        leave_data = await fetch_leave_summary(OfficeContent, Commonparam)
        code, name = leave_map[intent]
        return format_leave_response(leave_data, code, name)

    # ------------- PAY SLIP (LATEST) -------------
    if intent == "pay_slip":
        payroll_periods = await fetch_payroll_periods(OfficeContent, Commonparam)
        if isinstance(payroll_periods, dict) and payroll_periods.get("error"):
            return {"responseCode": "1001", "responseData": payroll_periods["error"]}

        first_period = payroll_periods[0] if payroll_periods else None
        if not first_period:
            return {"responseCode": "1004", "responseData": "No payroll periods found"}

        process_id = first_period.get("ProcessPayRollID")
        salary_slip = await fetch_salary_slip(OfficeContent, process_id, Commonparam)
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": "Last month's payslip",
            "salary_slip": salary_slip,
        }

    # ------------- PAY SLIP OF MONTH -------------
    if intent == "pay_slip_of_month":
        # collect month names + abbreviations
        months = [m.lower() for m in calendar.month_name if m] + [m.lower() for m in calendar.month_abbr if m]
        month_found = None
        t_low = text.lower()
        for m in months:
            if m and m in t_low:
                month_found = m
                break

        if not month_found:
            return {
                "responseCode": "1003",
                "responseData": "Month not found in text",
                "message": "Please specify a valid month (e.g., January)",
            }

        # convert name/abbr to number
        try:
            month_number = list(calendar.month_name).index(month_found.capitalize())
        except ValueError:
            month_number = list(calendar.month_abbr).index(month_found.capitalize())

        payroll_periods = await fetch_payroll_periods(OfficeContent, Commonparam)
        if isinstance(payroll_periods, dict) and payroll_periods.get("error"):
            return {"responseCode": "1001", "responseData":"something went wrong","message": payroll_periods["error"]}

        target_period = next((p for p in payroll_periods if p.get("Payrollmonth") == month_number), None)
        if not target_period:
            return {"responseCode": "0000","responseData":"Completed Successfully", "message": f"No payroll found for {month_found.capitalize()}"}

        process_id = target_period["ProcessPayRollID"]
        salary_slip = await fetch_salary_slip(OfficeContent, process_id, Commonparam)

        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": f"Payslip for {month_found.capitalize()}",
            "salary_slip": salary_slip,
        }

    # ------------- APPLY LEAVE (multi-turn, no Rasa forms) -------------
    # Continue an ongoing leave flow even if Rasa intent isn't apply_leave,
    # as long as there is a partial record for this uid.
    if intent == "apply_leave" or uid in leave_requests:
        info = leave_requests.get(uid, {})
        # Try to auto-fill from the incoming text if fields are missing
        if "LeaveID" not in info:
            leave_id, leave_name = parse_leave_type(text)
            if leave_id:
                info["LeaveID"] = leave_id
                info["LeaveName"] = leave_name

        if "Leavefrom" not in info or "Leaveto" not in info:
            d1, d2 = extract_dates_from_text(text)
            if d1 and d2:
                info["Leavefrom"] = fmt_date(d1)
                info["Leaveto"] = fmt_date(d2)

        if "Reason" not in info:
            # crude heuristic: anything after 'because' or 'reason' becomes reason
            m = re.search(r"(?:because|reason is|reason)\s+(.+)", text, re.IGNORECASE)
            if m:
                info["Reason"] = m.group(1).strip()

        # Persist the partial info
        leave_requests[uid] = info

        # Now prompt for the next missing piece
        if "LeaveID" not in info:
            return {
                "responseCode": "0000",
                "responseData": "Need leave type",
                "message": "What type of leave would you like to apply? (Casual / Sick / etc.)",
            }

        if "Leavefrom" not in info or "Leaveto" not in info:
            return {
                "responseCode": "0000",
                "responseData": "Need leave dates",
                "message": "Please share the dates Eg(08/22/2025)",
            }

       # if "Reason" not in info:
            return {
                "responseCode": "0000",
                "responseData": "Need reason",
                "message": "What is the reason for your leave?",
            }

        # All info is present → call API
        try:
            leave_from_dt = datetime.strptime(info["Leavefrom"], "%d/%m/%Y")
            leave_to_dt = datetime.strptime(info["Leaveto"], "%d/%m/%Y")
        except Exception:
            # If parsing failed, ask again
            # (this can happen if user typed invalid date format)
            info.pop("Leavefrom", None)
            info.pop("Leaveto", None)
            leave_requests[uid] = info
            return {
                "responseCode": "1005",
                "responseData": "Invalid date format",
                "message": "Please resend dates in dd/mm/yyyy format (e.g., 20/08/2025 to 22/08/2025).",
            }

        payload = { 
            "Mode": "save",
            "LeaveID": info["LeaveID"],
            "Leavefrom": info["Leavefrom"],
            "Leaveto": info["Leaveto"],
            "Offdaysfrom": info["Leavefrom"],
            "Offdaysto": info["Leaveto"],
            "Noofleavedays": inclusive_days(leave_from_dt, leave_to_dt),
            "Timemode": 1,
            "Reason": "Medical leave",  # <-- hardcoded instead of info["fever"]
            "Holiday": 0,
            "Weekend": 0,
            "Daysleaveclubbing": 0,
            "LeavePolicyInstanceLimitID": 0,
            "Returndate": fmt_date(leave_to_dt + timedelta(days=1)),
            "Approvalstatus": "P",
            "Firsthalf": 0,
            "Lasthalf": 0,
            "Roledeligation": 0,
            "Contactaddress": "",
            "Contactnumber": "",
            "Salaryadvance": 0,
            "IsNoticePeriod": 0,
            "Passportrequest": 0,
            "Roldleavetrantype": None,
            "Duallaps": 0,
            "Balancedaystofuture": 0,
        }

        api_result = await save_leave_application(OfficeContent, Commonparam, payload)
        # cleanup after submit
        leave_requests.pop(uid, None)

        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": "Leave application submitted.",
            "api_result": api_result,
            "submitted": payload,
        }

    # ------------- FALLBACK -------------
    if intent == "nlu_fallback":
        return {
            "responseCode": "0002",
            "responseData": "Fallback triggered",
            "message": "Sorry, I didn’t understand that. Can you rephrase?",
        }

    # Generic fallback
    return {
        "responseCode": "0002",
        "responseData": "Fallback triggered",
        "message": "Sorry, I didn’t understand that. Can you rephrase?",
    }

# -----------------------------
# Endpoints
# -----------------------------

@app.post("/analyze/")
async def analyze_text(input: InputText):
    result = await agent.parse_message(input.text)
    intent = result.get("intent", {}).get("name")
    return await handle_intent(intent, input.OfficeContent, input.Commonparam, input.text)

@app.post("/analyze-rasa/")
async def analyze_rasa(input: InputText):
    sender_id = input.OfficeContent.get("uid", "default_user")

    # Create a UserMessage
    message = UserMessage(text=input.text, sender_id=sender_id)

    # Handle with Rasa agent
    responses = await agent.handle_message(message)

    if responses:
        last_response = responses[-1]
        return last_response
    else:
        return {"responseCode": "0002", "responseData": "Fallback", "message": "No response from Rasa."}


@app.post("/analyze-rasas/")
async def analyze_rasa(input: InputText):
    sender_id = input.OfficeContent.get("uid", "default_user")

    # Let Rasa always process the message first
    message = UserMessage(text=input.text, sender_id=sender_id)
    responses = await agent.handle_message(message)

    # Extract detected intent from tracker state or from responses metadata
    # (or parse separately ONLY if you don’t call handle_message after)
    nlu_result = await agent.parse_message(input.text)
    intent = nlu_result.get("intent", {}).get("name")

    if intent != "apply_leave":
        # Handle other intents yourself
        return await handle_intent(intent, input.OfficeContent, input.Commonparam, input.text)

    if responses:
        return responses[-1]
    else:
        return {"responseCode": "0002", "responseData": "Fallback", "message": "No response from Rasa."}


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
    print(f"🎤 intent = {intent}")

    return await handle_intent(intent, OfficeContent, Commonparam, text)




