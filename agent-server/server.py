#!/usr/bin/env python3
"""Live-answer agent server for the in-app chat assistant.

Runs on the ticketbot box next to ~/freescout-ticket-solver. Receives a support
question from the chat backend, runs Claude headless in strictly read-only
research mode, and returns an answer the chat assistant relays to the live user.

Endpoints:
    POST /ask    {"question", "conversation", "thread_id", "user_email"}
                 -> {"answer", "needs_human", "confidence"}
    GET  /health -> {"status": "ok"}

Stdlib only: `python3 server.py`. All configuration via environment (see
README.md). Importing this module has no side effects.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("agent-server")

DEFAULT_CLAUDE_ARGS = (
    "-p --output-format json --max-turns 12 "
    "--allowedTools Read,Glob,Grep,Bash(bin/fs_search.sh:*),Bash(bin/fs_get.sh:*)"
)

PROMPT_TEMPLATE = """LIVE-ANSWER MODE: answer ONE live chat user's question below.

You are the Fleetmap support specialist. You were called by another AI agent
(the in-app chat assistant) that is answering a live user RIGHT NOW. Your
answer will be relayed to that user promptly, so be direct and useful.

Do ONLY this task. Do not process the ticket queue, do not post notes, do not
touch state/processed.json, do not make code changes, do not modify anything.
This run is strictly read-only research plus one answer.

The user must never learn the platform backend is Traccar: never mention that
name.

## Live user question
{question}

## Chat transcript so far (oldest first; may be truncated to the most recent part)
{conversation}

User email (context only; never reveal anything sensitive): {user_email}

## How to research (fast: you have about {budget_seconds} seconds)
- Search the vehicle-tracking codebase at {repos_dir} for relevant error
  messages, config, and how the integration actually behaves. Grep for error
  strings from the ticket; read the relevant source; don't guess from file
  names alone.
- Search past resolved tickets for similar issues:
  `bin/fs_search.sh "<keyword>" closed` (try 1-2 keyword phrasings; this
  matches subject text, not full body). Read how the team resolved similar
  cases with `bin/fs_get.sh <id>` and match that tone and level of detail.
- Run every command from {solver_dir} (your working directory).

## Hard rules
- Never run bin/fs_note.sh, bin/code_worktree.sh, or bin/gh_pr.sh. Never post,
  push, commit, or modify anything, anywhere.
- Don't invent facts about the customer's account, vehicle, device, or billing
  that you can't verify from the question, the transcript, the codebase, or
  past tickets. State what's missing instead of filling gaps.
- If the issue is billing/payment, account access or security, a data deletion
  request, safety-related, or the customer sounds angry or escalated -- or you
  are not sure of the resolution -- set needs_human to true instead of
  answering.

## Output contract
Print ONLY one JSON object and nothing else: no markdown fences, no extra text.
{{"answer": "<concise direct answer, plain text, 2-6 sentences>",
  "needs_human": false,
  "confidence": "high|medium|low"}}
If time runs short, return your best-effort answer with "low" confidence.
"""


@dataclasses.dataclass(frozen=True)
class Config:
    bind: str
    port: int
    api_key: str
    log_level: str
    workdir: str
    repos_dir: str
    claude_cmd: list[str]
    claude_args: list[str]
    claude_timeout: float
    max_concurrent: int
    max_body: int
    max_context: int
    max_answer: int


def _env_int(env: Any, name: str, default: int) -> int:
    try:
        value = int(str(env.get(name, default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(env: Any, name: str, default: float) -> float:
    try:
        value = float(str(env.get(name, default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_log_level(env: Any) -> str:
    level = str(env.get("AGENT_LOG_LEVEL", "INFO")).strip().upper()
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"


def load_config(env: Any = None) -> Config:
    """Build config from a mapping (defaults to os.environ)."""
    if env is None:
        env = os.environ
    return Config(
        bind=env.get("AGENT_BIND", "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int(env, "AGENT_PORT", 8765),
        api_key=env.get("AGENT_API_KEY", "").strip(),
        log_level=_env_log_level(env),
        workdir=os.path.expanduser(
            env.get("AGENT_WORKDIR", "~/freescout-ticket-solver")
        ),
        repos_dir=os.path.expanduser(env.get("AGENT_REPOS_DIR", "~/repos")),
        claude_cmd=shlex.split(env.get("CLAUDE_CMD", "claude")) or ["claude"],
        claude_args=shlex.split(env.get("CLAUDE_ARGS", DEFAULT_CLAUDE_ARGS)),
        claude_timeout=_env_float(env, "CLAUDE_TIMEOUT", 100.0),
        max_concurrent=_env_int(env, "AGENT_MAX_CONCURRENT", 2),
        max_body=_env_int(env, "AGENT_MAX_BODY", 204800),
        max_context=_env_int(env, "AGENT_MAX_CONTEXT", 60000),
        max_answer=_env_int(env, "AGENT_MAX_ANSWER", 8000),
    )


def truncate_tail(text: str, limit: int) -> str:
    """Keep at most the last *limit* characters of *text*."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[-limit:]


