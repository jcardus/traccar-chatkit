"""Analyse oversized API results with the hosted code interpreter.

A result too large for the conversation is uploaded to an OpenAI container
instead. A small analyst agent with the code interpreter answers questions over
the full file, so only its short answer reaches the main model.

Container files are capped at 50 MiB. Larger results are gzipped, then split
into parts when the gzip itself exceeds the cap. A body that is already too
large to parse on this host is uploaded as the gzip it arrived in.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import time
from typing import Any

from agents import Agent, CodeInterpreterTool, Runner, function_tool
from openai import AsyncOpenAI

from .constants import MODEL
from .traccar import UnparsedBody

logger = logging.getLogger(__name__)

# While set, analyze_data does not run and the assistant is told to stop after
# the file check. Leave this off so the model can choose the next step.
SKIP_ANALYSIS = False
CONTAINER_IDLE_MINUTES = 20
# OpenAI rejects container files at 50 MiB. Stay a little under that.
MAX_CONTAINER_FILE_BYTES = 50 * 1024 * 1024 - 65_536
MAX_CONTAINER_PARTS = 40
MANIFEST_NAME = "fleet-parts-manifest.json"
# Raw JSON per split part. Gzip of this is under the container cap even when
# the text does not compress.
_PART_RAW_LIMIT = 32 * 1024 * 1024
_WS = frozenset((0x20, 0x09, 0x0A, 0x0D))
_VALUE_END = _WS | {ord(","), ord("]")}

ANALYST_INSTRUCTIONS = """
You are a data analyst for a GPS fleet tracking platform.

A JSON file from the platform API is in the code interpreter at the path you are given.
A path ending in .json.gz is gzip-compressed JSON. A path ending in fleet-parts-manifest.json lists part paths. Any other path is JSON.

The sandbox has about 1 GB of memory. Parsing JSON uses several times the text size. You decide how to read the file and how to compute the answer. Never estimate; compute.
Answer concisely with the computed numbers.

