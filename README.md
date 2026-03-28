# mail4agent

A tiny SQLite-backed mailbox service for local agent/harness messaging, plus a standard-library HTTP API, admin UI, and stdio CLI.

## What It Does

- Stores harnesses, projects, mailboxes, messages, deliveries, and routing in SQLite
- Exposes HTTP endpoints for `send`, `claim`, `ack`, `nack`, `heartbeat`, `retry-queue`, `resolve`, `message`, `thread`, `inbox`, `thread-summaries`, and `mark-thread-read`
- Protects admin routes with either an env admin token or admin username/password
- Lets the configured admin token call the normal mailbox routes directly for operator workflows
- Issues harness tokens and in-memory agent session tokens
- Includes a browser admin page and a `client.py` CLI for login/send/handoff/claim/retry-queue/thread/inbox/thread-summaries/mark-thread-read/reply/consume plus the first typed-runtime protocol/envelope helpers
- Includes repo-local dogfood helpers for medium planner/reviewer runs, a high-effort operator run, and a minimal operator oncall supervisor/server path
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
- `GET /whoami` for agent sessions also exposes `default_inbox_address`, which `inbox`, `thread-summaries`, and `mark-thread-read` can use when `--to-address` is omitted

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

List recent visible messages for the current session inbox:

```powershell
python .\client.py inbox --limit 10
```

Bash equivalent:

```bash
python3 ./client.py inbox --limit 10
```

Filter that inbox view by message type when needed:

```powershell
python .\client.py inbox --limit 10 --message-type codex.task
```

Filter that inbox view by sender when needed:

```powershell
python .\client.py inbox --limit 10 --from-address operator@mail4agent.dogfood
```

Filter that inbox view to messages created at or after a specific ISO timestamp:

```powershell
python .\client.py inbox --limit 10 --since 2026-03-24T16:02:19.605Z
```

Show only messages that are still unread for the current mailbox session:

```powershell
python .\client.py inbox --limit 10 --unread-only
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

Forward one visible message to another mailbox as a mailbox-native handoff while keeping the same `thread_id`:

```powershell
python .\client.py handoff --message-id <MESSAGE_ID> --to-address integrator@consumer_app.dogfood --message-type integration_handoff --summary "Please review the supplier reply."
```

The handoff message carries a `kind = mailbox_handoff` payload with source message refs and a source payload snapshot. This is useful when the target mailbox should continue the same coordination thread but cannot directly read the original mailbox's full thread history.

Run an operator mailbox command directly with the admin token:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
python .\client.py whoami
python .\client.py send --admin-token $env:MAILBOX_ADMIN_TOKEN --from-address operator@mail4agent.codex --to-address shadow@mail4agent.ops --payload-json '{"task":"admin-direct"}'
```

