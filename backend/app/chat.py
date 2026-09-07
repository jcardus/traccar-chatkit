"""ChatKit server integration for the boilerplate backend."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Final, cast
from uuid import uuid4

import boto3
from agents import Agent, RunContextWrapper, Runner, function_tool
from chatkit.agents import (
    AgentContext,
    ClientToolCall,
    ThreadItemConverter,
    stream_agent_response,
)
from chatkit.server import ChatKitServer, ThreadItemDoneEvent
from chatkit.types import (
    ClientToolCallItem,
    HiddenContextItem,
    ImageAttachment,
    ThreadMetadata,
    UserMessageItem,
)
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseInputContentParam,
    ResponseInputTextParam,
)
from openai.types.responses.response_input_image_param import ResponseInputImageParam
from pydantic import ConfigDict, Field

from .constants import INSTRUCTIONS, MODEL
from .neon_store import NeonStore
from .traccar import _get_session_id, _get_traccar_url, fleetmap_url, invoke

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SUPPORTED_COLOR_SCHEMES: Final[frozenset[str]] = frozenset({"light", "dark"})
CLIENT_THEME_TOOL_NAME: Final[str] = "switch_theme"
SHOW_HTML_TOOL_NAME: Final[str] = "show_html"
SHOW_HTML_SCREENSHOT_PROMPT: Final[str] = (
    "This is a screenshot of the HTML you just rendered for the user. Review it: "
    "if the layout is broken, data is missing, or something looks wrong, call "
    "show_html again with corrected HTML. Otherwise give the user a brief "
    "confirmation of what you rendered."
)
REPORTS_DIR: Final[Path] = Path(__file__).parent.parent / "reports"
# Pending screenshot tasks keyed by filename, so requests can await them
screenshot_tasks: dict[str, asyncio.Task] = {}

# Runs in the page before a screenshot: wait for webfonts, then for the DOM to
# be quiet for `settleMs` (JS that renders charts/maps after load), capped at
# `capMs`, then two paint frames. Used only when the page does not set
# `window.__SCREENSHOT_READY__` itself.
_RENDER_SETTLE_JS: Final[str] = """
() => new Promise((resolve) => {
    const settleMs = 400;
    const capMs = 8000;
    const start = Date.now();
    let last = Date.now();
    const obs = new MutationObserver(() => { last = Date.now(); });
    obs.observe(document.documentElement, {
        subtree: true, childList: true, attributes: true, characterData: true,
    });
    const finish = () => {
        obs.disconnect();
        requestAnimationFrame(() => requestAnimationFrame(resolve));
    };
    const tick = () => {
        if (Date.now() - last >= settleMs || Date.now() - start >= capMs) finish();
        else setTimeout(tick, 100);
    };
    const fonts = (document.fonts && document.fonts.ready)
        ? document.fonts.ready : Promise.resolve();
    fonts.then(tick, tick);
})
"""


def _normalize_color_scheme(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in SUPPORTED_COLOR_SCHEMES:
        return normalized
    if "dark" in normalized:
        return "dark"
    if "light" in normalized:
        return "light"
    raise ValueError("Theme must be either 'light' or 'dark'.")


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _validate_js_syntax(html: str) -> str | None:
    logger.info("_validate_js_syntax")
    """Extract and validate JavaScript syntax from HTML. Returns error message or None if valid."""
    script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
    scripts = script_pattern.findall(html)

    if not scripts:
        return None

    for i, script in enumerate(scripts):
        script = script.strip()
        if not script:
            continue
        # Use Node.js to check syntax (new Function parses but doesn't execute)
        result = subprocess.run(
            ["node", "-e", f"new Function({repr(script)})"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            error = result.stderr.strip()
            return f"JavaScript syntax error in script block {i + 1}: {error}"
    logger.info("syntax ok")
    return None


def _save_html_file(
    html: str,
    email: str | None,
    cookie: str | None = None,
    traccar_url: str = "http://gps.frotaweb.com",
) -> str:
    """Save HTML to a file and return the public URL (no DB write).

    When *session* is provided it is embedded as a subdomain so the server
    can recover the session from the hostname on later requests made by
    the rendered page, e.g.
        https://{session}.chat.frotaweb.com/chatkit/{filename}
    """
    REPORTS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_email = re.sub(r"[^a-zA-Z0-9._-]", "_", email or "unknown")
    filename = f"{timestamp}_{safe_email}.html"
    file_path = REPORTS_DIR / filename

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Insert session as a subdomain: https://host -> https://{session}.host
    from urllib.parse import urlparse, urlunparse

    if traccar_url == fleetmap_url:
        base_domain = "https://i8ttracker.com.br"
    else:
        base_domain = "https://rastreon.net"
    parsed = urlparse(base_domain)
    parsed = parsed._replace(netloc=f"{cookie}.{parsed.netloc}")
    base_url = urlunparse(parsed)
    url = f"{base_url}/chatkit/{filename}"
    logger.info("Saved HTML: %s", url)
    return url


def _is_tool_completion_item(item: Any) -> bool:
    return isinstance(item, ClientToolCallItem)


def _screenshot_url_from_output(output: Any) -> str | None:
    if isinstance(output, dict):
        url = output.get("screenshot_url")
        if isinstance(url, str) and url:
            return url
    return None


def _completed_show_html_screenshot_url(item: Any) -> str | None:
    """Screenshot URL if *item* is a show_html call the frontend answered with one."""
    if (
        isinstance(item, ClientToolCallItem)
        and item.name == SHOW_HTML_TOOL_NAME
        and item.status == "completed"
    ):
        return _screenshot_url_from_output(item.output)
    return None


def _thread_item_done(thread_id: str, item: Any) -> Any:
    if ThreadItemDoneEvent is None:
        raise RuntimeError("ThreadItemDoneEvent type is unavailable")

    attempts: tuple[dict[str, Any], ...] = (
        {"thread_id": thread_id, "item": item},
        {"threadId": thread_id, "item": item},
        {"item": item},
    )

    for kwargs in attempts:
        try:
            return ThreadItemDoneEvent(**kwargs)
        except TypeError:
            continue

    return ThreadItemDoneEvent(item=item)


class TraccarThreadItemConverter(ThreadItemConverter):
    """Converts image attachments to input_image content for the model."""

    async def attachment_to_message_content(self, attachment) -> ResponseInputContentParam:
        if isinstance(attachment, ImageAttachment):
            return ResponseInputImageParam(
                type="input_image",
                image_url=str(attachment.preview_url),
                detail="low",
            )
        raise NotImplementedError(f"Unsupported attachment type: {attachment.type}")

    async def client_tool_call_to_input(self, item: ClientToolCallItem):
        """Feed the show_html screenshot back to the model as an image.

        The frontend answers the pending ``show_html`` client tool call (via
        ``threads.add_client_tool_output``) with ``{"screenshot_url": ...}``.
        ChatKit then re-enters ``respond(thread, None)``; we splice the
        screenshot into the model input as a user-role image message so the
        model can visually verify what it rendered and iterate if needed.
        """
        if isinstance(item, ClientToolCallItem) and item.name == SHOW_HTML_TOOL_NAME:
            # show_html runs server-side as a real function tool, so its
            # function_call / output already live under previous_response_id.
            # Re-emitting them here (super()'s behaviour) would duplicate the
            # call_id. Only contribute the screenshot, when we have one.
            screenshot_url = _completed_show_html_screenshot_url(item)
            if not screenshot_url:
                return None
            content: list[ResponseInputContentParam] = [
                ResponseInputImageParam(
                    type="input_image",
                    image_url=screenshot_url,
                    detail="low",
                ),
                ResponseInputTextParam(
                    type="input_text",
                    text=SHOW_HTML_SCREENSHOT_PROMPT,
                ),
            ]
            return [EasyInputMessageParam(type="message", role="user", content=content)]
        return await super().client_tool_call_to_input(item)


class TraccarAgentContext(AgentContext):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    store: Annotated[NeonStore, Field(exclude=True)]
    request_context: dict[str, Any]


def _user_message_text(item: UserMessageItem) -> str:
    parts: list[str] = []
    for part in item.content:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


class TraccarAssistantServer(ChatKitServer[dict[str, Any]]):
    def __init__(self) -> None:
        self.store: NeonStore = NeonStore()
        super().__init__(self.store)
        tools = [
            invoke_api,
            show_html,
            get_openapi_yaml,
        ]
        self.assistant = Agent[TraccarAgentContext](
            model=MODEL, name="Traccar Assistant", instructions=INSTRUCTIONS, tools=cast(Any, tools)
        )
        self._thread_item_converter = self._init_thread_item_converter()

    async def respond(
        self,
        thread: ThreadMetadata,
        item: UserMessageItem | None,
        context: dict[str, Any],
    ) -> AsyncIterator[Any]:
        agent_context = TraccarAgentContext(
            thread=thread,
            store=self.store,
            request_context=context,
        )

        target_item = item
        if target_item is None:
            target_item = await self._latest_thread_item(thread, context)

        logger.info(
            "respond: item=%s target_item=%s type=%s",
            type(item).__name__ if item else None,
            type(target_item).__name__ if target_item else None,
            getattr(target_item, "type", None),
        )

        if target_item is None:
            return

        metadata = dict(getattr(thread, "metadata", {}) or {})
        previous_response_id = metadata.get("previous_response_id")
        agent_context.previous_response_id = previous_response_id

        # Client tool calls normally end the turn. The exception is a completed
        # show_html call carrying a screenshot URL: the frontend attached the
        # rendered screenshot and ChatKit re-entered respond() so the model can
        # review it.
        if (
            _is_tool_completion_item(target_item)
            and _completed_show_html_screenshot_url(target_item) is None
        ):
            return

        agent_input = await self._to_agent_input(thread, target_item)
        if agent_input is None:
            return

        result = Runner.run_streamed(
            self.assistant,
            agent_input,
            context=agent_context,
            previous_response_id=previous_response_id,
        )
        async for event in stream_agent_response(agent_context, result):
            yield event

        response_identifier = getattr(result, "last_response_id", None)
        if response_identifier is not None:
            metadata["previous_response_id"] = response_identifier
            thread.metadata = metadata
            await self.store.save_thread(thread, context)

        return

    async def to_message_content(self, _input) -> ResponseInputContentParam:
        raise RuntimeError("File attachments are not supported in this demo.")

    def _init_thread_item_converter(self) -> Any | None:
        return TraccarThreadItemConverter()

    async def _latest_thread_item(
        self, thread: ThreadMetadata, context: dict[str, Any]
    ) -> Any | None:
        try:
            items = await self.store.load_thread_items(thread.id, None, 1, "desc", context)
        except Exception:  # pragma: no cover - defensive
            return None

        return items.data[0] if getattr(items, "data", None) else None

    async def _to_agent_input(
        self,
        thread: ThreadMetadata,
        item: Any,
    ) -> Any | None:
        if _is_tool_completion_item(item) and _completed_show_html_screenshot_url(item) is None:
            return None

        converter = getattr(self, "_thread_item_converter", None)
        if converter is not None:
            for attr in (
                "to_agent_input",
                "to_input_item",
                "convert",
                "convert_item",
                "convert_thread_item",
            ):
                method = getattr(converter, attr, None)
                if method is None:
                    continue
                call_args: list[Any] = [item]
                call_kwargs: dict[str, Any] = {}
                try:
                    signature = inspect.signature(method)
                except (TypeError, ValueError):
                    signature = None

                if signature is not None:
                    params = [
                        parameter
                        for parameter in signature.parameters.values()
                        if parameter.kind
                        not in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        )
                    ]
                    if len(params) >= 2:
                        next_param = params[1]
                        if next_param.kind in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        ):
                            call_args.append(thread)
                        else:
                            call_kwargs[next_param.name] = thread

                result = method(*call_args, **call_kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

        if isinstance(item, UserMessageItem):
            return _user_message_text(item)

        return None

    async def _add_hidden_item(
        self,
        thread: ThreadMetadata,
        context: dict[str, Any],
        content: str,
    ) -> None:
        await self.store.add_thread_item(
            thread.id,
            HiddenContextItem(
                id=_gen_id("msg"),
                thread_id=thread.id,
                created_at=datetime.now(),
                content=content,
            ),
            context,
        )


def create_chatkit_server() -> TraccarAssistantServer | None:
    """Return a configured ChatKit server instance if dependencies are available."""
    return TraccarAssistantServer()


MAX_RESPONSE_SIZE: Final[int] = 548576


@function_tool(description_override="invoke traccar api")
async def invoke_api(
    ctx: RunContextWrapper[TraccarAgentContext],
    method: str,
    path: str,
    body: str,
):
    result = invoke(
        method,
        path,
        body,
        ctx.context.request_context.get("request"),
    )
    response_size = len(json.dumps(result))
    logger.info("invoke_api response size: %d bytes", response_size)
    if response_size > MAX_RESPONSE_SIZE:
        logger.warning("Response too large: %d bytes, limit: %d", response_size, MAX_RESPONSE_SIZE)
        return {
            "error": f"Response too large ({response_size} bytes). "
            "Fetch this data client-side in your HTML using JavaScript fetch() instead."
        }
    return result


def _get_user_email_from_traccar(context: dict[str, Any]) -> str | None:
    """Get user email from Traccar session."""
    try:
        request = context.get("request")
        if not request:
            return None
        session = invoke("get", "session", "", request)
        return session.get("email") if session else None
    except Exception as e:
        logger.warning("Failed to get user from Traccar: %s", e)
        return None


@function_tool(description_override="Display rendered html to the user")
async def show_html(ctx: RunContextWrapper[TraccarAgentContext], html: str) -> dict[str, str]:
    try:
        logger.info("TOOL: show_html")
        js_error = _validate_js_syntax(html)
        if js_error:
            logger.warning("JS validation failed: %s", js_error)
            return {"error": js_error}
        email = _get_user_email_from_traccar(ctx.context.request_context)
        session = _get_session_id(ctx.context.request_context.get("request"))
        traccar_url = _get_traccar_url(ctx.context.request_context.get("request"))
        html_url = _save_html_file(html, email, session, traccar_url)
        await ctx.context.store.save_html_report(email, ctx.context.thread.id, html_url)
        # Take a screenshot via local Playwright headless Chromium
        screenshot_filename = html_url.rsplit("/", 1)[-1].replace(".html", ".png")
        screenshot_path = REPORTS_DIR / screenshot_filename
        screenshot_url = html_url.replace(".html", ".png")

        async def _take_screenshot() -> None:
            import time

            start = time.monotonic()
            try:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    try:
                        page = await browser.new_page(viewport={"width": 1280, "height": 720})
                        await page.goto(html_url, wait_until="load", timeout=90000)

                        # Best-effort network settle. Cap it short: some pages
                        # keep a connection open and never reach "idle".
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except PlaywrightTimeoutError:
                            pass

                        # If the page signals its own readiness, trust that and
                        # skip the heuristics below.
                        signalled = False
                        try:
                            await page.wait_for_function(
                                "window.__SCREENSHOT_READY__ === true", timeout=10000
                            )
                            signalled = True
                        except PlaywrightTimeoutError:
                            pass

                        if not signalled:
                            # Wait for webfonts, then for the DOM to stop
                            # mutating (JS-driven charts/maps rendering after
                            # load), then a couple of paint frames.
                            try:
                                await page.evaluate(_RENDER_SETTLE_JS)
                            except Exception as exc:  # best effort only
                                logger.info("render-settle wait skipped: %s", exc)
                            await page.wait_for_timeout(500)

                        await page.screenshot(
                            path=str(screenshot_path), full_page=True, timeout=120000
                        )
                        elapsed = time.monotonic() - start
                        logger.info(
                            "Screenshot saved: %s (%.1fs%s)",
                            screenshot_path,
                            elapsed,
                            ", signalled" if signalled else "",
                        )
                    finally:
                        await browser.close()
            except Exception as e:
                elapsed = time.monotonic() - start
                logger.warning("Screenshot failed (%.1fs): %s", elapsed, e)
            finally:
                screenshot_tasks.pop(screenshot_filename, None)

        screenshot_tasks[screenshot_filename] = asyncio.create_task(_take_screenshot())

        # The frontend renders the HTML, warms `screenshot_url` (which blocks on
        # the background screenshot task server-side), then answers this client
        # tool call with {"screenshot_url": ...}. ChatKit re-enters respond() and
        # TraccarThreadItemConverter.client_tool_call_to_input feeds the image
        # back to the model.
        ctx.context.client_tool_call = ClientToolCall(
            name="show_html",
            arguments={
                "html": html,
                "html_url": html_url,
                "screenshot_url": screenshot_url,
            },
        )
        logger.info("show_html success")
        return {"result": "success", "url": html_url}
    except Exception:
        logger.exception("show_html failed")
        return {"error": "Internal error rendering HTML"}


@function_tool(description_override="Forward the user question to a real agent.")
async def forward_to_real_agent(ctx: RunContextWrapper[TraccarAgentContext], question: str) -> str:
    logger.info("forward_to_real_agent")
    """Send the user's question to support via email."""
    request = ctx.context.request_context.get("request")
    session = invoke("get", "session", "", request) if request else None
    user_email = session.get("email") if session else "unknown"
    thread_id = ctx.context.thread.id

    ses = boto3.client("ses", region_name="eu-west-1")
    ses.send_email(
        Source="support@fleetmap.io",
        Destination={"ToAddresses": ["support@fleetmap.io"]},
        Message={
            "Subject": {"Data": f"Support request from {user_email}"},
            "Body": {
                "Text": {
                    "Data": f"User: {user_email}\nThread: {thread_id}\n\nQuestion:\n{question}"
                }
            },
        },
    )
    return "Your question has been forwarded to our support team. They will get back to you soon."


@function_tool(description_override="Open API specification (yaml) for the Traccar server")
async def get_openapi_yaml() -> str:
    logger.info("TOOL: get_openapi_yaml")
    return (Path(__file__).parent / "openapi.yaml").read_text()
