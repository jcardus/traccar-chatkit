from datetime import datetime, timezone

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
        "https://afconsultingsystems.com",
        "https://plataforma.ubisat.cl"
    ]
    if origin and any(origin.startswith(domain) for domain in fleetmap_origins):
        return "https://traccar-eu.joaquim.workers.dev"
    return "http://gps.frotaweb.com"
def _get_cookie(request):
    """Get a cookie from x-fleet-session (with JSESSIONID= prefix) or fall back to cookie header."""
    if not request or not hasattr(request, "headers"):
        return None
    fleet_session = request.headers.get("x-fleet-session")
    if fleet_session:
        return f"JSESSIONID={fleet_session}"
    return request.headers.get("cookie")


def _format_date(value):
    """Convert datetime objects to UTC and format as ISO 8601 with Z suffix"""
    if isinstance(value, datetime):
        # Convert to UTC if timezone-aware, otherwise assume it's already UTC
        utc_value = value.astimezone(timezone.utc) if value.tzinfo else value
        return utc_value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value

def invoke(method, path, body, request):
    """Generic API invocation with an arbitrary JSON body string."""
    import json as json_module

    cookie = _get_cookie(request)
    headers = {"Cookie": cookie, "Accept": "application/json"}
    if method.upper() in ("POST", "PUT"):
        headers["Content-Type"] = "application/json"

    url = f"{_get_traccar_url(request).rstrip('/')}/api/{path.lstrip('/')}"

    print(f"TRACCAR {method.upper()}: {url}")
    print(f"Body: {body}")

    parsed_body = json_module.loads(body) if body else None
    response = requests.request(method.upper(), url, headers=headers, json=parsed_body)
    response.raise_for_status()
    json_response = response.json()

    if isinstance(json_response, list):
        print(f"Response: {len(json_response)} items, first 3: {json_response[:3]}")
    else:
        print(json_response)

    return json_response
