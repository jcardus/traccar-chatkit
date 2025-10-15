import os
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode

def _get_traccar_url():
    return os.environ.get("TRACCAR_URL") or "http://gps.frotaweb.com"

def _format_date(value):
    """Convert datetime objects to UTC and format as ISO 8601 with Z suffix"""
    if isinstance(value, datetime):
        # Convert to UTC if timezone-aware, otherwise assume it's already UTC
        utc_value = value.astimezone(timezone.utc) if value.tzinfo else value
        return utc_value.strftime('%Y-%m-%dT%H:%M:%SZ')
    return value

def get(path, request, device_id=None, from_date=None, to_date=None):
    cookie = request.headers.get("cookie") if request and hasattr(request, "headers") else None
    headers = {"Cookie": cookie, "Accept": "application/json"}

    # Build query parameters
    params = {}
    if device_id is not None:
        params["deviceId"] = device_id
    if from_date is not None:
        params["from"] = _format_date(from_date)
    if to_date is not None:
        params["to"] = _format_date(to_date)

    # Build full URL with query parameters
    url = f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}"
    if params:
        url += f"?{urlencode(params)}"

    print("TRACCAR: "  + url)
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for bad status codes
    json = response.json()
    # Print only first few items if response is a list
    if isinstance(json, list):
        print(f"Response: {len(json)} items, first 3: {json[:3]}")
    else:
        print(json)
    return json

def post(self, path, json):
    return requests.post(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}", json=json)

def put(self, path, json):
    return requests.put(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}", json=json)

def delete(self, path):
    return requests.delete(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}")

