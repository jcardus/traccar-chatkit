"""Support-escalation tools: specialist consult first, human email fallback."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
import httpx
from agents import RunContextWrapper, function_tool
from chatkit.types import AssistantMessageItem, UserMessageItem

from .agent_context import TraccarAgentContext
from .traccar import invoke

logger = logging.getLogger(__name__)


def _user_message_text(item: UserMessageItem) -> str:
    parts: list[str] = []
    for part in item.content:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _agent_config() -> tuple[str | None, float, str | None]:
    """Read specialist-agent server config from the environment."""
    url = os.environ.get("CLAUDE_AGENT_URL", "").strip() or None
    try:
        timeout = float(os.environ.get("CLAUDE_AGENT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    api_key = os.environ.get("CLAUDE_AGENT_API_KEY", "").strip() or None
    return url, timeout, api_key


def _parse_agent_response(payload: Any, raw_text: str) -> tuple[str | None, bool, str]:
    """Parse an agent-server reply into (answer, needs_human, confidence).

    The server contract is {"answer", "needs_human", "confidence"}; accept
    answer-shaped JSON or plain text leniently.
    """
    needs_human = False
    confidence = ""
    answer: str | None = None
    if isinstance(payload, dict):
        needs_human = payload.get("needs_human") is True
        raw_confidence = payload.get("confidence")
        if isinstance(raw_confidence, str):
            confidence = raw_confidence.strip().lower()
        for key in ("answer", "response", "reply", "text", "content", "message", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                answer = value.strip()
                break
    elif isinstance(payload, str) and payload.strip():
        answer = payload.strip()
    if answer is None:
        text = (raw_text or "").strip()
        answer = text or None
    return answer, needs_human, confidence


async def _ask_support_specialist(
    question: str, conversation: str, user_email: str, thread_id: str
) -> tuple[str | None, bool, str]:
    """Ask the specialist agent server. Returns (answer, needs_human, confidence)."""
    url, timeout, api_key = _agent_config()
    if not url:
        logger.warning("Specialist agent skipped: thread=%s (CLAUDE_AGENT_URL not set)", thread_id)
        return None, False, ""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "question": question,
        "thread_id": thread_id,
        "user_email": user_email,
        "conversation": conversation,
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception:
        logger.exception(
            "Specialist agent request failed: thread=%s took=%.2fs",
            thread_id,
            time.monotonic() - start,
        )
        return None, False, ""
    elapsed = time.monotonic() - start
    logger.info(
        "Specialist agent responded: thread=%s status=%s took=%.2fs",
        thread_id,
        resp.status_code,
        elapsed,
    )
    if resp.status_code >= 400:
        logger.warning(
            "Specialist agent error: thread=%s status=%s body=%.500s",
            thread_id,
            resp.status_code,
            resp.text,
        )
        return None, False, ""
    try:
        payload: Any = resp.json()
    except ValueError:
        payload = None
    answer, needs_human, confidence = _parse_agent_response(payload, resp.text)
    if not answer and not needs_human:
        logger.warning("Specialist agent returned no usable answer: thread=%s", thread_id)
    return answer, needs_human, confidence


@function_tool(
    description_override="Ask the support specialist agent for an answer to relay to the user."
)
async def ask_support_specialist(ctx: RunContextWrapper[TraccarAgentContext], question: str) -> str:
    """Consult the specialist agent server and return its answer for the user."""
    request = ctx.context.request_context.get("request")
    thread_id = ctx.context.thread.id
    logger.info(
        "Specialist consult started: thread=%s question_length=%d request_present=%s",
        thread_id,
        len(question),
        request is not None,
    )

    try:
        session = invoke("get", "session", "", request) if request else None
        user_email = (session.get("email") if session else None) or "unknown"
        logger.info(
            "Specialist consult context loaded: thread=%s user_email_present=%s",
            thread_id,
            user_email != "unknown",
        )
        conversation = await _format_support_thread(ctx, question)
        answer, needs_human, confidence = await _ask_support_specialist(
            question, conversation, user_email, thread_id
        )
    except Exception:
        logger.exception("Specialist consult failed: thread=%s", thread_id)
        answer, needs_human, confidence = None, False, ""

    if needs_human:
        logger.info("Specialist deferred to human: thread=%s", thread_id)
        return (
            "The specialist determined this question needs a human and did not answer "
            "it. Call the forward_to_real_agent tool with the user's question to "
            "escalate it to a human agent."
        )
    if answer:
        logger.info("Specialist answered: thread=%s confidence=%s", thread_id, confidence)
        if confidence == "low":
            return (
                "The specialist answered with low confidence. Present this answer to the "
                "user tentatively in your own words, making clear you are not fully "
                "certain:\n\n" + answer
            )
        return (
            "The specialist answered the question. "
            "Present this answer to the user in your own words:\n\n" + answer
        )
    logger.info("Specialist unreachable, escalating: thread=%s", thread_id)
    return (
        "The specialist could not be reached or did not answer. Call the "
        "forward_to_real_agent tool with the user's question to escalate it to a "
        "human agent."
    )


@function_tool(description_override="Forward the user question to a human support agent via email.")
async def forward_to_real_agent(ctx: RunContextWrapper[TraccarAgentContext], question: str) -> str:
    """Escalate the user's question to a human via support email."""
    request = ctx.context.request_context.get("request")
    thread_id = ctx.context.thread.id
    logger.info(
        "Support forwarding started: thread=%s question_length=%d request_present=%s",
        thread_id,
        len(question),
        request is not None,
    )

    try:
        session = invoke("get", "session", "", request) if request else None
        user_email = (session.get("email") if session else None) or "unknown"
        conversation = await _format_support_thread(ctx, question)

        ses = boto3.client("ses", region_name="us-east-1")
        response = ses.send_email(
            Source="support@gpsmanager.io",
            Destination={"ToAddresses": ["support@fleetmap.io"]},
            Message={
                "Subject": {"Data": f"Support request from {user_email}"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"User: {user_email}\nThread: {thread_id}\n\n"
                            f"Conversation:\n{conversation}"
                        )
                    }
                },
            },
        )
        response_metadata = response.get("ResponseMetadata", {})
        logger.info(
            "Support forwarding accepted by SES: thread=%s message_id=%s http_status=%s",
            thread_id,
            response.get("MessageId"),
            response_metadata.get("HTTPStatusCode"),
        )
    except Exception:
        logger.exception("Support forwarding failed: thread=%s", thread_id)
        contact = os.environ.get("SUPPORT_CONTACT_EMAIL", "support@fleetmap.io").strip()
        contact = contact or "support@fleetmap.io"
        return (
            "Forwarding to a human agent failed. Tell the user to contact our support "
            f"team directly at {contact}."
        )

    return "Your question has been forwarded to our support team. They will get back to you soon."


async def _format_support_thread(ctx: RunContextWrapper[TraccarAgentContext], question: str) -> str:
    thread_id = ctx.context.thread.id
    context = ctx.context.request_context
    after = None
    messages: list[str] = []

    while True:
        page = await ctx.context.store.load_thread_items(thread_id, after, 100, "asc", context)
        for item in page.data:
            if isinstance(item, UserMessageItem):
                text = _user_message_text(item)
                attachment_names = [attachment.name for attachment in item.attachments]
                if attachment_names:
                    attachment_list = ", ".join(attachment_names)
                    text = (
                        f"{text}\nAttachments: {attachment_list}"
                        if text
                        else (f"Attachments: {attachment_list}")
                    )
                role = "User"
            elif isinstance(item, AssistantMessageItem):
                text = "\n".join(part.text for part in item.content if getattr(part, "text", None))
                role = "Assistant"
            else:
                continue

            if text:
                messages.append(f"[{item.created_at.isoformat()}] {role}:\n{text}")

        if not page.has_more:
            break
        if not page.after or page.after == after:
            raise RuntimeError(f"Could not paginate support thread {thread_id}")
        after = page.after

    return "\n\n".join(messages) or f"[Current request]\nUser:\n{question}"
