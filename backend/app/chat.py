"""ChatKit server integration for the boilerplate backend."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Final, cast
from uuid import uuid4

from agents import Agent, RunContextWrapper, Runner, WebSearchTool, function_tool
from chatkit.agents import ClientToolCall, ThreadItemConverter, stream_agent_response
from chatkit.server import ChatKitServer, ThreadItemDoneEvent
from chatkit.types import ClientToolCallItem, HiddenContextItem, ThreadMetadata, UserMessageItem
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseInputContentParam,
    ResponseInputTextParam,
)
from openai.types.responses.response_input_image_param import ResponseInputImageParam

from .agent_context import TraccarAgentContext
from .analysis import analyze_data, store_result
from .attachments import REPORTS_DIR, TraccarAttachmentStore, attachment_to_input, public_base_url
from .constants import INSTRUCTIONS, MODEL
from .neon_store import NeonStore
from .page_traffic import ApiTraffic, wait_until_quiet
from .support import _user_message_text, ask_support_specialist, forward_to_real_agent
from .traccar import _get_session_id, _get_traccar_url, invoke

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Every OpenAI request and trace upload is logged at INFO by httpx; that was most of the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
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


SYNTAX_SNIPPET_RADIUS: Final[int] = 60


def _compact_node_syntax_error(stderr: str) -> str:
    """Reduce node's syntax-error output to the message and the code around the error.

    Node prints the whole offending source line (minified scripts make it thousands of
    characters) plus a stack trace. Both go to the log and back to the model, so keep
    only the error message and a short window around the caret.
    """
    lines = stderr.strip().splitlines()
    message = next((ln.strip() for ln in lines if re.match(r"\w*Error\b", ln.strip())), None)
    caret = next((i for i, ln in enumerate(lines) if ln.strip() and set(ln.strip()) == {"^"}), None)
    if message is None:
        return stderr.strip()[:300]
    if caret is None or caret == 0:
        # Node omits the caret for very long lines; give the message and, from the
        # "<file>:<line>" header, the line in the script (new Function adds 2 lines).
        header = re.search(r":(\d+)$", lines[0].strip())
        line = int(header.group(1)) - 2 if header else 0
        return f"{message} (script line {line})" if line > 0 else message
    source = lines[caret - 1]
    column = lines[caret].index("^")
    start = max(0, column - SYNTAX_SNIPPET_RADIUS)
    snippet = source[start : column + SYNTAX_SNIPPET_RADIUS].strip()
    return f"{message} near: ...{snippet}..."


def _validate_js_syntax(html: str) -> str | None:
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
            error = _compact_node_syntax_error(result.stderr)
            return f"JavaScript syntax error in script block {i + 1}: {error}"
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

    url = f"{public_base_url(cookie, traccar_url)}/chatkit/{filename}"
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
    """Inlines uploaded attachments (images, PDFs, text files) into the model input."""

    async def attachment_to_message_content(self, attachment) -> ResponseInputContentParam:
        return attachment_to_input(attachment)

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
                    detail="high",
                ),
                ResponseInputTextParam(
                    type="input_text",
                    text=SHOW_HTML_SCREENSHOT_PROMPT,
                ),
            ]
            return [EasyInputMessageParam(type="message", role="user", content=content)]
        return await super().client_tool_call_to_input(item)


class TraccarAssistantServer(ChatKitServer[dict[str, Any]]):
    def __init__(self) -> None:
        self.store: NeonStore = NeonStore()
        super().__init__(self.store, TraccarAttachmentStore())
        tools = [
            invoke_api,
            show_html,
            ask_support_specialist,
            forward_to_real_agent,
            analyze_data,
            get_map_template,
            get_openapi_yaml,
            WebSearchTool(),
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
        turn_start = time.monotonic()
        try:
            async for event in self._respond(thread, item, context):
                yield event
        finally:
            elapsed = time.monotonic() - turn_start
            logger.debug("respond turn: thread=%s took %.2fs", thread.id, elapsed)
            if elapsed > 20:
                logger.warning("Slow respond turn: thread=%s took %.2fs", thread.id, elapsed)

    async def _respond(
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

        logger.debug(
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
        run_start = time.monotonic()
        event_count = 0
        logger.debug("agent run starting: thread=%s", thread.id)
        try:
            async for event in stream_agent_response(agent_context, result):
                event_count += 1
                yield event
        finally:
            # finally (not a plain trailing log line) so this still fires if the
            # client disconnects / the request is cancelled mid-stream -- that
            # cancellation *is* what a Vercel proxy timeout looks like from here,
            # and a bare statement after the loop would silently never run.
            logger.debug(
                "agent run: thread=%s events=%d took %.2fs",
                thread.id,
                event_count,
                time.monotonic() - run_start,
            )

        response_identifier = getattr(result, "last_response_id", None)
        if response_identifier is not None:
            metadata["previous_response_id"] = response_identifier
            thread.metadata = metadata
            await self.store.save_thread(thread, context)

        return

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


MAX_RESPONSE_SIZE: Final[int] = 1_000_000
# Total invoke_api bytes returned raw within one turn, so many smaller calls can't add up past it.
MAX_TURN_RESULT_BYTES: Final[int] = 1_500_000
# Cap on the sample records included when a result is too large to return in full.
SAMPLE_MAX_BYTES: Final[int] = 4000
SAMPLE_MAX_ITEMS: Final[int] = 3


def _summarize_large_result(
    result: Any, size: int, stored: dict[str, str] | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Describe an oversized API result without returning the data itself.

    The model gets the shape (count, field names, a few sample records) so it can
    write correct client-side fetch code in show_html instead of retrying.
    """
    summary: dict[str, Any] = {
        "truncated": True,
        "reason": reason or f"Response too large ({size} bytes) to return in full.",
    }
    if isinstance(result, list):
        summary["total_items"] = len(result)
        dicts = [item for item in result[:50] if isinstance(item, dict)]
        summary["fields"] = sorted({key for item in dicts for key in item})
        sample = result[:SAMPLE_MAX_ITEMS]
        while sample and len(json.dumps(sample)) > SAMPLE_MAX_BYTES:
            sample = sample[:-1]
        summary["sample"] = sample
    elif isinstance(result, dict):
        summary["fields"] = {
            key: f"list of {len(value)}" if isinstance(value, list) else type(value).__name__
            for key, value in result.items()
        }
    next_step = (
        "Do not retry this exact call. If you are answering a question, narrow the query "
        "(date range, a single device, a limit) or use a summary/aggregate endpoint. If the "
        "user wants to see or browse the data, fetch it client-side with JavaScript fetch() "
        "on the same path under /api/ and render it with show_html."
    )
    if stored:
        summary["stored_data"] = stored
        next_step += (
            " To compute exact answers over the full data (totals, maximums, counts, "
            "averages), call analyze_data with stored_data's container_id and path."
        )
    summary["next_step"] = next_step
    return summary


