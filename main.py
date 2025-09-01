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
from rasa.core.channels.channel import CollectingOutputChannel
from rasa.shared.core.events import SlotSet, AllSlotsReset
import logging
from logging.handlers import RotatingFileHandler



os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"

logger = logging.getLogger("fastapi-rasa")
logger.setLevel(logging.INFO)

# Log format
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Rotating file handler (5 MB max, keep 5 backups)
file_handler = RotatingFileHandler(
    "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(formatter)

# Attach handlers (avoid duplicates if reloaded)
if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

logger.info("🚀 Logging initialized. FastAPI starting...")

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
        logger.info("Loading the rasa moodel")

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

#API TO CALL LEAVE SUBMIT API
async def submit_leave_application(
    OfficeContent: dict,
    Commonparam: dict,
    leave_type: str,
    leave_to: str,
    reason: str
):
    Commonparamforleavelist = {"Description": "leavelistApp"}

    url = api_url(Commonparam, "Leavecompilation")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(Commonparamforleavelist)}"
    async with httpx.AsyncClient() as client:
        response = await client.post(url)

     


      
    # ✅ sanitize Commonparam first
    allowed_keys = {
        "Mode", "LeaveID", "Leavefrom", "Leaveto", "Offdaysfrom", "Offdaysto",
        "Noofleavedays", "Timemode", "Reason", "Holiday", "Weekend",
        "Daysleaveclubbing", "LeavePolicyInstanceLimitID", "Returndate",
        "Approvalstatus", "Firsthalf", "Lasthalf", "Roledeligation",
        "Contactaddress", "Contactnumber", "Salaryadvance", "IsNoticePeriod",
        "Passportrequest", "Roldleavetrantype", "Duallaps", "Balancedaystofuture"
    }
    cp = {k: v for k, v in (Commonparam or {}).items() if k in allowed_keys}

    # Build Commonparam payload (leave_from == leave_to)
    cp.update({
        "Mode": "save",
        "LeaveID": 2,   # map to backend leave ID if required
        "Leavefrom": leave_to,
        "Leaveto": leave_to,
        "Offdaysfrom": leave_to,
        "Offdaysto": leave_to,
        "Noofleavedays": 1,
        "Timemode": 1,
        "Reason": reason,
        "Holiday": 0,
        "Weekend": 0,
        "Daysleaveclubbing": 0,
        "LeavePolicyInstanceLimitID": 0,
        "Returndate": leave_to,
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
    })

    # Build full URL with query params
    url = api_url(Commonparam, "SaveLeaveApplication")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(cp)}"
    print("📤 Request URL:", url)
    logger.info(f"📤 Request URL: {url}")


    async with httpx.AsyncClient() as client:
        response = await client.post(url, timeout=30.0)  # POST without body (all in query string)
        print("🔎 Raw Response Text:", response.text)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)

            return {
                "responseCode": "0000",
                "responseData": "Leave application submitted successfully",
                "api_result": data,
                "submitted": cp,   # debug payload
            }
        except Exception as e:
            return {"responseCode": "1002", "responseData": f"Failed to parse JSON: {e}"}
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to submit leave application",
            "details": response.text,
        }







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
    

#fetch policy data

async def fetch_policy_data(OfficeContent: dict, Commonparam: dict):
    url = api_url(Commonparam, "GetForm_PolicyData")
    url = f"{url}?OfficeContent={json.dumps(OfficeContent)}&Commonparam={json.dumps(Commonparam)}"
    print("📤 Request URL:", url)

    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):  # sometimes backend double-encodes JSON
                data = json.loads(data)

            return {
                "responseCode": "0004",
                "responseData": "Completed successfully",
                "policy": data,   
            }
        except Exception as e:
            return {
                "responseCode": "1002",
                "responseData": f"Failed to parse JSON: {e}",
                "raw": response.text,   # keep raw text for debugging
            }
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to fetch policy data",
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
    


    if intent == "policy_data":
     return await fetch_policy_data(OfficeContent, Commonparam)


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
            "responseCode": "0000",
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



