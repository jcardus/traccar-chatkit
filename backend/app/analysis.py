"""Analyse oversized API results with the hosted code interpreter.

A result too large for the conversation is uploaded to an OpenAI container
instead. A small analyst agent with the code interpreter answers questions over
the full file, so only its short answer reaches the main model.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from agents import Agent, CodeInterpreterTool, Runner, function_tool
from openai import AsyncOpenAI

from .constants import MODEL

logger = logging.getLogger(__name__)

CONTAINER_IDLE_MINUTES = 20

ANALYST_INSTRUCTIONS = """
You are a data analyst for a GPS fleet tracking platform.

A JSON file from the platform API is available in the code interpreter at the path you are given.
Load it with Python and compute exactly what is asked. Never estimate; compute.
Answer concisely with the computed numbers.

Speeds in the data are in knots. Report speeds in km/h (multiply by 1.852).
Never mention that the platform backend is Traccar.
Refer to devices by name when the data has names, not by internal IDs.
"""


async def store_result(result: Any) -> dict[str, str] | None:
    """Upload a result to a fresh code-interpreter container.

    Returns the container id and file path, or None if the upload failed.
    """
    try:
        client = AsyncOpenAI()
        container = await client.containers.create(
            name="fleet-data",
            expires_after={"anchor": "last_active_at", "minutes": CONTAINER_IDLE_MINUTES},
        )
        uploaded = await client.containers.files.create(
            container.id,
            file=("data.json", json.dumps(result).encode(), "application/json"),
        )
    except Exception:
        logger.exception("Failed to store large result for analysis")
        return None
    return {"container_id": container.id, "path": uploaded.path}


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
        run = await Runner.run(analyst, f"The data file is at {path}.\n\n{question}")
    except Exception:
        logger.exception("analyze_data failed after %.2fs", time.monotonic() - start)
        return (
            "The analysis failed (the stored data may have expired). "
            "Narrow the query or fetch the data client-side in show_html instead."
        )
    logger.info("analyze_data took %.2fs", time.monotonic() - start)
    return str(run.final_output)
