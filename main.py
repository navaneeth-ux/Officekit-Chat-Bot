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
from datetime import datetime
import calendar
import re



os.environ["PATH"] += os.pathsep + r"C:\ffmpeg\bin"


app = FastAPI()

agent = None
whisper_model = None

class InputText(BaseModel):
    text: str
    OfficeContent: dict
    Commonparam: dict

leave_requests = {}  # key = uid, value = partial leave info

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



# 🔹 Helper to fetch payroll periods
async def fetch_payroll_periods(OfficeContent: dict, Commonparam: dict):
    base_url = Commonparam.get("Domain", "").rstrip("/")  # remove trailing slash if any

    Commonparam["AddNextYear"] = "2025"

    url = (
        f"{base_url}/FillPayRollPeriod"
        f"?OfficeContent={json.dumps(OfficeContent)}"
        f"&Commonparam={json.dumps(Commonparam)}"
    )
    print("📤 Request URL:", url)

    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)  # 👈 debug

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

# 🔹 Helper to fetch salary slip
async def fetch_salary_slip(OfficeContent: dict, ProcessPayRollID: int,Commonparam:dict):
    base_url = Commonparam.get("Domain", "").rstrip("/")  # remove trailing slash if any

    Commonparam = {"ProcessPayRollID": ProcessPayRollID}
    url = (
        f"{base_url}/GetSalarySlip"
        f"?OfficeContent={json.dumps(OfficeContent)}"
        f"&Commonparam={json.dumps(Commonparam)}"
    )

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



# 🔹 Helper to fetch leave data
async def fetch_leave_summary(OfficeContent: dict, Commonparam: dict):
    base_url = Commonparam.get("Domain", "").rstrip("/")  # remove trailing slash if any

    url = (
        f"{base_url}/Leavecompilation"
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
                {"LeaveCode": item.get("Description"), "LeaveBalance": item.get("LeaveBalance")}
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
    


# 🔹 Helper to fetch upcoming holidays
async def fetch_upcoming_holidays(OfficeContent: dict, Commonparam: dict):
    base_url = Commonparam.get("Domain", "").rstrip("/")  # remove trailing slash if any

    Commonparam["CurYear"] = str(datetime.now().year)

    url = (
        f"{base_url}/GetHolidayList"
        f"?OfficeContent={json.dumps(OfficeContent)}"
        f"&Commonparam={json.dumps(Commonparam)}"
    )
    print("📤 Request URL:", url)


    async with httpx.AsyncClient() as client:
        response = await client.post(url)
        print("🔎 Raw Response Text:", response.text)  # 👈 debug

    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, str):
                data = json.loads(data)

            today = datetime.today().date()

            # 🔹 Filter holidays after today
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
                            "Location": item.get("Location")
                        })
                except Exception:
                    continue

            return {
                "responseCode": "0000",
                "responseData": "Completed successfully",
                "upcoming_holidays": upcoming
            }

        except Exception as e:
            return {
                "responseCode": "1002",
                "responseData": f"Failed to parse JSON: {e}"
            }
    else:
        return {
            "responseCode": str(response.status_code),
            "responseData": "Failed to fetch holiday list",
            "details": response.text
        }



    


# 🔹 Format leave response uniformly
def format_leave_response(leave_data, code, leave_name):
    # Use leave_name for filtering since API returns full names like "Casual Leave"
    leave = next(
        (item for item in leave_data.get("leave_summary", []) if item.get("LeaveCode") == leave_name),
        None
    )
    if leave:
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": f"You have {leave['LeaveBalance']} {leave_name} left"
        }
    else:
        return {
            "responseCode": "0001",
            "responseData": "Something went wrong",
            "message": f"{leave_name} not found"
        }