@app.post("/analyze-old/")
async def analyze_rasa(input: InputText):
    sender_id = input.OfficeContent.get("uid", "default_user")

    nlu_result = await agent.parse_message(input.text)
    intent = nlu_result.get("intent", {}).get("name")

    if intent != "apply_leave":
        return await handle_intent(intent, input.OfficeContent, input.Commonparam, input.text)

    # Process message normally
    message = UserMessage(text=input.text, sender_id=sender_id)
    await agent.handle_message(message)

    # Get tracker to see what Rasa wants to ask next
    tracker = await agent.tracker_store.get_or_create_tracker(sender_id)
    
    # Get current slot values
    leave_type = tracker.get_slot("leave_type")

    leave_to = tracker.get_slot("leave_to") 
    reason = tracker.get_slot("reason")
    
    print(f"Current slots - leave_type: {leave_type}, leave_to: {leave_to}, reason: {reason}")
    
    # Return the appropriate question based on what's missing
    if not leave_type:
        return {
            "responseCode": "0000",
            "responseData": "success",
            "message": "What type of leave would you like to apply for? Please choose from:\n• **Sick** leave\n• **Casual** leave\n• **LOP** (Loss of Pay)"
        }
    



    elif not leave_to:
        return {
            "responseCode": "0000",
            "responseData": "Success",
            "message": "Please provide the end date of your leave (e.g., MM/DD/YYYY)."
        }
    elif not reason:
        return {
            "responseCode": "0000",
            "responseData": "Success",
            "message": "What is the reason for your leave?"
        }
    else:
        # All slots filled - form complete

        api_result = await submit_leave_application(
            OfficeContent=input.OfficeContent,
            Commonparam=input.Commonparam,
            leave_type=leave_type,
            leave_to=leave_to,
            reason=reason
        )


        return {
             
            "responseCode": "0000",
            "message": f"✅ Your {leave_type} application until {leave_to} for reason: {reason} has been submitted successfully!",
            "responseData": "Success",
            "api_result": api_result   # include API response for debugging

        }
    


@app.post("/analyze/")
async def analyze_rasa(input: InputText):
    sender_id = input.OfficeContent.get("uid", "default_user")
    
    # Get tracker to inspect form state
    nlu_result = await agent.parse_message(input.text)
    intent = nlu_result.get("intent", {}).get("name")

    tracker = await agent.tracker_store.get_or_create_tracker(sender_id)
    active_form_slot = tracker.get_slot("requested_slot")  # currently requested slot
    active_form_name = tracker.active_loop.name if tracker.active_loop else None
      
    if active_form_name and intent in ["cancel", "nlu_fallback"]:
     bot_message = await cancel_form(tracker, agent)

     return {
        "responseCode": "0000",
        "responseData": "success",
        "message": bot_message,
        "slots": {}
    }

    # Decide whether this message is normal intent or form input
    if active_form_name:

        message = UserMessage(text=input.text, sender_id=sender_id)
        await agent.handle_message(message)
    else:
        # Normal NLU intent detection

        if intent != "apply_leave":
            # call your custom handler for other intents
            return await handle_intent(intent, input.OfficeContent, input.Commonparam, input.text)

        # Process apply_leave normally
        message = UserMessage(text=input.text, sender_id=sender_id)
        await agent.handle_message(message)

    # Re-fetch tracker after handling message
    tracker = await agent.tracker_store.get_or_create_tracker(sender_id)
    
    # Get current slot values
    leave_type = tracker.get_slot("leave_type")
    leave_to = tracker.get_slot("leave_to")
    reason = tracker.get_slot("reason")

    # Determine what to ask next based on missing slots
    if not leave_type:
        bot_message = (
            "What type of leave would you like to apply for? Please choose from:\n"
            "• **Sick** leave\n• **Casual** leave\n• **LOP** (Loss of Pay)"
        )
    elif not leave_to:
        bot_message = "Please provide the end date of your leave (e.g., MM/DD/YYYY)."
    elif not reason:
        bot_message = "Can you provide a reason for your leave?"
    else:
         api_result = await submit_leave_application(
            OfficeContent=input.OfficeContent,
            Commonparam=input.Commonparam,
            leave_type=leave_type,
            leave_to=leave_to,
            reason=reason
         )
             # Clear slots after submission
         await cancel_form(tracker, agent)
         bot_message = "Application submitted successfully"

    return {
        "responseCode": "0000",
        "responseData": "success",
        "message": bot_message,
        "slots": {
            "leave_type": leave_type,
            "leave_to": leave_to,
            "reason": reason
        }
    }

async def cancel_form(tracker, agent):
    events = [
        SlotSet("leave_type", None),
        SlotSet("leave_from", None),
        SlotSet("leave_to", None),
        SlotSet("reason", None),
        SlotSet("requested_slot", None)
    ]
    tracker.active_loop = None
    for event in events:
        tracker.update(event)
    await agent.tracker_store.save(tracker)
    return "Your leave form has been cancelled. How can I help you now?"















RASA_URL = "http://localhost:5005/webhooks/rest/webhook"  # adjust if different
from fastapi import FastAPI, Request
import requests

@app.post("/analyze-test/")
async def analyze_test(request: Request):
    data = await request.json()
    text = data.get("text", "")
    office_content = data.get("OfficeContent", {})
    sender_id = office_content.get("uid", "default_user") 
    common_param = data.get("Commonparam", {})
 # extract uid

    # Send user input to Rasa
    message_payload = {
        "sender": sender_id,
        "message": text,
        "metadata": {
            "OfficeContent": office_content,
            "Commonparam": common_param
        }
    }

    # Send user input to Rasa
    rasa_response = requests.post(RASA_URL, json=message_payload)
    responses = rasa_response.json()

    # Extract only text messages from bot
    bot_messages = [r.get("text") for r in responses if r.get("text")]

    return {
        
        "input_text": text,
        "intent": responses,
        "bot_responses": bot_messages
    }


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




