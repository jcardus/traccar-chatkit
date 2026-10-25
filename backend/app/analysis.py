"""Analyse oversized API results with the hosted code interpreter.

A result too large for the conversation is uploaded to an OpenAI container
instead. A small analyst agent with the code interpreter answers questions over
the full file, so only its short answer reaches the main model.

Container files are capped at 50 MiB. Larger results are gzipped, then split
into parts when the gzip itself exceeds the cap.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Any

from agents import Agent, CodeInterpreterTool, Runner, function_tool
from openai import AsyncOpenAI

from .constants import MODEL

logger = logging.getLogger(__name__)

CONTAINER_IDLE_MINUTES = 20
# OpenAI rejects container files at 50 MiB. Stay a little under that.
MAX_CONTAINER_FILE_BYTES = 50 * 1024 * 1024 - 65_536
MAX_CONTAINER_PARTS = 40
MANIFEST_NAME = "fleet-parts-manifest.json"

ANALYST_INSTRUCTIONS = """
You are a data analyst for a GPS fleet tracking platform.

A JSON file from the platform API is available in the code interpreter at the path you are given.
Load it with Python and compute exactly what is asked. Never estimate; compute.
Answer concisely with the computed numbers.

How to load the file:
- If the path ends with fleet-parts-manifest.json, it is a split dataset. JSON-load it, then load every path in its "parts" list, in order. Concatenate them when they are lists.
- If a path ends with .json.gz, it is gzip-compressed JSON: json.load(gzip.open(path)).
- Otherwise JSON-load the file; that value is the data.

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


async def store_result(result: Any) -> dict[str, str] | None:
    """Upload a result to a fresh code-interpreter container.

    Returns the container id and file path, or None if the result cannot fit
    or the upload failed. The path is a gzip file or a parts manifest when the
    raw JSON exceeds the container file limit.
    """
    parts = _pack(result)
    if not parts:
        return None
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
    except Exception:
        logger.exception("Failed to store large result for analysis")
        return None
    logger.info(
        "Stored large result in %s at %s (%d bytes uploaded, %d file(s))",
        container.id,
        path,
        uploaded_bytes,
        len(uploaded_paths) if len(uploaded_paths) == 1 else len(uploaded_paths) + 1,
    )
    return {"container_id": container.id, "path": path}


@function_tool(
    description_override=(
        "Compute exact answers over a large stored API result using Python (totals, maximums, "
        "counts, averages, filtering). Use the container_id and path from invoke_api's "
        "stored_data. Returns a short text answer."
    )
)
async def analyze_data(container_id: str, path: str, question: str) -> str:
    logger.info("TOOL: analyze_data container=%s path=%s", container_id, path)
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
            "If the path ends with .json.gz, decompress it before parsing. "
            "If it ends with fleet-parts-manifest.json, load every path in its "
            "parts list, in order, and concatenate lists.\n\n"
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
