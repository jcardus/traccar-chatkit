"""FastAPI entrypoint wiring the ChatKit server and REST endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time

import httpx
from chatkit.server import StreamingResult
from chatkit.store import NotFoundError
from chatkit.types import ClientToolCallItem
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.responses import JSONResponse

from .attachments import MAX_ATTACHMENT_BYTES, OFFICE_EXTENSIONS, attachment_path
from .chat import (
    REPORTS_DIR,
    TraccarAssistantServer,
    create_chatkit_server,
    screenshot_tasks,
)
from .traccar import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    _get_cookie,
    _get_traccar_url,
    log_traccar_request,
    timeout_error_message,
    traccar_timeout,
)

logger = logging.getLogger(__name__)

# Cloudflare's proxy read timeout is 125s with no bytes from the origin.
# A comment frame is not a chat event; it only keeps that timer from firing
# while a tool call (analyze_data) produces no SSE data.
SSE_KEEPALIVE_INTERVAL = 20.0
SSE_KEEPALIVE = b": ping\n\n"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

app = FastAPI(title="ChatKit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_chatkit_server: TraccarAssistantServer | None = create_chatkit_server()


def _real_ip(request: Request) -> str:
    """Get the real client IP behind Cloudflare (o2o worker setup)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "unknown"
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    ip = _real_ip(request)
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "%s %s failed after %.2fs", request.method, request.url.path, time.monotonic() - start
        )
        raise
    # For streaming responses (e.g. /chatkit's SSE) this is time-to-first-byte,
    # not total duration -- the body streams after this returns. See the
    # per-stream timing logged in chatkit_endpoint for the true duration.
    elapsed = time.monotonic() - start
    logger.info(
        "%s %s %s -> %s in %.2fs (%s)",
        ip,
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
        request.headers.get("cf-ipcountry"),
    )
    if elapsed > 10:
        logger.warning(
            "Slow request: %s %s took %.2fs before response start",
            request.method,
            request.url.path,
            elapsed,
        )
    return response


def get_chatkit_server() -> TraccarAssistantServer:
    if _chatkit_server is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ChatKit dependencies are missing. Install the ChatKit Python "
                "package to enable the conversational endpoint."
            ),
        )
    return _chatkit_server


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_traccar(request: Request, path: str) -> Response:
    """Reverse-proxy /api/* requests to the Traccar server."""
    traccar_url = f"{_get_traccar_url(request).rstrip('/')}/api/{path}"
    if request.url.query:
        traccar_url += f"?{request.url.query}"

    cookie = _get_cookie(request)
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type

    body = await request.body()

    start = time.monotonic()
    status_code: int | str = "no response"
    response_chain = "no response"
    timed_out = False
    connect_failure = False
    resp = None
    connect, read = traccar_timeout(path, request.url.query)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect,
                read=read,
                write=READ_TIMEOUT,
                pool=CONNECT_TIMEOUT,
            )
        ) as client:
            resp = await client.request(
                request.method,
                traccar_url,
                headers=headers,
                content=body or None,
            )
        status_code = resp.status_code
        response_chain = " -> ".join(
            str(item.status_code) for item in [*resp.history, resp]
        )
    except httpx.ConnectTimeout:
        timed_out = True
        connect_failure = True
    except httpx.TimeoutException:
        timed_out = True
    finally:
        log_traccar_request(
            request.method,
            traccar_url,
            status_code,
            time.monotonic() - start,
            body=body or None,
            response_chain=response_chain,
        )

    if timed_out or resp is None:
        return JSONResponse(
            status_code=504,
            content={
                "error": timeout_error_message(
                    request.method, path, request.url.query, connect=connect_failure
                )
            },
        )

    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)


@app.get("/chatkit/{filename}")
async def get_file(filename: str) -> Response:
    """Serve report files generated by the chat assistant."""
    file_path = REPORTS_DIR / filename

    # Security check: ensure the file is within the reports directory
    if not file_path.is_relative_to(REPORTS_DIR):
        raise HTTPException(status_code=400, detail="Invalid file path")

    task = screenshot_tasks.get(filename)
    if task:
        wait_start = time.monotonic()
        await task
        logger.info(
            "get_file: %s waited %.2fs for pending screenshot task",
            filename,
            time.monotonic() - wait_start,
        )
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Report file not found")

    # Determine media type based on file extension
    if filename.endswith(".html"):
        # Serve HTML directly in browser
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/html")
    elif filename.endswith(".png"):
        return FileResponse(path=file_path, media_type="image/png")
    elif filename.endswith(OFFICE_EXTENSIONS):
        # Office uploads are never rendered inline; always download them.
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )
    elif not filename.endswith(".json"):
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return FileResponse(path=file_path, media_type=media_type)
    else:
        # Serve JSON files
        return FileResponse(path=file_path, media_type="application/json", filename=filename)


