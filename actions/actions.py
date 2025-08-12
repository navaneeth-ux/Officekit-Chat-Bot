from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher

class ActionApplyLeave(Action):
    def name(self) -> str:
        return "action_apply_leave"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: dict) -> list:
        
        # Placeholder API call simulation
        user_message = tracker.latest_message.get('text')
        # Here you'd process the text and make an actual API request

        dispatcher.utter_message(text=f"Leave request received: '{user_message}'")
        return []
