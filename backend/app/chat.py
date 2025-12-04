"""ChatKit server integration for the boilerplate backend."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Final
from uuid import uuid4

from agents import Agent, RunContextWrapper, Runner, function_tool
from chatkit.agents import (
    AgentContext,
    ClientToolCall,
    ThreadItemConverter,
    stream_agent_response,
)
from chatkit.server import ChatKitServer, ThreadItemDoneEvent
from chatkit.types import (
    Attachment,
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
from .traccar import get, put, post

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


def _save_html(html: str, thread_id: str) -> None:
    """Save HTML to a file and return the file URL."""
    # Ensure reports directory exists
    REPORTS_DIR.mkdir(exist_ok=True)

    # Generate unique filename with thread_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"html_{thread_id}_{timestamp}_{uuid4().hex[:8]}.html"
    file_path = REPORTS_DIR / filename

    # Write HTML to file
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    url = f"http://chat.frotaweb.com:8000/chatkit/{filename}"
    print("html url", url)

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
            get_device_events,
            get_device_stops,
            get_device_summary,
            get_device_trips,
            get_devices,
            get_drivers,
            get_positions,
            get_session,
            get_geofences,
            update_geofence,
            create_geofence,
            show_map,
            show_html,
            get_groups]
        self.assistant = Agent[TraccarAgentContext](
            model=MODEL,
            name="Traccar Assistant",
            instructions=INSTRUCTIONS,
            tools=tools
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

        target_item = item
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

    async def to_message_content(self, _input: Attachment) -> ResponseInputContentParam:
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


@function_tool(description_override="get devices")
async def get_devices(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/devices", ctx.context.request_context.get("request"))

@function_tool(description_override="get drivers")
async def get_drivers(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/drivers", ctx.context.request_context.get("request"))

@function_tool(description_override="get current user session")
async def get_session(ctx: RunContextWrapper[TraccarAgentContext]) -> dict[str, Any] | None:
    return get("api/session", ctx.context.request_context.get("request"))

@function_tool(description_override="get last known position for all devices")
async def get_positions(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/positions", ctx.context.request_context.get("request"))

@function_tool(description_override="get groups")
async def get_groups(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/groups", ctx.context.request_context.get("request"))

@function_tool(description_override="get device events for a given date range")
async def get_device_events(
        ctx: RunContextWrapper[TraccarAgentContext],
        device_id: int,
        from_date: datetime,
        to_date: datetime
) -> list[dict[str, Any]] | None:
    return get("api/reports/events", ctx.context.request_context.get("request"), device_id, from_date, to_date)

@function_tool(description_override="get device summary data (maximum speed, average speed, distance travelled, spent fuel and engine hours) for a given date range. Speeds are in knots.")
async def get_device_summary(
        ctx: RunContextWrapper[TraccarAgentContext],
        device_id: int,
        from_date: datetime,
        to_date: datetime
) -> list[dict[str, Any]] | None:
    return get("api/reports/summary", ctx.context.request_context.get("request"), device_id, from_date, to_date)


@function_tool(description_override="get device trips for a given date range. 'from_date' and 'to_date' should include timezone")
async def get_device_trips(
        ctx: RunContextWrapper[TraccarAgentContext],
        device_id: int,
        from_date: datetime,
        to_date: datetime
) -> list[dict[str, Any]] | None:
    return get("api/reports/trips", ctx.context.request_context.get("request"), device_id, from_date, to_date)

@function_tool(description_override="get device stops for a given date range")
async def get_device_stops(
        ctx: RunContextWrapper[TraccarAgentContext],
        device_id: int,
        from_date: datetime,
        to_date: datetime
) -> list[dict[str, Any]] | None:
    return get("api/reports/stops", ctx.context.request_context.get("request"), device_id, from_date, to_date)

@function_tool(description_override="get geofences")
async def get_geofences(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/geofences", ctx.context.request_context.get("request"))

@function_tool(description_override="update a geofence, area is a wkt string, coordinate order is lat,lon")
async def update_geofence(
        ctx: RunContextWrapper[TraccarAgentContext],
        geofence_id: int,
        area: str,
        name: str,
        description: str | None = None
) -> list[dict[str, Any]] | None:
    return put(f"api/geofences/{geofence_id}", ctx.context.request_context.get("request"), id=geofence_id, area=area, name=name, description=description)

@function_tool(description_override="create a geofence, area is a wkt string, coordinate order is lat,lon")
async def create_geofence(
        ctx: RunContextWrapper[TraccarAgentContext],
        area: str,
        name: str,
        description: str | None = None
) -> list[dict[str, Any]] | None:
    return post(f"api/geofences", ctx.context.request_context.get("request"), area=area, name=name, description=description)

@function_tool(description_override="Show a map with the provided Styled GeoJSON.\n\ngeojson argument should be a valid styled geojson string.")
async def show_map(ctx: RunContextWrapper[TraccarAgentContext], geojson: str) -> dict[str, str] | None:
    print("show_map")
    # Validate GeoJSON
    json.loads(geojson)
    ctx.context.client_tool_call = ClientToolCall(
        name="show_map",
        arguments={"geojson": geojson},
    )
    return {"result": "success"}

@function_tool(description_override="Display rendered html to the user")
async def show_html(ctx: RunContextWrapper[TraccarAgentContext], html: str) -> dict[str, str] | None:
    print("show_html")
    _save_html(html, ctx.context.thread.id)
    ctx.context.client_tool_call = ClientToolCall(
        name="show_html",
        arguments={"html": html},
    )
    return {"result": "success"}
