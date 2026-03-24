# mail4agent

A tiny SQLite-backed mailbox service for local agent/harness messaging, plus a standard-library HTTP API, admin UI, and stdio CLI.

## What It Does

- Stores harnesses, projects, mailboxes, messages, deliveries, and routing in SQLite
- Exposes HTTP endpoints for `send`, `claim`, `ack`, `nack`, `heartbeat`, `retry-queue`, `resolve`, `message`, `thread`, `thread-summaries`, and `mark-thread-read`
- Protects admin routes with either an env admin token or admin username/password
- Lets the configured admin token call the normal mailbox routes directly for operator workflows
- Issues harness tokens and in-memory agent session tokens
- Includes a browser admin page and a `client.py` CLI for login/send/claim/retry-queue/thread/thread-summaries/mark-thread-read/reply/consume
- Includes repo-local dogfood helpers for medium planner/reviewer runs and a high-effort operator run
- Stays dependency-free: Python standard library only

## Main Files

- `sqlite_mailbox.py`: SQLite storage layer
- `sqlite_mailbox_http.py`: HTTP server and admin/auth layer
- `sqlite_mailbox_admin_ui.html`: lightweight admin UI
- `client.py`: stdio-style CLI
- `codex_mailbox_client.py`: Python HTTP client
- `codex_mailbox_adapter.py`: handler-oriented adapter
- `codex_mailbox_demo_agent.py`: demo worker
- `codex_mailbox_demo_send.py`: demo sender

## Requirements

- Python 3.11+
- No third-party packages

## Quick Start

Start the server:

```powershell
python .\sqlite_mailbox_http.py --db .\mailbox.sqlite --host 127.0.0.1 --port 8787
```

