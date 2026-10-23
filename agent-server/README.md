# Live-answer agent server

Runs on the ticketbot box next to `~/freescout-ticket-solver`. It answers **one
live chat user's question per request** by running Claude headless in strictly
read-only research mode, so the in-app chat assistant can relay the answer
immediately instead of escalating by email.

Flow:

```
chat backend (ask_support_specialist tool)
  -- POST /ask {question, conversation, thread_id, user_email} -->
agent-server -- `claude -p` (read-only: Read/Glob/Grep + fs_search/fs_get) -->
  <-- {answer, needs_human, confidence} --
chat backend relays the answer to the live user
```

When the agent sets `needs_human` (sensitive topic or unsure), or the service
is unreachable, the chat backend escalates to a human via its
`forward_to_real_agent` email fallback.

## Files

- `server.py` — the service (stdlib only, no installs needed)
- `test_server.py` — maintained tests (`python3 test_server.py`)
- `agent-server.service` — systemd unit example

## Deploy on the ticketbot box

1. Get the files there (this repo is the source of truth):
   ```bash
   mkdir -p ~/agent-server && cd ~/agent-server
   # copy server.py + agent-server.service from the repo (git pull or scp)
   python3 -m py_compile server.py && python3 test_server.py
   ```
2. Create the env file (never commit it):
   ```bash
   printf 'AGENT_API_KEY=%s\n' "$(openssl rand -hex 32)" > ~/agent-server/agent.env
   chmod 600 ~/agent-server/agent.env
   ```
3. Check the `claude` CLI flags on that box before going live:
   ```bash
   claude --help | grep -E 'print|output-format|max-turns|allowedTools'
   ```
   `server.py` defaults to `claude -p --output-format json --max-turns 12
   --allowedTools Read,Glob,Grep,Bash(bin/fs_search.sh:*),Bash(bin/fs_get.sh:*)`.
   Flag names vary by CLI version — if any are missing, adapt `CLAUDE_ARGS`
   (env, see below) and re-run the smoke test. Never "fix" this with
   `--dangerously-skip-permissions` on a network-reachable service; extend the
   explicit allowlist instead.
4. Install and start the service (run as the same `ticketbot` user the cronjob
   uses, so Claude auth is identical):
   ```bash
   sudo cp agent-server.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now agent-server
   journalctl -u agent-server -f
   ```
5. Restrict network access: `AGENT_BIND` defaults to `127.0.0.1`. If the chat
   backend runs on another machine, reach the box over Tailscale/SSH tunnel, or
   bind `0.0.0.0` with a firewall rule allowing only the backend's IP. The
   bearer key is required either way; every agent run costs time and API money.
6. Smoke test (from wherever the backend will call it):
   ```bash
   curl -s http://127.0.0.1:8765/health
   curl -s -H "Authorization: Bearer $AGENT_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"question":"Where do I see last known positions?","thread_id":"smoke"}' \
     http://127.0.0.1:8765/ask
   ```
7. Set the backend env vars (see table below) wherever the chat backend runs.

## Environment

Service (`agent.env` / environment):

| Var | Default | Meaning |
|---|---|---|
| `AGENT_API_KEY` | (required, fail closed) | Bearer key; must match the backend's `CLAUDE_AGENT_API_KEY` |
| `AGENT_BIND` / `AGENT_PORT` | `127.0.0.1` / `8765` | Listen address |
| `AGENT_WORKDIR` | `~/freescout-ticket-solver` | cwd for runs (so `bin/fs_*.sh` resolve) |
| `AGENT_REPOS_DIR` | `~/repos` | Codebase path given to the agent |
| `CLAUDE_CMD` / `CLAUDE_ARGS` | `claude` / see §3 | Headless CLI invocation (prompt is appended as final arg) |
| `CLAUDE_TIMEOUT` | `100` (s) | Per-run kill deadline; keep below the backend timeout |
| `AGENT_MAX_CONCURRENT` | `2` | Simultaneous runs; excess gets `503 busy` |
| `AGENT_MAX_BODY` / `AGENT_MAX_CONTEXT` / `AGENT_MAX_ANSWER` | `204800` / `60000` / `8000` | Request cap / transcript tail kept / answer cap (chars) |

Chat backend:

| Var | Default | Meaning |
|---|---|---|
| `CLAUDE_AGENT_URL` | (required) | Full service URL incl. `/ask` |
| `CLAUDE_AGENT_API_KEY` | (none) | Must match the service's `AGENT_API_KEY` |
| `CLAUDE_AGENT_TIMEOUT` | `120` (s) | Must exceed the service's `CLAUDE_TIMEOUT` |
| `SUPPORT_CONTACT_EMAIL` | `support@fleetmap.io` | Last-resort human contact, used only if the email fallback itself fails |

## Note: the solver's CLAUDE.md

Headless runs execute with cwd `AGENT_WORKDIR`, so the CLI may also load the
solver's `CLAUDE.md` (cron queue instructions). Three mitigations keep live runs
on task: the per-call prompt starts with an explicit `LIVE-ANSWER MODE` task
that overrides queue work, the tool allowlist physically excludes every write
script (`fs_note.sh`, `code_worktree.sh`, `gh_pr.sh`), and `--max-turns` bounds
wandering. Still, append this section to `~/freescout-ticket-solver/CLAUDE.md`
so both modes are documented in one place:

```markdown
## Live-answer mode (via agent-server)

Besides the cron ticket-queue runs above, you may be invoked headless by
`agent-server` (same box) to answer ONE live chat user's question. You
recognize these runs because the task prompt starts with "LIVE-ANSWER MODE"
and asks a single question instead of queue processing.

In live-answer mode the queue workflow does not apply: do not list tickets,
do not post notes, do not touch `state/processed.json`, do not make code
changes. Only research (codebase, past tickets via `bin/fs_search.sh` /
`bin/fs_get.sh`) and print the single JSON answer object the prompt asks for.
The hard rules at the top still apply — read-only, no invented facts, flag
sensitive topics via `needs_human`.
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 unauthorized` | `CLAUDE_AGENT_API_KEY` (backend) != `AGENT_API_KEY` (service) |
| `503 busy` | 2 runs already in flight; raise `AGENT_MAX_CONCURRENT` or retry |
| `504 agent timed out` | Research exceeded `CLAUDE_TIMEOUT`; raise it (and the backend timeout), or narrow the task via fewer `--max-turns` |
| `502 agent returned no answer` | CLI errored; check `journalctl -u agent-server` stderr tail |
| `502 agent failed to start` | `claude` not on PATH for the service user; set absolute `CLAUDE_CMD` |
| Answers ignore the JSON contract | Older CLI without `--output-format json`; service falls back to raw text (low confidence) — still verify flags per §3 |

Logs (`journalctl -u agent-server`) record one line per request (outcome,
duration, sizes) and never the question text or user email.
