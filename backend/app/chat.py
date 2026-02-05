"""ChatKit server integration for the boilerplate backend."""

from __future__ import annotations

import inspect
import logging
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
    ThreadItem,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)
from openai.types.responses import ResponseInputContentParam
from pydantic import ConfigDict, Field

from .constants import INSTRUCTIONS, MODEL
from .sqlite_store import SQLiteStore
from .traccar import get, post, put

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
            show_html,
            get_openapi_yaml,
            get_groups,
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
    to_date: datetime,
) -> list[dict[str, Any]] | None:
    return get(
        "api/reports/events",
        ctx.context.request_context.get("request"),
        device_id,
        from_date,
        to_date,
    )


@function_tool(
    description_override="get device summary data (maximum speed, average speed, distance travelled, spent fuel and engine hours) for a given date range. Speeds are in knots."
)
async def get_device_summary(
    ctx: RunContextWrapper[TraccarAgentContext],
    device_id: int,
    from_date: datetime,
    to_date: datetime,
) -> list[dict[str, Any]] | None:
    return get(
        "api/reports/summary",
        ctx.context.request_context.get("request"),
        device_id,
        from_date,
        to_date,
    )


@function_tool(
    description_override="get device trips for a given date range. 'from_date' and 'to_date' should include timezone"
)
async def get_device_trips(
    ctx: RunContextWrapper[TraccarAgentContext],
    device_id: int,
    from_date: datetime,
    to_date: datetime,
) -> list[dict[str, Any]] | None:
    return get(
        "api/reports/trips",
        ctx.context.request_context.get("request"),
        device_id,
        from_date,
        to_date,
    )


@function_tool(description_override="get device stops for a given date range")
async def get_device_stops(
    ctx: RunContextWrapper[TraccarAgentContext],
    device_id: int,
    from_date: datetime,
    to_date: datetime,
) -> list[dict[str, Any]] | None:
    return get(
        "api/reports/stops",
        ctx.context.request_context.get("request"),
        device_id,
        from_date,
        to_date,
    )


@function_tool(description_override="get geofences")
async def get_geofences(ctx: RunContextWrapper[TraccarAgentContext]) -> list[dict[str, Any]] | None:
    return get("api/geofences", ctx.context.request_context.get("request"))


@function_tool(
    description_override="update a geofence, area is a wkt string, coordinate order is lat,lon"
)
async def update_geofence(
    ctx: RunContextWrapper[TraccarAgentContext],
    geofence_id: int,
    area: str,
    name: str,
    description: str | None = None,
) -> list[dict[str, Any]] | None:
    return put(
        f"api/geofences/{geofence_id}",
        ctx.context.request_context.get("request"),
        id=geofence_id,
        area=area,
        name=name,
        description=description,
    )


@function_tool(
    description_override="create a geofence, area is a wkt string, coordinate order is lat,lon"
)
async def create_geofence(
    ctx: RunContextWrapper[TraccarAgentContext],
    area: str,
    name: str,
    description: str | None = None,
) -> list[dict[str, Any]] | None:
    return post(
        "api/geofences",
        ctx.context.request_context.get("request"),
        area=area,
        name=name,
        description=description,
    )


@function_tool(description_override="Display rendered html to the user")
async def show_html(
    ctx: RunContextWrapper[TraccarAgentContext], html: str
) -> dict[str, str] | None:
    print("show_html")
    _save_html(html, ctx.context.thread.id)
    ctx.context.client_tool_call = ClientToolCall(
        name="show_html",
        arguments={"html": html},
    )
    return {"result": "success"}

