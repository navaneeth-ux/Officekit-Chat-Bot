# actions.py

from typing import Any, Text, Dict, List
import requests
import json
import logging
from datetime import datetime, timedelta
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction
from rasa_sdk.types import DomainDict
from rasa_sdk.events import SlotSet, AllSlotsReset

# Set up logging for better debugging
logger = logging.getLogger(__name__)



class ActionSayLeave(Action):

    def name(self) -> Text:
        return "action_say_leave"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any]
    ) -> List[Dict[Text, Any]]:

        # Debug prints
        print("🔍 [ActionSayLeave] run() called")
        print("Slots at this point:", tracker.current_slot_values())
        print("Latest user message:", tracker.latest_message)

        # Get leave info from slots
        leave_type = tracker.get_slot("leave_type")
        date = tracker.get_slot("leave_to")

        print("leave_type slot:", leave_type)
        print("leave_to slot:", date)

        if not leave_type or not date:
            dispatcher.utter_message(text="Some leave information is missing.")
            print("⚠️ Missing info, asked user again")
        else:
            dispatcher.utter_message(
                text=f"✅ Your leave type is {leave_type} until {date}"
            )
            print("✅ Sent confirmation message to user")

        return []




class ActionSubmitLeave(Action):
    """Action to submit leave application to your API"""
    
    def name(self) -> Text:
        return "action_submit_leave"
    
    def calculate_leave_days(self, leave_from: str, leave_to: str) -> int:
        """Calculate number of leave days"""
        try:
            from_date = datetime.strptime(leave_from, "%d/%m/%Y")
            to_date = datetime.strptime(leave_to, "%d/%m/%Y")
            delta = to_date - from_date
            return delta.days + 1
        except Exception as e:
            logger.error(f"Error calculating leave days: {e}")
            return 1
    
    def calculate_return_date(self, leave_to: str) -> str:
        """Calculate return date (next working day after leave ends)"""
        try:
            to_date = datetime.strptime(leave_to, "%d/%m/%Y")
            return_date = to_date + timedelta(days=1)
            return return_date.strftime("%d/%m/%Y")
        except Exception as e:
            logger.error(f"Error calculating return date: {e}")
            return leave_to
    
    def map_leave_type_to_id(self, leave_type: str) -> int:
        """Map leave type to LeaveID"""
        leave_type_mapping = {
            "casual": 1,
            "sick": 2, 
            "compensatory": 3,
            "lop": 4
        }
        return leave_type_mapping.get(leave_type.lower(), 1)
    
    def format_date_for_api(self, date_str: str) -> str:
        """Convert DD/MM/YYYY to MM/DD/YYYY format for API"""
        try:
            parsed_date = datetime.strptime(date_str, "%d/%m/%Y")
            return parsed_date.strftime("%m/%d/%Y")
        except Exception as e:
            logger.error(f"Error formatting date for API: {e}")
            return date_str
    
    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        
        logger.info("🚀 ACTION_SUBMIT_LEAVE STARTED!")
        
        # Get slot values from the tracker
        leave_type = tracker.get_slot("leave_type")
        leave_from = tracker.get_slot("leave_from")
        leave_to = tracker.get_slot("leave_to")
        reason = tracker.get_slot("reason")
        
        logger.info(f"Slot values - Type: {leave_type}, From: {leave_from}, To: {leave_to}, Reason: {reason}")
        
        if not all([leave_type, leave_from, leave_to, reason]):
            logger.error("Missing required fields!")
            dispatcher.utter_message(text="❌ Missing required information. Please try applying for leave again.")
            return [AllSlotsReset()]
        
        try:
            leave_days = self.calculate_leave_days(leave_from, leave_to)
            return_date = self.calculate_return_date(leave_to)
            leave_id = self.map_leave_type_to_id(leave_type)
            
            logger.info(f"Calculated - Days: {leave_days}, Return: {return_date}, ID: {leave_id}")
            
            api_leave_from = self.format_date_for_api(leave_from)
            api_leave_to = self.format_date_for_api(leave_to)
            api_return_date = self.format_date_for_api(return_date)
            
            office_content = {
                "ApiKey": "wba1kit5p900egc12weblo2385",
                "uid": "E558947D-CBE0-4896-9C8F-4DD9628F6FEE"
            }
            
            common_param = {
                "Mode": "save",
                "LeaveID": leave_id,
                "Leavefrom": api_leave_from,
                "Leaveto": api_leave_to,
                "Offdaysfrom": api_leave_from,
                "Offdaysto": api_leave_to,
                "Noofleavedays": leave_days,
                "Timemode": 1,
                "Reason": reason,
                "Holiday": 0,
                "Weekend": 0,
                "Daysleaveclubbing": 0,
                "LeavePolicyInstanceLimitID": 0,
                "Returndate": api_return_date,
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
                "Balancedaystofuture": 0
            }
            
            api_url = "http://10.25.25.124:82/api/AjaxAPI/SaveLeaveApplication"
            
            params = {
                "OfficeContent": json.dumps(office_content),
                "Commonparam": json.dumps(common_param)
            }
            
            logger.info("Making API call...")
            dispatcher.utter_message(text="⏳ Submitting your leave application...")
            
            # Use a timeout to prevent the bot from hanging
            response = requests.get(api_url, params=params, timeout=30)
            
            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Content: {response.text[:500]}...")
            
            if response.status_code == 200:
                dispatcher.utter_message(
                    text=f"🎉 **Leave Application Submitted Successfully!**\n\n"
                         f"📋 **Application Details:**\n"
                         f"🔸 **Leave Type:** {leave_type.title()}\n"
                         f"🔸 **From:** {leave_from}\n"
                         f"🔸 **To:** {leave_to}\n"
                         f"🔸 **Number of Days:** {leave_days}\n"
                         f"🔸 **Return Date:** {return_date}\n"
                         f"🔸 **Reason:** {reason}\n"
                         f"🔸 **Status:** Pending Approval\n\n"
                         f"✅ You will receive a confirmation email shortly."
                )
            else:
                logger.error(f"API call failed with status {response.status_code}")
                dispatcher.utter_message(
                    text=f"❌ **Failed to submit leave application**\n\n"
                         f"🔸 **Error Code:** {response.status_code}\n"
                         f"Please try again later or contact IT support."
                )
        except requests.exceptions.Timeout:
            logger.error("API request timeout")
            dispatcher.utter_message(
                text="⏰ **Request Timeout**\n\n"
                     "The server took too long to respond. Please try again."
            )
        except requests.exceptions.ConnectionError:
            logger.error("API connection error")
            dispatcher.utter_message(
                text="🌐 **Connection Error**\n\n"
                     "Unable to connect to the leave management system. Please check your internet connection and try again."
            )
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            dispatcher.utter_message(
                text=f"❌ **Unexpected Error**\n\n"
                     f"An error occurred while submitting your application.\n"
                     f"**Error:** {str(e)}\n\n"
                     f"Please try again or contact IT support."
            )
        
        logger.info("🏁 ACTION_SUBMIT_LEAVE COMPLETED!")
        
        # Clear all slots after submission
        return [AllSlotsReset()]