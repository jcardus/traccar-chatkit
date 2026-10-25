import gzip
import http.client
import io
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests

logger = logging.getLogger(__name__)
fleetmap_url = "https://api.pinme.io"

# Enough to see which report or command was sent, without filling the journal
# with a positions or route payload.
_BODY_LOG_LIMIT = 300
_SECRET_KEYS = frozenset({"password", "token", "refreshtoken", "authorization"})
_SECRET_IN_TEXT = re.compile(
    r"(?i)((?:password|token|authorization|jsessionid)(?:=|%3D))([^&\s]+)"
)

# Traccar has hung indefinitely when a call has no timeout. Connect fails fast.
# The first byte stays on a short timer (longer when a report or a ranged
# position history has to start generating). A year of positions or route for
# one device on gps.frotaweb.com has sat silent well past 90s and only then
# failed with no headers, so that long wait is 5 minutes between bytes.
# Once headers have arrived, a pause between body bytes may last as long, and
# the whole call still ends at a 10 minute cap. invoke_api runs invoke() on a
# worker thread, so this wait does not freeze the event loop or the chat keepalive.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 25.0
LONG_READ_TIMEOUT = 300.0
BODY_SILENCE_TIMEOUT = 60.0
LONG_BODY_SILENCE_TIMEOUT = 300.0
TOTAL_TIMEOUT = 90.0
LONG_TOTAL_TIMEOUT = 600.0
_BODY_CHUNK = 65_536
# A parsed JSON document several times larger than this is what exhausted the
# 2 GB host (a year of positions is about 188 MB of text and well over 1 GB of
# objects). At or under this size, invoke() still returns parsed JSON. Above
# it, the bytes are handed to the analyst container and are not parsed here.
MAX_PARSED_BODY_BYTES = 8 * 1024 * 1024
_PEEK_BYTES = 256 * 1024
_PEEK_ITEMS = 3
_PEEK_SAMPLE_BYTES = 4000


def _get_traccar_url(request):
    origin = request.headers.get("origin") if request and hasattr(request, "headers") else None
    logger.debug("Request origin: %s", origin)
    fleetmap_origins = [
        "https://moviflotte.com",
        "https://localizalia.net",
        "https://web.fleetrack.cl",
        "https://nogartel.fleetmap.io",
        "https://fleetmap.io",
        "https://plataforma.puntosat.cl",
        "https://afconsultingsystems.com",
        "https://plataforma.ubisat.cl",
    ]
    if origin and any(origin.startswith(domain) for domain in fleetmap_origins):
        return fleetmap_url
    hostname = request.headers.get("host", "") if request and hasattr(request, "headers") else ""
    if "i8ttracker.com.br" in hostname:
        return "https://api.pinme.io"
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