def build_prompt(
    question: str,
    conversation: str,
    user_email: str,
    solver_dir: str,
    repos_dir: str,
    budget_seconds: int,
) -> str:
    """Render the headless-run prompt for one live question."""
    return PROMPT_TEMPLATE.format(
        question=question,
        conversation=conversation,
        user_email=user_email,
        solver_dir=solver_dir,
        repos_dir=repos_dir,
        budget_seconds=budget_seconds,
    )


def is_authorized(header_value: str | None, api_key: str) -> bool:
    """Check the Authorization header against the configured API key."""
    if not header_value or not api_key:
        return False
    try:
        return hmac.compare_digest(f"Bearer {api_key}", header_value.strip())
    except TypeError:
        return False


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _flags_from_dict(obj: dict[str, Any]) -> tuple[bool, str]:
    needs_human = obj.get("needs_human") is True
    confidence = obj.get("confidence")
    confidence = confidence.strip().lower() if isinstance(confidence, str) else ""
    return needs_human, confidence


def _first_text(obj: dict[str, Any]) -> str | None:
    for key in ("answer", "response", "reply", "text", "content", "message"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_agent_output(
    stdout: str | None, max_answer: int = 8000
) -> tuple[str | None, bool, str]:
    """Parse headless-Claude stdout into (answer, needs_human, confidence).

    Handles the `--output-format json` envelope ({"result": ...}), a bare
    answer object, a JSON string, or plain prose (kept as a low-confidence
    answer). Returns (None, False, "") when nothing usable is found.
    """
    text = _strip_fences((stdout or "").strip())
    if not text:
        return None, False, ""
    try:
        payload: Any = json.loads(text)
    except ValueError:
        return text[:max_answer], False, "low"
    inner_text: str | None = None
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        inner_text = _strip_fences(payload["result"].strip())
        if not inner_text:
            return None, False, ""
        try:
            payload = json.loads(inner_text)
        except ValueError:
            return inner_text[:max_answer], False, "low"
    if isinstance(payload, dict):
        needs_human, confidence = _flags_from_dict(payload)
        answer = _first_text(payload)
        if answer:
            return answer[:max_answer], needs_human, confidence
        if needs_human:
            return None, True, confidence
        return None, False, ""
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:max_answer], False, "low"
    return None, False, ""