Register a typed protocol and execute a typed runtime envelope without hand-writing the raw envelope JSON:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
python .\client.py register-protocol --protocol Orders/v2 --schema-file .\orders.protocol.json
python .\client.py set-mailbox-protocols --address reviewer@mail4agent.codex --accepts Orders/v2
python .\client.py typed-send --to-address reviewer@mail4agent.codex --from-address operator@mail4agent.codex --protocol Orders/v2 --message QuoteReq --payload-json '{"order_id":"123","items":["sku-1"]}'
```

The typed commands currently target the admin-backed IR/runtime surface: `register-protocol`, `list-protocols`, `set-mailbox-protocols`, `get-mailbox-protocols`, `typed-send`, `typed-spawn`, and `typed-handoff`. When both `MAILBOX_SESSION_TOKEN` and `MAILBOX_ADMIN_TOKEN` are present in the environment, these typed commands automatically prefer the admin token unless you pass an explicit `--token` or `--admin-token`.

For pipe-friendly local tooling, the repo now also includes a native-stdio mailbox-language interpreter shell in `mailbox_language_stdio.py`. It currently speaks JSON lines and stays intentionally DSL-agnostic: each input line is one `{command, artifact}` request, and each output line is one structured JSON result or error.

Check and lower a protocol schema with an optional local compile cache:

```powershell
@'
{"id":"orders-check","command":"check","cache_dir":".\\.tmp_lang_cache","artifact":{"kind":"protocol_schema","protocol":"Orders/v2","schema":{"states":["Init","Done"],"start":"Init","messages":{"QuoteReq":{"required":["order_id"],"allow_additional_fields":false}},"transitions":[{"message":"QuoteReq","from":"Init","to":"Done"}]}}}
{"id":"orders-lower","command":"lower","cache_dir":".\\.tmp_lang_cache","artifact":{"kind":"protocol_schema","protocol":"Orders/v2","schema":{"states":["Init","Done"],"start":"Init","messages":{"QuoteReq":{"required":["order_id"],"allow_additional_fields":false}},"transitions":[{"message":"QuoteReq","from":"Init","to":"Done"}]}}}
'@ | python .\mailbox_language_stdio.py
```

Run a typed mailbox-binding or envelope request through the existing admin-backed IR runtime:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
@'
{"id":"bind-reviewer","command":"run","artifact":{"kind":"mailbox_binding","address":"reviewer@mail4agent.codex","accepts":["Orders/v2"]}}
{"id":"send-quote","command":"run","artifact":{"kind":"message_envelope","op":"send","from_address":"operator@mail4agent.codex","to_address":"reviewer@mail4agent.codex","protocol":"Orders/v2","message":"QuoteReq","payload":{"order_id":"123","items":["sku-1"]}}}
'@ | python .\mailbox_language_stdio.py --base-url http://127.0.0.1:8787
```

`mailbox_language_stdio.py` currently supports `check`, `lower`, and `run` for four machine-friendly artifact kinds: `protocol_schema`, `mailbox_binding`, `message_envelope`, and `handoff_event`. It emits structured diagnostics instead of Python tracebacks, and `run` reuses the same typed admin routes and client helpers as `client.py`.

Reply and ack:

```powershell
python .\client.py claim |
python .\client.py reply --payload-json '{"ok":true,"reply":"done"}' --ack-after
```

Run the generic consume worker once:

```powershell
python .\client.py consume --once -- python -c "import json,sys; print(json.load(sys.stdin)['message_id'])"
```

Run the minimal operator oncall supervisor once against the repo-local dogfood runtime:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood
```

Use `--watch` if you want the supervisor to keep polling for more work instead of exiting after one attempt.
Oncall and `consume` now default to mailbox-thread serialization: multiple supervisors will not claim the same delivery, and they also will not simultaneously process different active deliveries from the same thread within one mailbox route. Different threads can still run concurrently. Use distinct `--consumer-id` values for observability if you intentionally run more than one supervisor on the same mailbox, and set `--serialization-scope delivery` only if you explicitly want the older delivery-only behavior.

Inspect the persisted oncall role registry plus current thread bindings for one runtime:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood --inspect-registry
```

Run the watch-first oncall server entrypoint and let it stop after 10 idle minutes:

```powershell
python .\mailbox_oncall_server.py --role operator --runtime-dir .tmp_dogfood --idle-exit-after-seconds 600
```

PowerShell launcher equivalent:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_oncall_server.ps1 -Role operator -RuntimeDir .tmp_dogfood -IdleExitAfterSeconds 600
```

Experimental app-server backend:

```powershell
python .\mailbox_oncall_server.py --role operator --runtime-dir .tmp_dogfood --backend app-server --idle-exit-after-seconds 600
```

PowerShell launcher equivalent:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_oncall_server.ps1 -Role operator -RuntimeDir .tmp_dogfood -Backend app-server -IdleExitAfterSeconds 600
```