def _clip(text: str, limit: int = _BODY_LOG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _redact(value):
    if isinstance(value, dict):
        return {
            key: "***" if str(key).lower() in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    return _SECRET_IN_TEXT.sub(r"\1***", text)


def _body_for_log(body) -> str:
    """Short, redacted request body for the journal. Empty when there is no body."""
    if body is None or body == "" or body == b"":
        return ""
    if isinstance(body, (bytes, bytearray)):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if not body or body[:1] > b"\x7f":
                return f" body=<{len(body)} bytes>"
            return " body=" + _clip(_redact_text(body.decode("utf-8", errors="replace")))
        text = json.dumps(_redact(parsed), default=str, ensure_ascii=False)
        return " body=" + _clip(text)
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return " body=" + _clip(_redact_text(body))
        text = json.dumps(_redact(parsed), default=str, ensure_ascii=False)
        return " body=" + _clip(text)
    text = json.dumps(_redact(body), default=str, ensure_ascii=False)
    return " body=" + _clip(text)


def _split_path_query(path: str, query: str = "") -> tuple[str, str]:
    """Separate a path from a query that may be embedded in it, appended, or both."""
    path = path or ""
    query = query or ""
    if "://" in path:
        split = urlsplit(path)
        path_part = split.path
        embedded = split.query
    else:
        path_part, _, embedded = path.partition("?")
    combined = "&".join(part.lstrip("?") for part in (embedded, query) if part)
    return path_part, combined


def _long_read(path: str, query: str = "") -> bool:
    path_part, query_string = _split_path_query(path, query)
    segments = [segment.lower() for segment in path_part.split("/") if segment]
    if "reports" in segments:
        return True
    if segments and segments[-1] == "positions":
        names = {key.lower() for key in parse_qs(query_string)}
        if "from" in names and "to" in names:
            return True
    return False


def read_timeout_seconds(path: str, query: str = "") -> float:
    """How long to wait for the first byte.

    Reports, and /positions calls that name a from/to range, are allowed to
    start computing. Everything else fails at the short timer.
    """
    if _long_read(path, query):
        return LONG_READ_TIMEOUT
    return READ_TIMEOUT


def body_limits(path: str, query: str = "") -> tuple[float, float]:
    """(silence between body bytes, total call seconds) after headers arrive."""
    if _long_read(path, query):
        return LONG_BODY_SILENCE_TIMEOUT, LONG_TOTAL_TIMEOUT
    return BODY_SILENCE_TIMEOUT, TOTAL_TIMEOUT


def traccar_timeout(path: str, query: str = "") -> tuple[float, float]:
    """(connect, first-byte) timeouts.

    requests uses the second value until headers arrive. The /api proxy uses
    it as the httpx read timeout for the whole response.
    """
    return (CONNECT_TIMEOUT, read_timeout_seconds(path, query))


def _call_target(path: str, query: str = "") -> str:
    target = path if not query else f"{path}?{query}"
    if len(target) > 300:
        return target[:300] + "…"
    return target


class UnparsedBody:
    """Response bytes left unparsed so a large document is not built in memory.

    The payload is the file the analyst container should read. `compressed`
    means the payload is already gzip (the upstream encoding), so it can be
    uploaded without being inflated on this host.
    """

    def __init__(
        self,
        payload: bytes,
        *,
        compressed: bool,
        uncompressed_size: int | None,
        sample: list | None = None,
    ):
        self.payload = payload
        self.compressed = compressed
        self.uncompressed_size = uncompressed_size
        self.sample = sample or []


class IncompleteResponse(Exception):
    """Headers arrived and the body stopped before it was finished."""

    def __init__(self, status_code: int, received: int):
        self.status_code = status_code
        self.received = received
        super().__init__(f"HTTP {status_code} incomplete after {received} bytes")


def timeout_error_message(
    method: str, path: str, query: str = "", *, connect: bool = False
) -> str:
    """What the model, or a proxied client, should do when no byte arrives.

    `connect` is the 10s connect timer. Otherwise the message names the
    first-byte budget for this path.
    """
    seconds = CONNECT_TIMEOUT if connect else read_timeout_seconds(path, query)
    return (
        f"The server did not answer {method.upper()} {_call_target(path, query)} "
        f"within {seconds:.0f}s. "
        "Do not retry this same request."
    )


def incomplete_body_message(
    method: str, path: str, status_code: int, received: int, query: str = ""
) -> str:
    """What the model should do when the server started answering and then stopped."""
    return (
        f"The server answered {method.upper()} {_call_target(path, query)} "
        f"with HTTP {status_code} but the body was incomplete ({received} bytes received). "
        "Do not treat a partial result as the total. "
        "Do not retry this same request."
    )


def _set_read_timeout(response, seconds: float) -> None:
    """Raise the socket's read timeout after headers, so a pause can be longer."""
    raw = getattr(response, "raw", None)
    conn = getattr(raw, "_connection", None)
    sock = getattr(conn, "sock", None) if conn is not None else None
    if sock is None:
        fp = getattr(raw, "_fp", None)
        buffered = getattr(fp, "fp", None)
        sock = getattr(buffered, "raw", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(seconds)


def _content_encoding(response) -> str:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter("Content-Encoding")
    if not isinstance(value, str):
        value = getter("content-encoding")
    return value.lower() if isinstance(value, str) else ""


def _text_encoding(response) -> str:
    encoding = getattr(response, "encoding", None)
    if isinstance(encoding, str) and encoding:
        return encoding
    return "utf-8"


def _can_read_undecoded(response) -> bool:
    """True when the socket stream can return the wire bytes, still gzipped."""
    raw = getattr(response, "raw", None)
    if raw is None or not isinstance(getattr(raw, "decode_content", None), bool):
        return False
    return callable(getattr(raw, "read", None))


def _iter_undecoded(raw):
    while True:
        chunk = raw.read(_BODY_CHUNK, decode_content=False)
        if not chunk:
            return
        yield chunk


def _gzip_isize(payload: bytes) -> int | None:
    """Uncompressed size modulo 4 GiB, from a single gzip member's trailer."""
    if len(payload) < 8 or payload[:2] != b"\x1f\x8b":
        return None
    return int.from_bytes(payload[-4:], "little")


def _gunzip_if_small(payload: bytes, cap: int) -> bytes | None:
    """Inflate `payload` when the document fits in `cap`. None when it does not.

    The read itself stops at `cap`, so a year-long file is not inflated here
    even if the gzip trailer understates the size.
    """
    size = _gzip_isize(payload)
    if size is not None and size > cap:
        return None
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        return None
    return data


def _peek_sample(payload: bytes, *, compressed: bool) -> list:
    """The first few JSON values, so the model can see field names.

    Only a short prefix is inflated. A failure leaves the handoff with no sample.
    """
    try:
        if compressed:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
                blob = handle.read(_PEEK_BYTES)
        else:
            blob = payload[:_PEEK_BYTES]
        text = blob.decode("utf-8")
    except (OSError, EOFError, UnicodeError, gzip.BadGzipFile):
        return []
    items = _leading_values(text)
    while items and len(json.dumps(items)) > _PEEK_SAMPLE_BYTES:
        items = items[:-1]
    return items


def _leading_values(text: str) -> list:
    stripped = text.lstrip()
    if not stripped:
        return []
    decoder = json.JSONDecoder()
    if not stripped.startswith("["):
        try:
            value, _end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            return []
        return [value] if isinstance(value, dict) else []
    index = 1
    items: list = []
    length = len(stripped)
    while len(items) < _PEEK_ITEMS and index < length:
        while index < length and stripped[index] in " \t\r\n,":
            index += 1
        if index >= length or stripped[index] == "]":
            break
        try:
            value, index = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            break
        items.append(value)
    return items


def _parse_or_hand_off(payload: bytes, response):
    """Parse a modest JSON body. Hand a large one off as bytes."""
    encoding = _text_encoding(response)
    if len(payload) >= 2 and payload[:2] == b"\x1f\x8b":
        inflated = _gunzip_if_small(payload, MAX_PARSED_BODY_BYTES)
        if inflated is None:
            return UnparsedBody(
                payload,
                compressed=True,
                uncompressed_size=_gzip_isize(payload),
                sample=_peek_sample(payload, compressed=True),
            )
        return json.loads(inflated.decode(encoding))
    if len(payload) > MAX_PARSED_BODY_BYTES:
        return UnparsedBody(
            payload,
            compressed=False,
            uncompressed_size=len(payload),
            sample=_peek_sample(payload, compressed=False),
        )
    return json.loads(payload.decode(encoding))


def _read_full_body(response, deadline: float, silence: float, *, undecoded: bool = False) -> bytes:
    """Read until the body ends. A stall or the total cap raises IncompleteResponse.

    `undecoded` keeps a gzip body compressed. Decoding it here is what turns a
    16 MB positions file into the 188 MB document that then gets parsed.
    """
    chunks: list[bytes] = []
    received = 0
    if undecoded and _can_read_undecoded(response):
        stream = _iter_undecoded(response.raw)
    else:
        stream = response.iter_content(chunk_size=_BODY_CHUNK)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IncompleteResponse(response.status_code, received)
        _set_read_timeout(response, min(silence, remaining))
        try:
            chunk = next(stream)
        except StopIteration:
            break
        except (requests.RequestException, http.client.IncompleteRead) as exc:
            raise IncompleteResponse(response.status_code, received) from exc
        if chunk:
            chunks.append(chunk)
            received += len(chunk)
    return b"".join(chunks)


def log_traccar_request(
    method, url, status_code, elapsed, body=None, response_chain=None, *, failed=False
):
    """Log one Traccar call at INFO, or WARNING when it is slow or incomplete.

    The service runs at INFO, so a DEBUG line never reaches the journal.
    Cookies and passwords stay out of the line.
    """
    level = logging.WARNING if failed or elapsed > 5 else logging.INFO
    chain = f" (response chain: {response_chain})" if response_chain else ""
    logger.log(
        level,
        "%s %s%s -> %s%s took %.2fs",
        method,
        _clip(str(url), 1000),
        _body_for_log(body),
        status_code,
        chain,
        elapsed,
    )


def invoke(method, path, body, request):
    """Generic API invocation with an arbitrary JSON body string."""
    method = method.upper()
    cookie = _get_cookie(request)
    headers = {"Cookie": cookie, "Accept": "application/json"}
    if method in ("POST", "PUT"):
        headers["Content-Type"] = "application/json"

    url = f"{_get_traccar_url(request).rstrip('/')}/api/{path.lstrip('/')}"
    parsed_body = json.loads(body) if body and method != "GET" else None

    start = time.monotonic()
    status_code: int | str = "no response"
    response_chain = "no response"
    response = None
    incomplete: IncompleteResponse | None = None
    try:
        # stream=True returns as soon as the headers arrive. The first-byte
        # timer covers that wait. The body is read below on its own timers.
        response = requests.request(
            method,
            url,
            headers=headers,
            json=parsed_body,
            timeout=traccar_timeout(path),
            stream=True,
        )
        status_code = response.status_code
        response_chain = " -> ".join(
            str(item.status_code) for item in [*response.history, response]
        )
        response.raise_for_status()
        silence, total = body_limits(path)
        # A gzip body stays compressed. The large positions file is handed to
        # the analyst as that gzip; only a modest document is parsed here.
        undecoded = "gzip" in _content_encoding(response)
        try:
            raw = _read_full_body(
                response, start + total, silence, undecoded=undecoded
            )
        except IncompleteResponse as exc:
            incomplete = exc
            raise
        parsed = _parse_or_hand_off(raw, response)
        if isinstance(parsed, UnparsedBody):
            logger.info(
                "Handing off %s as %d %s bytes without parsing (uncompressed %s)",
                path,
                len(parsed.payload),
                "gzip" if parsed.compressed else "raw",
                parsed.uncompressed_size,
            )
        return parsed
    finally:
        if incomplete is not None:
            status_code = (
                f"{incomplete.status_code} incomplete, {incomplete.received} bytes"
            )
        if response is not None:
            response.close()
        log_traccar_request(
            method,
            url,
            status_code,
            time.monotonic() - start,
            body=parsed_body,
            response_chain=response_chain,
            failed=incomplete is not None,
        )