@app.put("/chatkit/attachments/{attachment_id}")
async def upload_attachment(
    attachment_id: str,
    request: Request,
    server: TraccarAssistantServer = Depends(get_chatkit_server),
) -> Response:
    """Second phase of a two-phase upload: the client PUTs the raw file bytes here."""
    try:
        attachment = await server.store.load_attachment(attachment_id, {"request": request})
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = attachment_path(attachment.id, attachment.mime_type)
    # Each attachment id is uploaded exactly once; never let a later PUT overwrite it.
    if file_path.exists():
        raise HTTPException(status_code=409, detail="Attachment already uploaded")

    data = await request.body()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="Attachment too large")

    file_path.parent.mkdir(exist_ok=True)
    file_path.write_bytes(data)
    logger.info("Uploaded attachment %s (%d bytes)", file_path.name, len(data))
    return Response(status_code=204)


_SSE_END = object()


async def _drain_task(pending: asyncio.Task | None) -> None:
    """Cancel the stream reader and wait until it has stopped.

    The reader is one task for the whole body, so a keepalive wait cannot
    cancel it. On disconnect it still has to be cancelled, or the agent turn
    keeps running.
    """
    if pending is None:
        return
    if not pending.done():
        pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("SSE reader did not stop cleanly")
        return


async def _read_sse(result: StreamingResult, queue: asyncio.Queue) -> None:
    """Copy one SSE body into a queue without changing tasks between chunks.

    ChatKit sets context vars for the agent request and resets them when the
    body ends. A fresh task per chunk resets those tokens in the wrong
    context and the stream ends with ValueError.
    """
    try:
        async for chunk in result:
            await queue.put(chunk)
        await queue.put(_SSE_END)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put(exc)


async def _timed_sse_stream(result: StreamingResult, label: str):
    """Re-yield an SSE stream, with comment frames during long silences.

    The `log_requests` middleware only measures time-to-first-byte. This
    accounts for the whole body, which is what Cloudflare's proxy read
    timeout (125s with no bytes from the origin) is measured against.

    Uses try/finally rather than try/except/else: when a client disconnects
    (which is what that Cloudflare timeout looks like from here), the server
    cancels this generator by throwing GeneratorExit/CancelledError into it.
    Neither is an Exception subclass, so `except Exception` does not catch it
    and a plain post-loop log line would never run.

    `asyncio.wait_for` is only applied to the queue read. Timing out that
    read does not cancel the agent turn.
    """
    start = time.monotonic()
    chunk_count = 0
    byte_count = 0
    keepalive_count = 0
    outcome = "cancelled"
    logger.debug("%s stream starting", label)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    reader = asyncio.create_task(_read_sse(result, queue))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                keepalive_count += 1
                yield SSE_KEEPALIVE
                continue
            if item is _SSE_END:
                outcome = "done"
                break
            if isinstance(item, Exception):
                outcome = "error"
                raise item
            chunk_count += 1
            byte_count += len(item)
            yield item
    except Exception:
        outcome = "error"
        raise
    finally:
        await _drain_task(reader)
        elapsed = time.monotonic() - start
        logger.info(
            "%s stream %s: %.2fs, %d chunks, %d bytes, %d keepalives",
            label,
            outcome,
            elapsed,
            chunk_count,
            byte_count,
            keepalive_count,
        )
        if outcome != "done" or elapsed > 20:
            logger.warning(
                "Slow/incomplete %s stream (%s): %.2fs, %d chunks, %d keepalives",
                label,
                outcome,
                elapsed,
                chunk_count,
                keepalive_count,
            )


async def _is_stale_tool_output(
    server: TraccarAssistantServer, payload: bytes, context: dict
) -> bool:
    """True if payload is a threads.add_client_tool_output with no pending tool call to answer.

    ChatKit only accepts a tool output while the thread's latest item is the pending
    ClientToolCallItem. If something was added after it (a newer user message, e.g. an
    html-error report sent while the screenshot was still being awaited), ChatKit raises
    ValueError and the stream dies with a traceback. The newer turn supersedes that tool
    call, so the output is dropped instead.
    """
    try:
        request = json.loads(payload)
        if request.get("type") != "threads.add_client_tool_output":
            return False
        thread_id = request["params"]["thread_id"]
        items = await server.store.load_thread_items(thread_id, None, 1, "desc", context)
    except Exception:
        return False
    return not any(
        isinstance(item, ClientToolCallItem) and item.status == "pending" for item in items.data
    )


async def _empty_stream():
    return
    yield


@app.post("/chatkit")
async def chatkit_endpoint(
    request: Request, server: TraccarAssistantServer = Depends(get_chatkit_server)
) -> Response:
    payload = await request.body()
    context = {"request": request}
    if await _is_stale_tool_output(server, payload, context):
        logger.warning("Dropping stale threads.add_client_tool_output: no pending tool call")
        return StreamingResponse(_empty_stream(), media_type="text/event-stream")
    result = await server.process(payload, context)
    if isinstance(result, StreamingResult):
        return StreamingResponse(
            _timed_sse_stream(result, "chatkit"),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    if hasattr(result, "json"):
        return Response(content=result.json, media_type="application/json")
    return JSONResponse(result)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