@function_tool(description_override="invoke traccar api")
async def invoke_api(
    ctx: RunContextWrapper[TraccarAgentContext],
    method: str,
    path: str,
    body: str = "",
):
    result = invoke(
        method,
        path,
        body,
        ctx.context.request_context.get("request"),
    )
    response_size = len(json.dumps(result))
    logger.log(
        logging.INFO if response_size >= 100_000 else logging.DEBUG,
        "invoke_api response size: %d bytes",
        response_size,
    )
    over_turn_budget = ctx.context.tool_result_bytes + response_size > MAX_TURN_RESULT_BYTES
    if response_size > MAX_RESPONSE_SIZE or over_turn_budget:
        logger.warning(
            "Response too large: %d bytes (limit %d, turn total %d of %d)",
            response_size,
            MAX_RESPONSE_SIZE,
            ctx.context.tool_result_bytes,
            MAX_TURN_RESULT_BYTES,
        )
        reason = None
        if response_size <= MAX_RESPONSE_SIZE:
            reason = (
                f"Too much data already returned this turn ({response_size} more bytes would "
                "exceed the budget). Do not split a range into many requests."
            )
        stored = await store_result(result)
        return _summarize_large_result(result, response_size, stored, reason)
    ctx.context.tool_result_bytes += response_size
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
        logger.debug("TOOL: show_html")
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
            start = time.monotonic()
            try:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    try:
                        page = await browser.new_page(viewport={"width": 1280, "height": 720})
                        traffic = ApiTraffic()
                        page.on("request", traffic.started)
                        page.on("requestfinished", traffic.finished)
                        page.on("requestfailed", traffic.finished)
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

                        # A page can report ready (e.g. the map's first idle) while the
                        # data it fetches from /api/ (routes, positions) is still loading,
                        # which gives the model a half-drawn map. Wait for that data too.
                        data_loaded = await wait_until_quiet(traffic)
                        if not data_loaded:
                            logger.warning(
                                "Screenshot taken with /api/ requests pending: %s", html_url
                            )
                        await page.wait_for_timeout(700)  # let the map draw what just arrived

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
        logger.debug("show_html success")
        return {"result": "success", "url": html_url}
    except Exception:
        logger.exception("show_html failed")
        return {"error": "Internal error rendering HTML"}


@function_tool(description_override="Open API specification (yaml) for the Traccar server")
async def get_openapi_yaml() -> str:
    logger.debug("TOOL: get_openapi_yaml")
    return (Path(__file__).parent / "openapi.yaml").read_text()


@function_tool(
    description_override=(
        "A working Mapbox GL HTML page to use as the starting point when rendering a map. "
        "Fetch this before writing any map HTML, fill in the marked section with your data, "
        "then pass the result to show_html."
    )
)
async def get_map_template() -> str:
    logger.debug("TOOL: get_map_template")
    return (Path(__file__).parent / "map_template.html").read_text()