@function_tool(description_override="Forward the user question to a real agent.")
async def forward_to_real_agent(
    ctx: RunContextWrapper[TraccarAgentContext], question: str
) -> str:
    print("forward_to_real_agent")
    """Send the user's question to support via email."""
    request = ctx.context.request_context.get("request")
    session = get("api/session", request) if request else None
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
    print("get_openapi_yaml")
    return """
openapi: 3.1.0
info:
  title: Traccar
  description: Traccar GPS tracking server API documentation.
tags:
  - name: Server
    description: Server information
  - name: Session
    description: User session management
  - name: Devices
    description: Device management
  - name: Groups
    description: Group management
  - name: Users
    description: User management
  - name: Permissions
    description: User permissions and other object linking
  - name: Positions
    description: Retrieving raw location information
  - name: Events
    description: Retrieving event information
  - name: Reports
    description: Reports generation
  - name: Notifications
    description: User notifications management
  - name: Geofences
    description: Geofence management
  - name: Commands
    description: Sending commands to devices and stored command management
  - name: Attributes
    description: Computed attributes management
  - name: Drivers
    description: Drivers management
  - name: Maintenance
    description: Maintenance management
  - name: Calendars
    description: Calendar management
  - name: Statistics
    description: Retrieving server statistics
paths:
  /commands:
    get:
      summary: Fetch a list of Saved Commands
      tags:
        - Commands
      description: Without params, it returns a list of Saved Commands the user has access to
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: Standard users can use this only with _deviceId_s, they have access to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Command'
    post:
      summary: Create a Saved Command
      tags:
        - Commands
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Command'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Command'
      x-codegen-request-body-name: body
  /commands/{id}:
    put:
      summary: Update a Saved Command
      tags:
        - Commands
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Command'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Command'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Saved Command
      tags:
        - Commands
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /commands/send:
    get:
      summary: Fetch a list of Saved Commands supported by Device at the moment
      description: >-
        Return a list of saved commands linked to Device and its groups,
        filtered by current Device protocol support
      tags:
        - Commands
      parameters:
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Command'
        '400':
          description: Could happen when the user doesn't have permission for the device
          content: {}
    post:
      summary: Dispatch commands to device
      description: Dispatch a new command or Saved Command if _body.id_ set
      tags:
        - Commands
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Command'
        required: true
      responses:
        '200':
          description: Command sent
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Command'
        '202':
          description: Command queued
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Command'
        '400':
          description: >-
            Could happen when the user doesn't have permission or an incorrect
            command _type_ for the device
          content: {}
      x-codegen-request-body-name: body
  /commands/types:
    get:
      summary: >-
        Fetch a list of available Commands for the Device or all possible
        Commands if Device ommited
      tags:
        - Commands
      parameters:
        - name: deviceId
          in: query
          description: >-
            Internal device identifier. Only works if device has already
            reported some locations
          schema:
            type: integer
        - name: protocol
          in: query
          description: Protocol name. Can be used instead of device id
          schema:
            type: string
        - name: textChannel
          in: query
          description: >-
            When `true` return SMS commands. If not specified or `false` return
            data commands
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/CommandType'
        '400':
          description: >-
            Could happen when trying to fetch from a device the user does not
            have permission
          content: {}
  /devices:
    get:
      summary: Fetch a list of Devices
      description: Without any params, returns a list of the user's devices
      tags:
        - Devices
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: id
          in: query
          description: >-
            To fetch one or more devices. Multiple params can be passed like
            `id=31&id=42`
          schema:
            type: integer
        - name: uniqueId
          in: query
          description: >-
            To fetch one or more devices. Multiple params can be passed like
            `uniqueId=333331&uniqieId=44442`
          schema:
            type: string
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Device'
        '400':
          description: No permission
          content: {}
    post:
      summary: Create a Device
      tags:
        - Devices
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Device'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Device'
      x-codegen-request-body-name: body
  /devices/{id}:
    put:
      summary: Update a Device
      tags:
        - Devices
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Device'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Device'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Device
      tags:
        - Devices
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /devices/{id}/accumulators:
    put:
      summary: Update total distance and hours of the Device
      tags:
        - Devices
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/DeviceAccumulators'
        required: true
      responses:
        '204':
          description: No Content
          content: {}
      x-codegen-request-body-name: body
  /groups:
    get:
      summary: Fetch a list of Groups
      description: Without any params, returns a list of the Groups the user belongs to
      tags:
        - Groups
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Group'
    post:
      summary: Create a Group
      tags:
        - Groups
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Group'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Group'
        '400':
          description: No permission
          content: {}
      x-codegen-request-body-name: body
  /groups/{id}:
    put:
      summary: Update a Group
      tags:
        - Groups
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Group'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Group'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Group
      tags:
        - Groups
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /permissions:
    post:
      summary: Link an Object to another Object
      tags:
        - Permissions
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Permission'
        required: true
      responses:
        '204':
          description: No Content
          content: {}
        '400':
          description: No permission
          content: {}
      x-codegen-request-body-name: body
    delete:
      summary: Unlink an Object from another Object
      tags:
        - Permissions
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Permission'
        required: true
      responses:
        '204':
          description: No Content
          content: {}
      x-codegen-request-body-name: body
  /positions:
    get:
      summary: Fetches a list of Positions
      description: >-
        We strongly recommend using [Traccar WebSocket
        API](https://www.traccar.org/traccar-api/) instead of periodically
        polling positions endpoint. Without any params, it returns a list of
        last known positions for all the user's Devices. _from_ and _to_ fields
        are not required with _id_.
      tags:
        - Positions
      parameters:
        - name: deviceId
          in: query
          description: >-
            _deviceId_ is optional, but requires the _from_ and _to_ parameters
            when used
          schema:
            type: integer
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          schema:
            type: string
            format: date-time
        - name: id
          in: query
          description: >-
            To fetch one or more positions. Multiple params can be passed like
            `id=31&id=42`
          schema:
            type: integer
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
            text/csv:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
            application/gpx+xml:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
    delete:
      summary: Deletes all the Positions of a device in the time span specified
      description: ''
      tags:
        - Positions
      parameters:
        - name: deviceId
          in: query
          description: ''
          schema:
            type: integer
          required: true
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          schema:
            type: string
            format: date-time
          required: true
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          schema:
            type: string
            format: date-time
          required: true
      responses:
        '204':
          description: No Content
          content: {}
        '400':
          description: Bad Request
          content: {}
  /positions/{id}:
    delete:
      summary: Delete a Position
      tags:
        - Positions
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
        '404':
          description: Not Found
          content: {}
  /server:
    get:
      summary: Fetch Server information
      tags:
        - Server
      security: []
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Server'
    put:
      summary: Update Server information
      tags:
        - Server
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Server'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Server'
      x-codegen-request-body-name: body
  /session:
    get:
      summary: Fetch Session information
      tags:
        - Session
      security: []
      parameters:
        - name: token
          in: query
          schema:
            type: string
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '404':
          description: Not Found
          content: {}
    post:
      summary: Create a new Session
      tags:
        - Session
      security: []
      requestBody:
        content:
          application/x-www-form-urlencoded:
            schema:
              required:
                - email
                - password
              properties:
                email:
                  type: string
                password:
                  type: string
                  format: password
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '401':
          description: Unauthorized
          content: {}
    delete:
      summary: Close the Session
      tags:
        - Session
      responses:
        '204':
          description: No Content
          content: {}
  /session/token:
    post:
      summary: Generate Session Token
      tags:
        - Session
      requestBody:
        content:
          application/x-www-form-urlencoded:
            schema:
              properties:
                expiration:
                  type: string
                  format: date-time
        required: false
      responses:
        '200':
          description: Token string
          content:
            text/plain:
              schema:
                type: string
  /session/token/revoke:
    post:
      summary: Revoke Session Token
      tags:
        - Session
      requestBody:
        content:
          application/x-www-form-urlencoded:
            schema:
              required:
                - token
              properties:
                token:
                  type: string
        required: true
      responses:
        '204':
          description: No Content
          content: {}
        '400':
          description: Bad Request
          content: {}
  /session/openid/auth:
    get:
      summary: Fetch Session information
      tags:
        - Session
      responses:
        '303':
          description: Redirect to OpenID Connect identity provider
          content: {}
  /session/openid/callback:
    get:
      summary: OpenID Callback
      tags:
        - Session
      responses:
        '303':
          description: Successful authentication, redirect to homepage
          content: {}
  /users:
    get:
      summary: Fetch a list of Users
      tags:
        - Users
      parameters:
        - name: userId
          in: query
          description: Can only be used by admin or manager users
          schema:
            type: string
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/User'
        '400':
          description: No Permission
          content: {}
    post:
      summary: Create a User
      tags:
        - Users
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/User'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
      x-codegen-request-body-name: body
  /users/{id}:
    put:
      summary: Update a User
      tags:
        - Users
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/User'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a User
      tags:
        - Users
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /notifications:
    get:
      summary: Fetch a list of Notifications
      description: >-
        Without params, it returns a list of Notifications the user has access
        to
      tags:
        - Notifications
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Notification'
    post:
      summary: Create a Notification
      tags:
        - Notifications
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Notification'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Notification'
      x-codegen-request-body-name: body
  /notifications/{id}:
    put:
      summary: Update a Notification
      tags:
        - Notifications
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Notification'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Notification'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Notification
      tags:
        - Notifications
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /notifications/types:
    get:
      summary: Fetch a list of available Notification types
      tags:
        - Notifications
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/NotificationType'
  /notifications/test:
    post:
      summary: Send test notification to current user via Email and SMS
      tags:
        - Notifications
      responses:
        '204':
          description: Successful sending
          content: {}
        '400':
          description: Could happen if sending has failed
          content: {}
  /geofences:
    get:
      summary: Fetch a list of Geofences
      description: Without params, it returns a list of Geofences the user has access to
      tags:
        - Geofences
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Geofence'
    post:
      summary: Create a Geofence
      tags:
        - Geofences
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Geofence'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Geofence'
      x-codegen-request-body-name: body
  /geofences/{id}:
    put:
      summary: Update a Geofence
      tags:
        - Geofences
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Geofence'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Geofence'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Geofence
      tags:
        - Geofences
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /events/{id}:
    get:
      tags:
        - Events
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Event'
  /reports/route:
    get:
      summary: >-
        Fetch a list of Positions within the time period for the Devices or
        Groups
      description: At least one _deviceId_ or one _groupId_ must be passed
      tags:
        - Reports
      parameters:
        - name: deviceId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: groupId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
            application/vnd.openxmlformats-officedocument.spreadsheetml.sheet:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
  /reports/events:
    get:
      summary: Fetch a list of Events within the time period for the Devices or Groups
      description: At least one _deviceId_ or one _groupId_ must be passed
      tags:
        - Reports
      parameters:
        - name: deviceId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: groupId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: type
          in: query
          description: '% can be used to return events of all types'
          style: form
          explode: false
          schema:
            type: array
            items:
              type: string
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Event'
            application/vnd.openxmlformats-officedocument.spreadsheetml.sheet:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Event'
  /reports/summary:
    get:
      summary: >-
        Fetch a list of ReportSummary within the time period for the Devices or
        Groups
      description: At least one _deviceId_ or one _groupId_ must be passed
      tags:
        - Reports
      parameters:
        - name: deviceId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: groupId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportSummary'
            application/vnd.openxmlformats-officedocument.spreadsheetml.sheet:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportSummary'
  /reports/trips:
    get:
      summary: >-
        Fetch a list of ReportTrips within the time period for the Devices or
        Groups
      description: At least one _deviceId_ or one _groupId_ must be passed
      tags:
        - Reports
      parameters:
        - name: deviceId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: groupId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportTrips'
            application/vnd.openxmlformats-officedocument.spreadsheetml.sheet:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportTrips'
  /reports/stops:
    get:
      summary: >-
        Fetch a list of ReportStops within the time period for the Devices or
        Groups
      description: At least one _deviceId_ or one _groupId_ must be passed
      tags:
        - Reports
      parameters:
        - name: deviceId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: groupId
          in: query
          style: form
          explode: true
          schema:
            type: array
            items:
              type: integer
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportStops'
            application/vnd.openxmlformats-officedocument.spreadsheetml.sheet:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ReportStops'
  /statistics:
    get:
      summary: Fetch server Statistics
      tags:
        - Statistics
      parameters:
        - name: from
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
        - name: to
          in: query
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          required: true
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Statistics'
  /calendars:
    get:
      summary: Fetch a list of Calendars
      description: Without params, it returns a list of Calendars the user has access to
      tags:
        - Calendars
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Calendar'
    post:
      summary: Create a Calendar
      tags:
        - Calendars
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Calendar'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Calendar'
      x-codegen-request-body-name: body
  /calendars/{id}:
    put:
      summary: Update a Calendar
      tags:
        - Calendars
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Calendar'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Calendar'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Calendar
      tags:
        - Calendars
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /attributes/computed:
    get:
      summary: Fetch a list of Attributes
      description: Without params, it returns a list of Attributes the user has access to
      tags:
        - Attributes
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Attribute'
    post:
      summary: Create an Attribute
      tags:
        - Attributes
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Attribute'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Attribute'
      x-codegen-request-body-name: body
  /attributes/computed/{id}:
    put:
      summary: Update an Attribute
      tags:
        - Attributes
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Attribute'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Attribute'
      x-codegen-request-body-name: body
    delete:
      summary: Delete an Attribute
      tags:
        - Attributes
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /drivers:
    get:
      summary: Fetch a list of Drivers
      description: Without params, it returns a list of Drivers the user has access to
      tags:
        - Drivers
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Driver'
    post:
      summary: Create a Driver
      tags:
        - Drivers
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Driver'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Driver'
      x-codegen-request-body-name: body
  /drivers/{id}:
    put:
      summary: Update a Driver
      tags:
        - Drivers
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Driver'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Driver'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Driver
      tags:
        - Drivers
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
  /maintenance:
    get:
      summary: Fetch a list of Maintenance
      description: Without params, it returns a list of Maintenance the user has access to
      tags:
        - Maintenance
      parameters:
        - name: all
          in: query
          description: Can only be used by admins or managers to fetch all entities
          schema:
            type: boolean
        - name: userId
          in: query
          description: Standard users can use this only with their own _userId_
          schema:
            type: integer
        - name: deviceId
          in: query
          description: >-
            Standard users can use this only with _deviceId_s, they have access
            to
          schema:
            type: integer
        - name: groupId
          in: query
          description: >-
            Standard users can use this only with _groupId_s, they have access
            to
          schema:
            type: integer
        - name: refresh
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Maintenance'
    post:
      summary: Create a Maintenance
      tags:
        - Maintenance
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Maintenance'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Maintenance'
      x-codegen-request-body-name: body
  /maintenance/{id}:
    put:
      summary: Update a Maintenance
      tags:
        - Maintenance
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Maintenance'
        required: true
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Maintenance'
      x-codegen-request-body-name: body
    delete:
      summary: Delete a Maintenance
      tags:
        - Maintenance
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '204':
          description: No Content
          content: {}
components:
  schemas:
    Position:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique position record identifier
        deviceId:
          type: integer
          format: int64
          description: Identifier of the device that reported this position
        protocol:
          type: string
          description: Device protocol name that produced the message
        deviceTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        fixTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        serverTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        valid:
          type: boolean
          description: Indicates whether the position was marked as valid by the device
        latitude:
          type: number
          description: Latitude in decimal degrees
        longitude:
          type: number
          description: Longitude in decimal degrees
        altitude:
          type: number
          description: Altitude above sea level in meters
        speed:
          type: number
          description: in knots
        course:
          type: number
          description: Heading in degrees (0-360) where 0 is true north
        address:
          type: string
          description: Resolved reverse-geocoded address if available
        accuracy:
          type: number
          description: Estimated positional accuracy in meters when provided
        network:
          type: object
          description: Network metadata (e.g. cell or Wi-Fi data) supplied by the device
          properties: {}
        geofenceIds:
          type: array
          items:
            type: integer
          description: List of geofence ids applicable to this position
        attributes:
          type: object
          description: Custom key-value attributes sent by the device or enrichments
          properties: {}
    User:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique user identifier
        name:
          type: string
          description: User display name
        email:
          type: string
          description: Email address used for login and notifications
        phone:
          type: string
          nullable: true
          description: Contact phone number for alerts
        readonly:
          type: boolean
          description: When true, the user cannot change settings
        administrator:
          type: boolean
          description: Grants full administrative privileges when enabled
        map:
          type: string
          nullable: true
          description: Preferred default map layer for the user
        latitude:
          type: number
          description: Default map center latitude for this user
        longitude:
          type: number
          description: Default map center longitude for this user
        zoom:
          type: integer
          description: Default map zoom level on login
        password:
          type: string
          description: Password for user authentication
        coordinateFormat:
          type: string
          nullable: true
          description: Preferred coordinate display format
        disabled:
          type: boolean
          description: Indicates whether the user account is disabled
        expirationTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
          nullable: true
        deviceLimit:
          type: integer
          description: Maximum number of devices the user can manage
        userLimit:
          type: integer
          description: Maximum number of subordinate users
        deviceReadonly:
          type: boolean
          description: Restricts user from modifying device attributes
        limitCommands:
          type: boolean
          description: Prevents user from sending unsupported commands
        fixedEmail:
          type: boolean
          description: Locks the email field to avoid changes
        poiLayer:
          type: string
          nullable: true
          description: External POI layer configured for the user
        attributes:
          type: object
          description: Additional custom user attributes
          properties: {}
    Server:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique server configuration identifier
        registration:
          type: boolean
          description: Whether new user registrations are allowed
        readonly:
          type: boolean
          description: When true only administrators can modify server-wide settings
        deviceReadonly:
          type: boolean
          description: Disallow device attribute changes for non-admins
        limitCommands:
          type: boolean
          description: Restrict command execution to supported protocol commands
        map:
          type: string
          description: Default map layer identifier
        bingKey:
          type: string
          description: Bing Maps API key used when Bing is selected as a provider
        mapUrl:
          type: string
          description: Custom tile server URL template if configured
        poiLayer:
          type: string
          description: External point-of-interest layer configuration
        announcement:
          type: string
          description: Message displayed to all users in the web application
        latitude:
          type: number
          description: Default map center latitude
        longitude:
          type: number
          description: Default map center longitude
        zoom:
          type: integer
          description: Default map zoom level
        version:
          type: string
          description: Traccar server version string
        forceSettings:
          type: boolean
          description: Forces users to use the server-wide settings instead of their own
        coordinateFormat:
          type: string
          description: Default coordinate format for displaying positions
        openIdEnabled:
          type: boolean
          description: Indicates whether OpenID authentication is available
        openIdForce:
          type: boolean
          description: Require OpenID authentication for all users when enabled
        attributes:
          type: object
          description: Additional server-level configuration values
          properties: {}
    Command:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique saved command identifier
        deviceId:
          type: integer
          format: int64
          description: Target device identifier when the command is bound to one device
        description:
          type: string
          description: User friendly label displayed in the UI
        type:
          type: string
          description: Command type as defined by the device protocol
        textChannel:
          type: boolean
          description: Whether to send the command using the SMS channel
        attributes:
          type: object
          description: Additional parameters required by the command type
          properties: {}
    Device:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique identifier assigned by Traccar
        name:
          type: string
          description: Human friendly device label
        uniqueId:
          type: string
          description: Hardware or protocol specific unique identifier
        status:
          type: string
          description: Current connection status such as online, offline, or unknown
        disabled:
          type: boolean
          description: Whether the device is disabled by an administrator
        lastUpdate:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
          nullable: true
        positionId:
          type: integer
          format: int64
          nullable: true
          description: Identifier of the last known position
        groupId:
          type: integer
          format: int64
          nullable: true
          description: Parent group identifier when the device is assigned to a group
        phone:
          type: string
          nullable: true
          description: Contact phone number used for SMS commands
        model:
          type: string
          nullable: true
          description: Device model or hardware revision
        contact:
          type: string
          nullable: true
          description: Responsible person's contact information
        category:
          type: string
          nullable: true
          description: Free form category used for grouping devices in the UI
        attributes:
          type: object
          description: Custom attributes for protocol or business specific data
          properties: {}
    Group:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique group identifier
        name:
          type: string
          description: Group display name
        groupId:
          type: integer
          format: int64
          description: Parent group identifier for nested grouping
        attributes:
          type: object
          description: Arbitrary metadata attached to the group
          properties: {}
    Permission:
      type: object
      properties:
        userId:
          type: integer
          format: int64
          description: User id, can be only first parameter
        deviceId:
          type: integer
          format: int64
          description: >-
            Device id, can be first parameter or second only in combination with
            userId
        groupId:
          type: integer
          format: int64
          description: >-
            Group id, can be first parameter or second only in combination with
            userId
        geofenceId:
          type: integer
          format: int64
          description: Geofence id, can be second parameter only
        notificationId:
          type: integer
          format: int64
          description: Notification id, can be second parameter only
        calendarId:
          type: integer
          format: int64
          description: >-
            Calendar id, can be second parameter only and only in combination
            with userId
        attributeId:
          type: integer
          format: int64
          description: Computed attribute id, can be second parameter only
        driverId:
          type: integer
          format: int64
          description: Driver id, can be second parameter only
        managedUserId:
          type: integer
          format: int64
          description: >-
            User id, can be second parameter only and only in combination with
            userId
        commandId:
          type: integer
          format: int64
          description: Saved command id, can be second parameter only
      description: >-
        This is a permission map that contain two object indexes. It is used to
        link/unlink objects. Order is important. Example: { deviceId:8,
        geofenceId: 16 }
    CommandType:
      type: object
      properties:
        type:
          type: string
          description: Command type identifier
    Geofence:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique identifier for the geofence
        name:
          type: string
          description: Human-readable name shown in lists and maps
        description:
          type: string
          description: Details about the geofence for display in the UI
        area:
          type: string
          description: Geofence area definition encoded as a WKT string
        calendarId:
          type: integer
          format: int64
          description: Calendar identifier limiting when the geofence is active
        attributes:
          type: object
          description: Custom key-value pairs for integrations or UI overrides
          properties: {}
    Notification:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique identifier for the notification
        type:
          type: string
          description: Notification category such as geofenceEnter or ignitionOn
        description:
          type: string
          nullable: true
          description: User-defined text describing the notification
        always:
          type: boolean
          description: Whether the notification triggers regardless of schedule
        commandId:
          type: integer
          format: int64
          description: Identifier of the command to send when the notification triggers
        notificators:
          type: string
          description: Comma-separated delivery channels (for example, web, mail)
        calendarId:
          type: integer
          format: int64
          description: Calendar identifier restricting when the notification is active
        attributes:
          type: object
          description: Additional custom attributes used by notificators or templates
          properties: {}
    NotificationType:
      type: object
      properties:
        type:
          type: string
          description: Notification type identifier
    Event:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique event identifier
        type:
          type: string
          description: Event type name
        eventTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        deviceId:
          type: integer
          format: int64
          description: Device associated with the event
        positionId:
          type: integer
          format: int64
          description: Position record linked to the event when applicable
        geofenceId:
          type: integer
          format: int64
          description: Geofence referenced by the event if any
        maintenanceId:
          type: integer
          description: Maintenance record tied to the event
        attributes:
          type: object
          description: Additional event-specific attributes
          properties: {}
    ReportSummary:
      type: object
      properties:
        deviceId:
          type: integer
          format: int64
          description: Device identifier for the summary row
        deviceName:
          type: string
          description: Human readable device name
        maxSpeed:
          type: number
          description: in knots
        averageSpeed:
          type: number
          description: in knots
        distance:
          type: number
          description: in meters
        spentFuel:
          type: number
          description: in liters
        engineHours:
          type: integer
          description: Engine hours accumulated for the report period, in milliseconds
    ReportTrips:
      type: object
      properties:
        deviceId:
          type: integer
          format: int64
          description: Device identifier for the trip
        deviceName:
          type: string
          description: Human readable device name
        maxSpeed:
          type: number
          description: in knots
        averageSpeed:
          type: number
          description: in knots
        distance:
          type: number
          description: in meters
        spentFuel:
          type: number
          description: in liters
        duration:
          type: integer
          description: Trip duration in milliseconds
        startTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        startAddress:
          type: string
          description: Address where the trip started
        startLat:
          type: number
          description: Starting latitude in decimal degrees
        startLon:
          type: number
          description: Starting longitude in decimal degrees
        endTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        endAddress:
          type: string
          description: Address where the trip ended
        endLat:
          type: number
          description: Ending latitude in decimal degrees
        endLon:
          type: number
          description: Ending longitude in decimal degrees
        driverUniqueId:
          type: string
          description: Unique identifier of the driver assigned to the trip
        driverName:
          type: string
          description: Name of the driver assigned to the trip
    ReportStops:
      type: object
      properties:
        deviceId:
          type: integer
          format: int64
          description: Device identifier for the stop
        deviceName:
          type: string
          description: Human readable device name
        duration:
          type: integer
          description: Stop duration in milliseconds
        startTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        address:
          type: string
          description: Address where the stop occurred
        lat:
          type: number
          description: Stop latitude in decimal degrees
        lon:
          type: number
          description: Stop longitude in decimal degrees
        endTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        spentFuel:
          type: number
          description: in liters
        engineHours:
          type: integer
          description: Engine hours accumulated during the stop, in milliseconds
    Statistics:
      type: object
      properties:
        captureTime:
          type: string
          description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
          format: date-time
        activeUsers:
          type: integer
          description: Number of active users in the capture period
        activeDevices:
          type: integer
          description: Number of active devices in the capture period
        requests:
          type: integer
          description: Total API requests processed
        messagesReceived:
          type: integer
          description: Number of device messages received
        messagesStored:
          type: integer
          description: Number of device messages stored to the database
    DeviceAccumulators:
      type: object
      properties:
        deviceId:
          type: integer
          format: int64
          description: Device identifier for the accumulator entry
        totalDistance:
          type: number
          description: in meters
        hours:
          type: number
          description: Total engine hours recorded by the device, in milliseconds
    Calendar:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique calendar identifier
        name:
          type: string
          description: Calendar display name
        data:
          type: string
          description: base64 encoded in iCalendar format
        attributes:
          type: object
          description: Custom calendar attributes
          properties: {}
    Attribute:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique computed attribute identifier
        description:
          type: string
          description: Human readable name of the attribute
        attribute:
          type: string
          description: Attribute name used in expressions
        expression:
          type: string
          description: Expression that defines how the attribute is calculated
        type:
          type: string
          description: String|Number|Boolean
    Driver:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique driver identifier
        name:
          type: string
          description: Driver full name
        uniqueId:
          type: string
          description: Unique external identifier for the driver
        attributes:
          type: object
          description: Custom driver attributes
          properties: {}
    Maintenance:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique maintenance item identifier
        name:
          type: string
          description: Maintenance task name
        type:
          type: string
          description: Metric the maintenance is based on
        start:
          type: number
          description: Current accumulated value when maintenance tracking starts
        period:
          type: number
          description: Threshold value after which maintenance is due
        attributes:
          type: object
          description: Custom maintenance attributes
          properties: {}
    QueuedCommand:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Identifier of the queued command job
        deviceId:
          type: integer
          format: int64
          description: Device identifier the queued command will be delivered to
        type:
          type: string
          description: Command type that will be executed
        textChannel:
          type: boolean
          description: Indicates whether the queued command uses SMS delivery
        attributes:
          type: object
          description: Stored parameters for the queued command
          properties: {}
    NotificationMessage:
      type: object
      properties:
        subject:
          type: string
          description: Subject or title of the notification
        digest:
          type: string
          description: Short summary shown in compact contexts; defaults to the body when omitted
        body:
          type: string
          description: Full notification text
        priority:
          type: boolean
          description: Whether the message should be treated as high priority
      required:
        - body
    Order:
      type: object
      properties:
        id:
          type: integer
          format: int64
          description: Unique order identifier
        uniqueId:
          type: string
          description: External order identifier used by clients
        description:
          type: string
          description: Additional details about the order assignment
        fromAddress:
          type: string
          description: Pickup location address
        toAddress:
          type: string
          description: Destination address
        attributes:
          type: object
          description: Custom order attributes
          properties: {}
  parameters:
    entityId:
      name: id
      in: path
      required: true
      schema:
        type: integer
        format: int64
    all:
      name: all
      in: query
      description: Can only be used by admins or managers to fetch all entities
      schema:
        type: boolean
    refresh:
      name: refresh
      in: query
      schema:
        type: boolean
    userId:
      name: userId
      in: query
      description: Standard users can use this only with their own _userId_
      schema:
        type: integer
        format: int64
    deviceId:
      name: deviceId
      in: query
      description: Standard users can use this only with _deviceId_s, they have access to
      schema:
        type: integer
        format: int64
    groupId:
      name: groupId
      in: query
      description: Standard users can use this only with _groupId_s, they have access to
      schema:
        type: integer
        format: int64
    deviceIdArray:
      name: deviceId
      in: query
      style: form
      explode: true
      schema:
        type: array
        items:
          type: integer
          format: int64
    groupIdArray:
      name: groupId
      in: query
      style: form
      explode: true
      schema:
        type: array
        items:
          type: integer
          format: int64
    fromTime:
      name: from
      in: query
      description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
      required: true
      schema:
        type: string
        format: date-time
    toTime:
      name: to
      in: query
      description: in ISO 8601 format. eg. `1963-11-22T18:30:00Z`
      required: true
      schema:
        type: string
        format: date-time
  requestBodies:
    Device:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Device'
      required: true
    Permission:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Permission'
      required: true
    Group:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Group'
      required: true
    User:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/User'
      required: true
    Geofence:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Geofence'
      required: true
    Calendar:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Calendar'
      required: true
    Attribute:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Attribute'
      required: true
    Driver:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Driver'
      required: true
    Command:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Command'
      required: true
    Notification:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Notification'
      required: true
    Maintenance:
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Maintenance'
      required: true
    """
