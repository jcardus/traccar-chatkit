"""Shared agent context type for ChatKit tools."""

from __future__ import annotations

from typing import Annotated, Any

from chatkit.agents import AgentContext
from pydantic import ConfigDict, Field

from .neon_store import NeonStore


class TraccarAgentContext(AgentContext):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    store: Annotated[NeonStore, Field(exclude=True)]
    request_context: dict[str, Any]
