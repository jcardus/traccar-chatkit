from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

def _get_traccar_url(request):
    origin = request.headers.get("origin") if request and hasattr(request, "headers") else None
    print(f"Request origin: {origin}")
    fleetmap_origins = [
        "https://moviflotte.com",
        "https://localizalia.net",
        "https://web.fleetrack.cl",
        "https://nogartel.fleetmap.io",
        "https://fleetmap.io",
        "https://plataforma.puntosat.cl",
        "https://afconsultingsystems.com"
    ]
    if origin and any(origin.startswith(domain) for domain in fleetmap_origins):
        return "https://traccar-eu.joaquim.workers.dev"
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

def _make_request_with_body(method, path, request, _id=None, name=None, description=None, area=None):
    """Common function for PUT and POST requests with JSON body."""
    cookie = request.headers.get("cookie") if request and hasattr(request, "headers") else None
    headers = {"Cookie": cookie, "Accept": "application/json", "Content-Type": "application/json"}

    # Build request body with provided parameters
    body = {}
    if _id is not None:
        body["id"] = _id
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if area is not None:
        body["area"] = area

    # Build full URL
    url = f"{_get_traccar_url(request).rstrip('/')}/{path.lstrip('/')}"

    print(f"TRACCAR {method.upper()}: {url}")
    print(f"Body: {body}")

    # Make request with the specified method
    response = requests.request(method, url, headers=headers, json=body)
    response.raise_for_status()  # Raise an exception for bad status codes
    json_response = response.json()

    # Print only first few items if response is a list
    if isinstance(json_response, list):
        print(f"Response: {len(json_response)} items, first 3: {json_response[:3]}")
    else:
        print(json_response)

    return json_response

def put(path, request, id=None, name=None, description=None, area=None):
    return _make_request_with_body("PUT", path, request, id, name, description, area)

def post(path, request, name=None, description=None, area=None):
    return _make_request_with_body("POST", path, request, None, name, description, area)