Optional: use a static admin bearer token instead of relying only on the bootstrap/admin-account flow:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
python .\sqlite_mailbox_http.py --db .\mailbox.sqlite --host 127.0.0.1 --port 8787
```

On first start, if no admin account exists, the server prints a one-time loopback-only setup URL like:

```text
http://127.0.0.1:8787/setup-admin?token=...
```

Open that URL on the same machine, create an admin username/password, then open:

```text
http://127.0.0.1:8787/admin-ui
```

Use the admin UI to:

- create a harness
- create a project
- create one or more mailboxes
- create a harness token
- preview an agent login config

## Bash Notes

The project has been exercised mainly from PowerShell so far. The Bash snippets below are expected usage patterns, but they have not been end-to-end smoke-tested yet.

Start the server from a Bash-compatible shell:

```bash
python3 ./sqlite_mailbox_http.py --db ./mailbox.sqlite --host 127.0.0.1 --port 8787
```

Or with a static admin token:

```bash
export MAILBOX_ADMIN_TOKEN="dev-admin-token"
python3 ./sqlite_mailbox_http.py --db ./mailbox.sqlite --host 127.0.0.1 --port 8787
```

## Auth Model

- `MAILBOX_ADMIN_TOKEN` protects `/admin/*` and can also drive the ordinary mailbox routes directly
- Harness tokens are long-lived and stored hashed in SQLite
- `POST /login` exchanges a harness token for an agent session token
- `POST /logout` invalidates the current agent session token immediately
- Agent session tokens are stored in server memory only
- Repeating the same login identity reuses the same in-memory session token until it expires
- Restarting the server invalidates all agent session tokens
- `GET /whoami` exposes session expiry metadata such as `expires_at` and `expires_in_seconds`
- `GET /whoami` for agent sessions also exposes `default_inbox_address`, which thread-state commands can use when `--to-address` is omitted

## CLI Usage

Login and export a session token:

```powershell
$env:MAILBOX_BASE_URL = "http://127.0.0.1:8787"
$env:MAILBOX_SESSION_TOKEN = (
  python .\client.py login `
    --output token `
    --project-id mail4agent `
    --roles planner,reviewer `
    --session main
)
```

Inspect the current session and explicitly log it out:

```powershell
python .\client.py whoami
python .\client.py logout
```

Bash equivalent:

```bash
export MAILBOX_BASE_URL="http://127.0.0.1:8787"
export MAILBOX_TOKEN="<HARNESS_TOKEN>"
export MAILBOX_SESSION_TOKEN="$(
  python3 ./client.py login \
    --output token \
    --project-id mail4agent \
    --roles planner,reviewer \
    --session main
)"
```

Environment variable summary:

- `MAILBOX_BASE_URL`: mailbox server base URL, defaults to `http://127.0.0.1:8787`
- `MAILBOX_ADMIN_TOKEN`: optional static admin bearer token for `/admin/*` and direct operator mailbox commands
- `MAILBOX_TOKEN`: harness token, mainly used for `client.py login`
- `MAILBOX_SESSION_TOKEN`: agent session token, preferred for normal mailbox commands after login
- `MAILBOX_CONFIG`: optional path to `mailbox_client.json`
- `MAILBOX_TIMEOUT_SECONDS`: optional HTTP timeout override

Typical Bash flow:

```bash
export MAILBOX_BASE_URL="http://127.0.0.1:8787"
export MAILBOX_TOKEN="<HARNESS_TOKEN>"
export MAILBOX_SESSION_TOKEN="$(python3 ./client.py login --output token --project-id mail4agent --roles planner --session main)"
unset MAILBOX_TOKEN
```

That last `unset` is optional, but it keeps the runtime environment focused on the session token once login is done.

Send a message:

```powershell
'{"task_type":"echo","text":"hello"}' | python .\client.py send --to-address reviewer@mail4agent.codex
```

Bash equivalent:

```bash
printf '%s\n' '{"task_type":"echo","text":"hello"}' | python3 ./client.py send --to-address reviewer@mail4agent.codex
```

Claim one delivery:

```powershell
python .\client.py claim
```

Bash equivalent:

```bash
python3 ./client.py claim
```

Read a thread in terminal-friendly text format:

```powershell
python .\client.py --format text thread --message-id <MESSAGE_ID>
```

Bash equivalent:

```bash
python3 ./client.py --format text thread --message-id <MESSAGE_ID>
```

List thread summaries for the current session inbox:

```powershell
python .\client.py thread-summaries --limit 10
```

Bash equivalent:

```bash
python3 ./client.py thread-summaries --limit 10
```

If you want to target a different mailbox explicitly, `--to-address` still works:

```powershell
python .\client.py thread-summaries --to-address reviewer@mail4agent.codex --limit 10
```

Mark a thread read for the current session inbox:

```powershell
python .\client.py mark-thread-read --thread-id <THREAD_ID>
```

Bash equivalent:

```bash
python3 ./client.py mark-thread-read --thread-id <THREAD_ID>
```

And the explicit mailbox override still works when needed:

```powershell
python .\client.py mark-thread-read --thread-id <THREAD_ID> --to-address reviewer@mail4agent.codex
```

Inspect retry-pending deliveries for the current harness or session scope:

```powershell
python .\client.py retry-queue --project-id mail4agent --limit 10
```

Bash equivalent:

```bash
python3 ./client.py retry-queue --project-id mail4agent --limit 10
```

Run an operator mailbox command directly with the admin token:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
python .\client.py whoami
python .\client.py send --admin-token $env:MAILBOX_ADMIN_TOKEN --from-address operator@mail4agent.codex --to-address shadow@mail4agent.ops --payload-json '{"task":"admin-direct"}'
```

Reply and ack:

```powershell
python .\client.py claim |
python .\client.py reply --payload-json '{"ok":true,"reply":"done"}' --ack-after
```

Bash equivalent:

```bash
python3 ./client.py claim | python3 ./client.py reply --payload-json '{"ok":true,"reply":"done"}' --ack-after
```

Run a lightweight worker loop:

```powershell
python .\client.py consume -- python .\client.py reply --payload-json '{"ok":true}'
```

Bash equivalent:

```bash
python3 ./client.py consume -- python3 ./client.py reply --payload-json '{"ok":true}'
```

## Demo Agent

Start the demo agent:

```powershell
python .\codex_mailbox_demo_agent.py
```

Send a demo task and wait for a reply:

```powershell
python .\codex_mailbox_demo_send.py --task-type upper_text --text "hello codex" --wait-for-reply
```

## Medium Dogfood Smoke

For a bounded two-agent Codex smoke on top of this mailbox repo:

1. Start the server with a static admin token.
2. Run `python .\dogfood_smoke_bootstrap.py`.
3. Send an operator task to `planner@mail4agent.dogfood`.
4. Launch one medium planner agent:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 planner
```

5. Launch one medium reviewer agent if the planner hands work off:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 reviewer
```

See [docs/dogfood-medium-smoke-20260324.md](E:\agent_misc\mail4agent\docs\dogfood-medium-smoke-20260324.md) for the full runbook.

For bounded mailbox-native update work, launch the operator path:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 operator
```

That operator path logs in with the harness token, targets the `operator@mail4agent.dogfood` group mailbox, and uses `gpt-5.4` with `model_reasoning_effort = high`. See [docs/dogfood-operator-update-flow-20260324.md](E:\agent_misc\mail4agent\docs\dogfood-operator-update-flow-20260324.md).

## Notes

- `mailbox.sqlite` and other SQLite database files are intentionally ignored by git
- CLI output defaults to JSON; pass `--format text` for a more readable terminal view
- `client.py thread-summaries` now uses the logged-in session's `default_inbox_address` when available, and `client.py mark-thread-read --thread-id <THREAD_ID>` uses that same default; both commands still accept explicit `--to-address <MAILBOX>` overrides
- `client.py retry-queue` exposes retry-pending deliveries with attempt counts, next retry time, and a short last-error summary
- `client.py login --output token` always prints only the token, so it works well with env assignment and redirection
- After a server restart, in-memory agent session tokens are invalid; run `client.py login` again to get a fresh session token