Speeds in the data are in knots. Report speeds in km/h (multiply by 1.852).
Never mention that the platform backend is Traccar.
Refer to devices by name when the data has names, not by internal IDs.
"""


def _dumps(result: Any) -> bytes:
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()


def _pack(result: Any) -> list[tuple[str, bytes, str]] | None:
    """Return (filename, body, content_type) parts that each fit the container limit.

    None means the result cannot fit, even split. A single part is the data
    itself. Several parts are gzipped slices; the caller adds a manifest.
    """
    raw = _dumps(result)
    if len(raw) <= MAX_CONTAINER_FILE_BYTES:
        return [("data.json", raw, "application/json")]
    compressed = gzip.compress(raw, compresslevel=6)
    if len(compressed) <= MAX_CONTAINER_FILE_BYTES:
        return [("data.json.gz", compressed, "application/gzip")]
    if not isinstance(result, list):
        logger.warning(
            "Could not pack result of %d bytes (gzip %d) under the %d byte container limit",
            len(raw),
            len(compressed),
            MAX_CONTAINER_FILE_BYTES,
        )
        return None
    return _pack_list(result, len(compressed))


def _pack_list(items: list[Any], gzip_bytes: int) -> list[tuple[str, bytes, str]] | None:
    if not items:
        return [("data.json", b"[]", "application/json")]
    per_item = max(1.0, gzip_bytes / len(items))
    chunk_size = max(1, int((MAX_CONTAINER_FILE_BYTES * 0.85) / per_item))
    parts: list[tuple[str, bytes, str]] = []
    index = 0
    while index < len(items):
        chunk = items[index : index + chunk_size]
        payload = gzip.compress(_dumps(chunk), compresslevel=6)
        if len(payload) > MAX_CONTAINER_FILE_BYTES:
            if chunk_size == 1:
                logger.warning(
                    "Single item gzipped to %d bytes; cannot store under %d",
                    len(payload),
                    MAX_CONTAINER_FILE_BYTES,
                )
                return None
            chunk_size = max(1, chunk_size // 2)
            continue
        if len(parts) >= MAX_CONTAINER_PARTS:
            logger.warning(
                "Result needs more than %d parts; refusing to store", MAX_CONTAINER_PARTS
            )
            return None
        parts.append((f"part-{len(parts) + 1:03d}.json.gz", payload, "application/gzip"))
        index += len(chunk)
    return parts


def _pack_unparsed(body: UnparsedBody) -> list[tuple[str, bytes, str]] | None:
    """Pack wire bytes that were never parsed.

    A gzip that already fits is uploaded unchanged. Anything larger is split
    from a stream so the uncompressed document is not held as one buffer.
    """
    if body.compressed and len(body.payload) >= 2 and body.payload[:2] == b"\x1f\x8b":
        if len(body.payload) <= MAX_CONTAINER_FILE_BYTES:
            return [("data.json.gz", body.payload, "application/gzip")]
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body.payload)) as handle:
                return _parts_from_stream(handle)
        except (OSError, EOFError, gzip.BadGzipFile):
            logger.warning("Could not split a %d byte gzip for the container", len(body.payload))
            return None
    if len(body.payload) <= MAX_CONTAINER_FILE_BYTES:
        return [("data.json", body.payload, "application/json")]
    compressed = gzip.compress(body.payload, compresslevel=6)
    if len(compressed) <= MAX_CONTAINER_FILE_BYTES:
        return [("data.json.gz", compressed, "application/gzip")]
    return _parts_from_stream(io.BytesIO(body.payload))


def _parts_from_stream(stream) -> list[tuple[str, bytes, str]] | None:
    splitter = _ArraySplitter(_PART_RAW_LIMIT)
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        splitter.feed(chunk)
        if splitter.failed or splitter.done:
            break
    groups = splitter.finish()
    if not groups:
        logger.warning("Unparsed result is not a JSON array that can be split")
        return None
    packed: list[bytes] = []
    for group in groups:
        payload = gzip.compress(b"[" + b",".join(group) + b"]", compresslevel=6)
        if len(payload) > MAX_CONTAINER_FILE_BYTES:
            logger.warning(
                "A split part gzipped to %d bytes; cannot store under %d",
                len(payload),
                MAX_CONTAINER_FILE_BYTES,
            )
            return None
        packed.append(payload)
    if len(packed) == 1:
        return [("data.json.gz", packed[0], "application/gzip")]
    return [
        (f"part-{index + 1:03d}.json.gz", payload, "application/gzip")
        for index, payload in enumerate(packed)
    ]


class _ArraySplitter:
    """Split a JSON array into raw element groups without parsing values."""

    def __init__(self, raw_limit: int):
        self.raw_limit = raw_limit
        self.parts: list[list[bytes]] = []
        self.current: list[bytes] = []
        self.current_len = 0
        self.failed = False
        self.done = False
        self._mode = "start"
        self._depth = 0
        self._string = False
        self._escape = False
        self._element = bytearray()

    def feed(self, chunk: bytes) -> None:
        if self.failed or self.done:
            return
        for byte in chunk:
            self._byte(byte)
            if self.failed or self.done:
                return

    def finish(self) -> list[list[bytes]] | None:
        if self.failed or not self.done:
            return None
        self._flush()
        if self.failed or not self.parts:
            return None
        return self.parts

    def _byte(self, byte: int) -> None:
        if self._mode == "start":
            if byte in _WS:
                return
            if byte != ord("["):
                self.failed = True
                return
            self._mode = "between"
            return
        if self._mode == "between":
            if byte in _WS or byte == ord(","):
                return
            if byte == ord("]"):
                self.done = True
                self._mode = "done"
                return
            self._mode = "value"
            self._depth = 0
            self._string = False
            self._escape = False
            self._element = bytearray()
        if self._mode != "value":
            return
        if self._string:
            self._element.append(byte)
            if self._escape:
                self._escape = False
            elif byte == ord("\\"):
                self._escape = True
            elif byte == ord('"'):
                self._string = False
                if self._depth == 0:
                    self._finish_element()
                    self._mode = "between"
            return
        if self._depth == 0 and byte in _VALUE_END:
            if self._element:
                self._finish_element()
            if byte == ord("]"):
                self.done = True
                self._mode = "done"
            else:
                self._mode = "between"
            return
        self._element.append(byte)
        if byte == ord('"'):
            self._string = True
        elif byte in (ord("{"), ord("[")):
            self._depth += 1
        elif byte in (ord("}"), ord("]")):
            self._depth -= 1
            if self._depth < 0:
                self.failed = True
                return
            if self._depth == 0:
                self._finish_element()
                self._mode = "between"

    def _finish_element(self) -> None:
        element = bytes(self._element)
        self._element = bytearray()
        if not element or self.failed:
            return
        extra = len(element) + (1 if self.current else 0)
        if self.current and self.current_len + extra > self.raw_limit:
            self._flush()
            extra = len(element)
        if not self.current and extra > self.raw_limit:
            self.parts.append([element])
            if len(self.parts) > MAX_CONTAINER_PARTS:
                self.failed = True
            return
        self.current.append(element)
        self.current_len += extra

    def _flush(self) -> None:
        if not self.current:
            return
        self.parts.append(self.current)
        self.current = []
        self.current_len = 0
        if len(self.parts) > MAX_CONTAINER_PARTS:
            self.failed = True


async def _upload_parts(parts: list[tuple[str, bytes, str]]) -> dict[str, str] | None:
    try:
        client = AsyncOpenAI()
        container = await client.containers.create(
            name="fleet-data",
            expires_after={"anchor": "last_active_at", "minutes": CONTAINER_IDLE_MINUTES},
        )
        uploaded_paths: list[str] = []
        uploaded_bytes = 0
        for name, payload, content_type in parts:
            uploaded = await client.containers.files.create(
                container.id,
                file=(name, payload, content_type),
            )
            uploaded_paths.append(uploaded.path)
            uploaded_bytes += len(payload)
        path = uploaded_paths[0]
        if len(uploaded_paths) > 1:
            manifest = json.dumps({"parts": uploaded_paths}).encode()
            uploaded = await client.containers.files.create(
                container.id,
                file=(MANIFEST_NAME, manifest, "application/json"),
            )
            path = uploaded.path
            uploaded_bytes += len(manifest)
        files = await _listed_files(client, container.id)
    except Exception:
        logger.exception("Failed to store large result for analysis")
        return None
    logger.info(
        "Stored large result in %s at %s (%d bytes uploaded, %d file(s)); container lists %s",
        container.id,
        path,
        uploaded_bytes,
        len(uploaded_paths) if len(uploaded_paths) == 1 else len(uploaded_paths) + 1,
        files,
    )
    return {"container_id": container.id, "path": path, "files": files}


async def _listed_files(client, container_id: str) -> list[dict[str, Any]]:
    """Read the container's own file list, so an upload is confirmed by a second call."""
    listed: list[dict[str, Any]] = []
    async for item in client.containers.files.list(container_id):
        listed.append(
            {
                "id": item.id,
                "path": item.path,
                "bytes": item.bytes,
                "source": item.source,
            }
        )
    return listed


