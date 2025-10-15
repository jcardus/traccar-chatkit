import requests
from datetime import datetime, timezone
from urllib.parse import urlencode

def _get_traccar_url(request):
    origin = request.headers.get("origin") if request and hasattr(request, "headers") else None
    print(f"Request origin: {origin}")
    fleetmap_origins = [
        "https://moviflotte.com",
        "https://localizalia.net",
        "https://web.fleetrack.cl",
        "https://nogartel.fleetmap.io"
    ]
    if origin and any(origin.startswith(domain) for domain in fleetmap_origins):
        return "http://gps.fleetmap.pt"
    return "http://gps.frotaweb.com"

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
    url = f"{_get_traccar_url(request).rstrip('/')}/{path.lstrip('/')}"
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


def put(path, request, id=None, name=None, description=None, area=None):
    cookie = request.headers.get("cookie") if request and hasattr(request, "headers") else None
    headers = {"Cookie": cookie, "Accept": "application/json", "Content-Type": "application/json"}
    # Build request body with provided parameters
    body = get_body(area, description, name, id)
    # Build full URL
    url = f"{_get_traccar_url(request).rstrip('/')}/{path.lstrip('/')}"
    print("TRACCAR PUT: " + url)
    print(f"Body: {body}")
    response = requests.put(url, headers=headers, json=body)
    response.raise_for_status()  # Raise an exception for bad status codes
    json = response.json()
    # Print only first few items if response is a list
    if isinstance(json, list):
        print(f"Response: {len(json)} items, first 3: {json[:3]}")
    else:
        print(json)
    return json

def post(path, request, name=None, description=None, area=None):
    cookie = request.headers.get("cookie") if request and hasattr(request, "headers") else None
    headers = {"Cookie": cookie, "Accept": "application/json", "Content-Type": "application/json"}
    # Build request body with provided parameters
    body = get_body(area, description, name)
    # Build full URL
    url = f"{_get_traccar_url(request).rstrip('/')}/{path.lstrip('/')}"
    print("TRACCAR POST: " + url)
    print(f"Body: {body}")
    response = requests.post(url, headers=headers, json=body)
    response.raise_for_status()  # Raise an exception for bad status codes
    json = response.json()
    # Print only first few items if response is a list
    if isinstance(json, list):
        print(f"Response: {len(json)} items, first 3: {json[:3]}")
    else:
        print(json)
    return json


def get_body(area, description, name, _id=0) -> dict[str, int]:
    body = {"id": _id}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if area is not None:
        body["area"] = area
    return body