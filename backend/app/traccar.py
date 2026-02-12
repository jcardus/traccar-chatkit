import logging

import requests

logger = logging.getLogger(__name__)

def _get_traccar_url(request):
    origin = request.headers.get("origin") if request and hasattr(request, "headers") else None
    logger.info("Request origin: %s", origin)
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
    if origin and "i8ttracker.com.br" in origin:
        return "https://traccar-eu.joaquim.workers.dev"
    return "http://gps.frotaweb.com"
def _get_session_from_host(request):
    """Extract session from subdomain, e.g. {session}.rastreon.net -> session."""
    host = request.headers.get("host", "")
    # Expect at least 3 parts: {session}.rastreon.net
    parts = host.split(".")
    if len(parts) >= 3:
        return parts[0]
    return None

def _get_session_id(request):
    """Extract the bare JSESSIONID value (without .node0 suffix) from the request."""
    cookie = _get_cookie(request)
    if not cookie:
        return None
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("JSESSIONID="):
            return part.split("=", 1)[1].split(".")[0]
    return None

def _get_cookie(request):
    """Get a cookie from x-fleet-session, cookie header, or hostname subdomain."""
    if not request or not hasattr(request, "headers"):
        return None
    fleet_session = request.headers.get("x-fleet-session")
    if fleet_session and fleet_session != "null":
        return f"JSESSIONID={fleet_session}"
    cookie = request.headers.get("cookie")
    if cookie:
        return cookie
    # Fall back to session embedded in hostname subdomain
    session = _get_session_from_host(request)
    if session:
        return f"JSESSIONID={session}"
    return None
def invoke(method, path, body, request):
    """Generic API invocation with an arbitrary JSON body string."""
    import json as json_module

    cookie = _get_cookie(request)
    headers = {"Cookie": cookie, "Accept": "application/json"}
    if method.upper() in ("POST", "PUT"):
        headers["Content-Type"] = "application/json"

    url = f"{_get_traccar_url(request).rstrip('/')}/api/{path.lstrip('/')}"

    logger.info("%s %s %s", method.upper(), url, body)

    parsed_body = json_module.loads(body) if body else None
    response = requests.request(method.upper(), url, headers=headers, json=parsed_body)
    response.raise_for_status()
    return response.json()