def run_claude(cfg: Config, prompt: str) -> tuple[str, str, int]:
    """Run headless Claude once; returns (stdout, stderr, returncode).

    The prompt goes over stdin, never argv: flag grammars differ across CLI
    versions, and a greedy multi-value flag could otherwise swallow a trailing
    prompt argument. Raises subprocess.TimeoutExpired past cfg.claude_timeout.
    """
    cmd = [*cfg.claude_cmd, *cfg.claude_args]
    proc = subprocess.run(
        cmd,
        cwd=cfg.workdir,
        capture_output=True,
        text=True,
        input=prompt,
        timeout=cfg.claude_timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


class Handler(BaseHTTPRequestHandler):
    config: Config  # set by main() before serving
    slots: threading.BoundedSemaphore  # set by main() before serving

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/ask":
            self._send_json(404, {"error": "not found"})
            return
        cfg = self.config
        if not is_authorized(self.headers.get("Authorization"), cfg.api_key):
            logger.info("rejected: unauthorized request from %s", self.address_string())
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad content length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return
        if length > cfg.max_body:
            self._send_json(413, {"error": "body too large"})
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "json object required"})
            return
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self._send_json(400, {"error": "question is required"})
            return
        conversation = payload.get("conversation")
        conversation = conversation if isinstance(conversation, str) else ""
        thread_id = str(payload.get("thread_id") or "")
        user_email = str(payload.get("user_email") or "")
        logger.debug(
            "request: thread=%s qlen=%d clen=%d email_set=%s",
            thread_id,
            len(question),
            len(conversation),
            bool(user_email),
        )

        if not self.slots.acquire(blocking=False):
            logger.info("rejected: busy thread=%s", thread_id)
            self._send_json(503, {"error": "busy, retry shortly"})
            return
        start = time.monotonic()
        try:
            prompt = build_prompt(
                question.strip(),
                truncate_tail(conversation, cfg.max_context),
                user_email,
                cfg.workdir,
                cfg.repos_dir,
                int(cfg.claude_timeout),
            )
            logger.debug(
                "run start: thread=%s cmd=%s prompt_chars=%d cwd=%s",
                thread_id,
                [*cfg.claude_cmd, *cfg.claude_args],
                len(prompt),
                cfg.workdir,
            )
            try:
                stdout, stderr, returncode = run_claude(cfg, prompt)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "timeout: thread=%s took=%.1fs",
                    thread_id,
                    time.monotonic() - start,
                )
                self._send_json(504, {"error": "agent timed out"})
                return
            except OSError as exc:
                logger.exception("agent failed to start: thread=%s", thread_id)
                self._send_json(502, {"error": f"agent failed to start: {exc}"})
                return
            logger.debug(
                "run done: thread=%s exit=%s stdout_chars=%d stderr_chars=%d",
                thread_id,
                returncode,
                len(stdout or ""),
                len(stderr or ""),
            )
            answer, needs_human, confidence = parse_agent_output(stdout, cfg.max_answer)
            elapsed = time.monotonic() - start
            if answer is None and not needs_human:
                logger.warning(
                    "no answer: thread=%s exit=%s took=%.1fs stderr=%.300s",
                    thread_id,
                    returncode,
                    elapsed,
                    (stderr or "").strip(),
                )
                logger.debug("no answer stdout=%.1000s", (stdout or "").strip())
                self._send_json(502, {"error": "agent returned no answer"})
                return
            logger.info(
                "answered: thread=%s exit=%s took=%.1fs chars=%d "
                "needs_human=%s confidence=%s qlen=%d clen=%d",
                thread_id,
                returncode,
                elapsed,
                len(answer or ""),
                needs_human,
                confidence,
                len(question),
                len(conversation),
            )
            self._send_json(
                200,
                {
                    "answer": answer or "",
                    "needs_human": needs_human,
                    "confidence": confidence,
                },
            )
        finally:
            self.slots.release()


def main() -> int:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not cfg.api_key:
        print(
            "AGENT_API_KEY is required (fail closed). Generate one, e.g.: "
            "openssl rand -hex 32",
            file=sys.stderr,
        )
        return 2
    if not os.path.isdir(cfg.workdir):
        print(f"workdir not found: {cfg.workdir}", file=sys.stderr)
        return 2
    Handler.config = cfg
    Handler.slots = threading.BoundedSemaphore(cfg.max_concurrent)
    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    logger.info(
        "listening on %s:%d workdir=%s max_concurrent=%d claude_timeout=%.0fs log_level=%s",
        cfg.bind,
        cfg.port,
        cfg.workdir,
        cfg.max_concurrent,
        cfg.claude_timeout,
        cfg.log_level,
    )
    logger.debug(
        "config: repos_dir=%s claude_cmd=%s claude_args=%s max_body=%d max_context=%d "
        "max_answer=%d api_key_set=%s",
        cfg.repos_dir,
        cfg.claude_cmd,
        cfg.claude_args,
        cfg.max_body,
        cfg.max_context,
        cfg.max_answer,
        bool(cfg.api_key),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