# 🔹 Handle all leave-related intents
async def handle_intent(intent, OfficeContent, Commonparam, text: str):

    leave_map = {
    "available_casual_leaves": ("CL", "Casual Leave"),
    "available_com_leaves": ("COM", "Compensatory Leave"),
    "available_sl_leaves": ("SL", "Sick Leave"),
    "available_lop_leaves": ("LOP", "Loss of Pay"),
    "available_ent_leaves": ("ENT", "Electricity And Network Trouble Leave")
    }

    if intent in leave_map:
        leave_data = await fetch_leave_summary(OfficeContent, Commonparam)

        code, name = leave_map[intent]
        return format_leave_response(leave_data, code, name)
    
    elif intent == "greet":
        return {
            "responseCode": "0000",
            "responseData": "Completed successfully",
            "message": "Hi How can I help you"
        } 
    


    elif intent == "upcoming_holidays":
        leave_data = await fetch_upcoming_holidays(OfficeContent, Commonparam)
        return leave_data

    elif intent == "available_leaves":
        leave_data = await fetch_leave_summary(OfficeContent, Commonparam)
        return leave_data


    elif intent == "pay_slip":
        payroll_periods = await fetch_payroll_periods(OfficeContent, Commonparam)
        if isinstance(payroll_periods, dict) and payroll_periods.get("error"):
          return {
            "responseCode": "1001",
            "responseData": payroll_periods["error"]
         } 
     
        first_period = payroll_periods[0] if payroll_periods else None
     
        ProcessPayRollID = first_period["ProcessPayRollID"]
        salary_slip = await fetch_salary_slip(OfficeContent, ProcessPayRollID,Commonparam)
    
        return {
        "responseCode": "0000",
        "responseData": "Completed successfully",
        "message":"pay_slip",
        "salary_slip": salary_slip
             }
    


 # fetch pay slip of specific month 
    elif intent == "pay_slip_of_month":
    # ✅ collect month names + abbreviations
     months = [m.lower() for m in calendar.month_name if m] + \
             [m.lower() for m in calendar.month_abbr if m]  
    # ✅ find the month mentioned in text
     month_found = None
     for m in months:
        if m in text.lower():
            month_found = m
            break

     if not month_found:
        return {
            "responseCode": "1003",
            "responseData": "Month not found in text",
            "message": "Please specify a valid month (e.g., January)"
        }

     try:
        month_number = list(calendar.month_name).index(month_found.capitalize())
     except ValueError:
        month_number = list(calendar.month_abbr).index(month_found.capitalize())

     # ✅ fetch payroll periods
     payroll_periods = await fetch_payroll_periods(OfficeContent, Commonparam)
     if isinstance(payroll_periods, dict) and payroll_periods.get("error"):
        if not target_period:
         return {
            "responseCode": "1001",
            "responseData": payroll_periods["error"]
        }

            # Convert month name/abbr to number
     target_period = next(
        (p for p in payroll_periods if p.get("Payrollmonth") == month_number),
        None
     )

        # Fetch payroll periods
     if not target_period:
        return {
            "responseCode": "1004",
            "responseData": f"No payroll found for {month_found.capitalize()}"
        }

     ProcessPayRollID = target_period["ProcessPayRollID"]


     salary_slip = await fetch_salary_slip(OfficeContent, ProcessPayRollID,Commonparam)


     ProcessPayRollID = target_period["ProcessPayRollID"]
     salary_slip = await fetch_salary_slip(OfficeContent, ProcessPayRollID,Commonparam)

     return {
        "responseCode": "0000",
        "responseData": "Completed successfully",
        "message": f"Payslip for {month_found.capitalize()}",
        "salary_slip": salary_slip
    }

   

    


    elif intent == "apply_leave":
     uid = OfficeContent.get("uid") or "default"
     if uid not in leave_requests:
        leave_requests[uid] = {}

     leave_info = leave_requests[uid]

     # Step 1: Ask for leave type
     if "LeaveID" not in leave_info:
        leave_info["step"] = "leave_type"
        return {
            "responseCode": "0000",
            "responseData": "Need leave type",
            "message": "What type of leave would you like to apply? (Casual / Sick / etc.)"
        }

     # Step 2: Ask for leave dates
     elif "Leavefrom" not in leave_info or "Leaveto" not in leave_info:
        leave_info["step"] = "leave_dates"
        return {
            "responseCode": "0000",
            "responseData": "Need leave dates",
            "message": "Please provide the start and end dates for your leave."
        }

     # Step 3: Ask for reason
     elif "Reason" not in leave_info:
        leave_info["step"] = "leave_reason"
        return {
            "responseCode": "0000",
            "responseData": "Need reason",
            "message": "What is the reason for your leave?"
        }

     # Step 4: All info available → Call SaveLeaveApplication API
     else:
        Commonparam.update({
            "Mode": "save",
            "LeaveID": leave_info["LeaveID"],
            "Leavefrom": leave_info["Leavefrom"],
            "Leaveto": leave_info["Leaveto"],
            "Noofleavedays": 1,  # You can calculate difference
            "Reason": leave_info["Reason"],
            "Approvalstatus": "P"
        })

        base_url = Commonparam.get("Domain", "").rstrip("/")
        url = (
            f"{base_url}/SaveLeaveApplication"
            f"?OfficeContent={json.dumps(OfficeContent)}"
            f"&Commonparam={json.dumps(Commonparam)}"
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(url)

        del leave_requests[uid]  # cleanup after submit
        return response.json()




        ####
    elif intent == "nlu_fallback":
     return {
        "responseCode": "0002",
        "responseData": "Fallback triggered",
        "message": "Sorry, I didn’t understand that. Can you rephrase?"
    }



    else:
        
         return{
        "responseCode": "0002",
        "responseData": "Fallback triggered",
        "message": "Sorry, I didn’t understand that. Can you rephrase?"

         }


# 🔹 Text endpoint
@app.post("/analyze/")
async def analyze_text(input: InputText):

    result = await agent.parse_message(input.text)
    intent = result.get("intent", {}).get("name")
    return await handle_intent(intent, input.OfficeContent, input.Commonparam,input.text)


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

    return await handle_intent(intent, OfficeContent, Commonparam)
