import json
import httpx

async def apply_leave(OfficeContent: dict, Commonparam: dict):
    """
    Call the SaveLeaveApplication API to apply leave.
    """
    try:
        base_url = "http://10.25.25.124:82"

        url = (
            f"{base_url}/api/AjaxAPI/SaveLeaveApplication"
            f"?OfficeContent={json.dumps(OfficeContent)}"
            f"&Commonparam={json.dumps(Commonparam)}"
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    except Exception as e:
        return {"error": str(e)}