If you want the child Codex run isolated from the main checkout, point the oncall path at a temporary workspace plus a temporary `CODEX_HOME`:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_oncall_server.ps1 -Role operator -RuntimeDir C:\temp\mail4agent_runtime -IdleExitAfterSeconds 60 -WorkspaceDir C:\temp\mail4agent_workspace -CodexHomeDir C:\temp\mail4agent_codex_home
```

The direct Python entrypoints expose the same controls as `--codex-workspace-dir` and `--codex-home-dir`. When those are set, the child prompt follows the current workspace instead of assuming the canonical repo root, so temporary smoke work and cleanup stay isolated.

`--backend app-server` is now available as an experimental alternative. It starts `codex app-server --listen stdio://`, records the returned thread and turn ids in the oncall summary metadata, and keeps mailbox plus registry state as the durable source of truth. Within one long-lived oncall process, the app-server adapter now reports `supports_worker_reuse = true`, probes whether an existing thread binding is still live, and reuses the same app-server thread for follow-up deliveries on the same mailbox thread when that probe succeeds.

That reuse is still process-local and recoverable rather than durable app-server state: if the watcher restarts, the supervisor will fall back to a fresh worker when the previous `worker_id` is no longer live, record `recovery_reason = previous_worker_not_available`, and continue with mailbox plus registry files as the source of truth.

When a fresh worker is created for an existing mailbox thread, the app-server backend now also injects bounded recovery context from the thread registry into the new prompt: previous `worker_id`, previous `last_processed_message_id`, `recovery_reason`, and the bounded `handoff_summary` from the last completed run. That keeps cold recovery mailbox-driven and inspectable rather than depending on app-server-only hidden state.

The app-server backend can now also resolve a per-delivery `workspace_dir` within the configured oncall workspace root. In practice this means one long-lived watcher can keep separate live workers for the same mailbox thread across different child workspaces, while still using the repo-root mailbox CLI and prompt assets from the main oncall checkout.

Thread registry files now keep those workspace-local bindings under `bindings_by_workspace`, keyed by normalized absolute workspace path. Follow-up deliveries on the same mailbox thread will reuse the existing app-server worker only when both the mailbox thread and resolved workspace match a live binding; switching to a different child workspace starts a different worker without overwriting the older binding.

Reusable backends now also accept worker-lifecycle controls:

```powershell
python .\mailbox_oncall_server.py --role operator --runtime-dir .tmp_dogfood --backend app-server --worker-idle-timeout-seconds 600 --worker-max-age-seconds 3600
```

PowerShell launcher equivalent:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_oncall_server.ps1 -Role operator -RuntimeDir .tmp_dogfood -Backend app-server -WorkerIdleTimeoutSeconds 600 -WorkerMaxAgeSeconds 3600
```

`--worker-idle-timeout-seconds` closes an unused sticky worker after that many idle seconds, while `--worker-max-age-seconds` retires a worker after that total lifetime even if it is still being reused. Both default to `900` and `3600` respectively for reusable backends, and passing `0` disables that limit.

The current `codex-cli` backend still launches one fresh worker per claimed delivery, while thread registry files now capture `worker_id`, reuse support, recovery decisions, bounded handoff summaries, and app-server turn metadata so reusable backends stay inspectable.

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

For a watch-first operator mailbox worker with optional idle exit:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_oncall_server.ps1 -Role operator -RuntimeDir .tmp_dogfood -IdleExitAfterSeconds 600
```

## Notes

- `mailbox.sqlite` and other SQLite database files are intentionally ignored by git
- CLI output defaults to JSON; pass `--format text` for a more readable terminal view
- `client.py inbox`, `client.py thread-summaries`, and `client.py mark-thread-read --thread-id <THREAD_ID>` now use the logged-in session's `default_inbox_address` when available; each command still accepts an explicit `--to-address <MAILBOX>` override
- `client.py retry-queue` exposes retry-pending deliveries with attempt counts, next retry time, and a short last-error summary
- `client.py login --output token` always prints only the token, so it works well with env assignment and redirection
- After a server restart, in-memory agent session tokens are invalid; run `client.py login` again to get a fresh session token
- The first typed-runtime CLI surface is intentionally IR-first and still admin-backed; `mailbox_language_stdio.py` now provides the first native-stdio interpreter shell on top of that same contract, and the next step is source DSL parsing and lowering rather than changing the mailbox server