async def store_result(result: Any) -> dict[str, str] | None:
    """Upload a result to a fresh code-interpreter container.

    Returns the container id and file path, or None if the result cannot fit
    or the upload failed. The path is a gzip file or a parts manifest when the
    raw JSON exceeds the container file limit.
    """
    parts = _pack(result)
    if not parts:
        return None
    return await _upload_parts(parts)


async def store_unparsed(body: UnparsedBody) -> dict[str, str] | None:
    """Upload response bytes that were never parsed.

    A gzip that fits the container is uploaded as those exact bytes. Returns
    the container id and file path, or None when the file cannot fit.
    """
    parts = _pack_unparsed(body)
    if not parts:
        return None
    return await _upload_parts(parts)


FILE_CHECK_INSTRUCTIONS = """
You decide whether a stored data file can be analyzed, and how.
You do not have the file. You only have the description you are given.
The code interpreter sandbox has about 1 GB of memory. Parsing JSON uses several times its text size.
Say whether it is analyzable and what approach you would use. Do not compute the user's answer from the sample.
"""


@function_tool(
    description_override=(
        "Ask whether a stored file can be analyzed. Pass the container_id, path, "
        "file byte size, uncompressed size, and a description of what the file contains "
        "(fields and a few sample records). Does not load the file or compute an answer."
    )
)
async def file_can_be_analyzed(
    container_id: str,
    path: str,
    file_bytes: int,
    uncompressed_bytes: int,
    contents: str,
) -> str:
    logger.info(
        "TOOL: file_can_be_analyzed container=%s path=%s file_bytes=%s uncompressed=%s",
        container_id,
        path,
        file_bytes,
        uncompressed_bytes,
    )
    checker = Agent(
        name="File check",
        model=MODEL,
        instructions=FILE_CHECK_INSTRUCTIONS,
        tools=[],
    )
    prompt = (
        f"container_id: {container_id}\n"
        f"path: {path}\n"
        f"file bytes: {file_bytes}\n"
        f"uncompressed bytes: {uncompressed_bytes}\n"
        f"what the file contains:\n{contents}\n\n"
        "Can this file be analyzed?"
    )
    start = time.monotonic()
    try:
        run = await Runner.run(checker, prompt)
    except Exception:
        logger.exception("file_can_be_analyzed failed after %.2fs", time.monotonic() - start)
        return "The file check failed. Report the stored file listing and do not estimate an answer."
    logger.info("file_can_be_analyzed took %.2fs", time.monotonic() - start)
    return str(run.final_output)


@function_tool(
    description_override=(
        "Compute exact answers over a large stored API result using Python (totals, maximums, "
        "counts, averages, filtering). Use the container_id and path from invoke_api's "
        "stored_data. Returns a short text answer."
    )
)
async def analyze_data(container_id: str, path: str, question: str) -> str:
    logger.info("TOOL: analyze_data container=%s path=%s", container_id, path)
    if SKIP_ANALYSIS:
        return (
            "Analysis is turned off. Call file_can_be_analyzed with this container_id "
            f"({container_id}), path ({path}), the file byte size, the uncompressed size, "
            "and a description of what the file contains. Do not estimate a count."
        )
    start = time.monotonic()
    analyst = Agent(
        name="Data analyst",
        model=MODEL,
        instructions=ANALYST_INSTRUCTIONS,
        tools=[
            CodeInterpreterTool(tool_config={"type": "code_interpreter", "container": container_id})
        ],
    )
    try:
        run = await Runner.run(
            analyst,
            f"The data file is at {path}.\n"
            "Decide how to read it and compute the answer.\n\n"
            f"{question}",
        )
    except Exception:
        logger.exception("analyze_data failed after %.2fs", time.monotonic() - start)
        return (
            "The analysis failed (the stored data may have expired). "
            "Narrow the query or fetch the data client-side in show_html instead."
        )
    logger.info("analyze_data took %.2fs", time.monotonic() - start)
    return str(run.final_output)
