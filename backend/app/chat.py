"""ChatKit server integration for the boilerplate backend."""

from __future__ import annotations

import inspect
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Final, cast
from uuid import uuid4

import boto3
import requests
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
    ThreadItem,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)
from openai.types.responses import ResponseInputContentParam
from pydantic import ConfigDict, Field

from .constants import INSTRUCTIONS, MODEL
from .sqlite_store import SQLiteStore
from .traccar import invoke

# If you want to check what's going on under the hood, set this to DEBUG
logging.basicConfig(level=logging.INFO)

SUPPORTED_COLOR_SCHEMES: Final[frozenset[str]] = frozenset({"light", "dark"})
CLIENT_THEME_TOOL_NAME: Final[str] = "switch_theme"
REPORTS_DIR: Final[Path] = Path(__file__).parent.parent / "reports"


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

    return None


def _save_html(html: str, email: str) -> str:
    """Save HTML to a file and return the public URL."""
    REPORTS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"html_{timestamp}_{email}.html"
    file_path = REPORTS_DIR / filename

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    url = f"https://chat.frotaweb.com/chatkit/{filename}"
    print("PAGE: ", url)
    return url


def _screenshot_url(url: str) -> str:
    """Take a screenshot of a URL via microlink and return the image URL."""
    resp = requests.get(
        "https://api.microlink.io",
        params={"url": url, "screenshot": "true", "embed": "screenshot.url"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.url


def _is_tool_completion_item(item: Any) -> bool:
    return isinstance(item, ClientToolCallItem)


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


class TraccarAgentContext(AgentContext):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    store: Annotated[SQLiteStore, Field(exclude=True)]
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
        self.store: SQLiteStore = SQLiteStore()
        super().__init__(self.store)
        tools = [
            invoke_api,
            show_html,
            render_html,
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
    ) -> AsyncIterator[ThreadStreamEvent]:
        agent_context = TraccarAgentContext(
            thread=thread,
            store=self.store,
            request_context=context,
        )

        target_item: ThreadItem | None = item
        if target_item is None:
            target_item = await self._latest_thread_item(thread, context)

        if target_item is None or _is_tool_completion_item(target_item):
            return

        agent_input = await self._to_agent_input(thread, target_item)
        if agent_input is None:
            return

        metadata = dict(getattr(thread, "metadata", {}) or {})
        previous_response_id = metadata.get("previous_response_id")
        agent_context.previous_response_id = previous_response_id

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
        converter_cls = ThreadItemConverter
        if converter_cls is None or not callable(converter_cls):
            return None

        attempts: tuple[dict[str, Any], ...] = (
            {"to_message_content": self.to_message_content},
            {"message_content_converter": self.to_message_content},
            {},
        )

        for kwargs in attempts:
            try:
                return converter_cls(**kwargs)
            except TypeError:
                continue
        return None

    async def _latest_thread_item(
        self, thread: ThreadMetadata, context: dict[str, Any]
    ) -> ThreadItem | None:
        try:
            items = await self.store.load_thread_items(thread.id, None, 1, "desc", context)
        except Exception:  # pragma: no cover - defensive
            return None

        return items.data[0] if getattr(items, "data", None) else None

    async def _to_agent_input(
        self,
        thread: ThreadMetadata,
        item: ThreadItem,
    ) -> Any | None:
        if _is_tool_completion_item(item):
            return None

        converter = getattr(self, "_thread_item_converter", None)
        if converter is not None:
            for attr in (
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


MAX_RESPONSE_SIZE: Final[int] = 10485760  # 10MB


@function_tool(description_override="invoke traccar api")
async def invoke_api(
        ctx: RunContextWrapper[TraccarAgentContext],
        method: str,
        path: str,
        body: str,
):
    import json
    result = invoke(
        method,
        path,
        body,
        ctx.context.request_context.get("request"),
    )
    response_size = len(json.dumps(result))
    if response_size > MAX_RESPONSE_SIZE:
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
        print(f"Failed to get user from Traccar: {e}")
        return None

@function_tool(description_override="Display rendered html to the user")
async def show_html(
    ctx: RunContextWrapper[TraccarAgentContext], html: str
) -> dict[str, str]:
    print("TOOL: show_html")
    js_error = _validate_js_syntax(html)
    if js_error:
        print(f"JS validation failed: {js_error}")
        return {"error": js_error}
    email = _get_user_email_from_traccar(ctx.context.request_context)
    _save_html(html, email)
    ctx.context.client_tool_call = ClientToolCall(
        name="show_html",
        arguments={"html": html},
    )
    return {"result": "success"}

@function_tool(description_override="Render HTML in a headless browser and return a screenshot URL. Use this to verify that generated HTML looks correct.")
async def render_html(ctx: RunContextWrapper[TraccarAgentContext], html: str) -> dict[str, str]:
    print("TOOL: render_html")
    js_error = _validate_js_syntax(html)
    if js_error:
        return {"error": js_error}
    try:
        email = _get_user_email_from_traccar(ctx.context.request_context)
        html_url = _save_html(html, email)
        screenshot = _screenshot_url(html_url)
        return {"screenshot_url": screenshot}
    except Exception as e:
        return {"error": str(e)}

@function_tool(description_override="Forward the user question to a real agent.")
async def forward_to_real_agent(
    ctx: RunContextWrapper[TraccarAgentContext], question: str
) -> str:
    print("forward_to_real_agent")
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
    print("TOOL: get_openapi_yaml")
    return (Path(__file__).parent / "openapi.yaml").read_text()
