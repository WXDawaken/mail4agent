# Dogfood Medium Smoke

## Goal

- Run the canonical Python mailbox repo as a real local communication surface for two medium-effort Codex agents.
- Keep the first smoke bounded: bootstrap a local harness, send one operator task, let one planner and one reviewer process at most one delivery each, then inspect thread and retry state.

## Baseline

- Repo: `E:\agent_misc\mail4agent`
- Branch: `codex/mail4agent-dogfood-wave1`
- Language policy: Python-first, Rust-selective
- Agent config target: `gpt-5.4` with `model_reasoning_effort = medium`

## One-Time Setup Per Server Run

Start the local server with a static admin token:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\sqlite_mailbox_http.py --db .\.tmp_dogfood\mailbox.sqlite --host 127.0.0.1 --port 8787
```

In another shell, bootstrap the dogfood harness and runtime files:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\dogfood_smoke_bootstrap.py
```

That writes runtime-only assets under `.tmp_dogfood\`:

- `harness.token`
- `operator.mailbox_client.json`
- `planner.mailbox_client.json`
- `reviewer.mailbox_client.json`
- `operator.preview.json`
- `planner.preview.json`
- `reviewer.preview.json`
- `bootstrap_summary.json`

## Operator Flow

Use the admin token directly for mailbox operator actions:

```powershell
$env:MAILBOX_BASE_URL = "http://127.0.0.1:8787"
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\client.py whoami
```

Kick off one planner task:

```powershell
python .\client.py send `
  --admin-token $env:MAILBOX_ADMIN_TOKEN `
  --from-address operator@mail4agent.dogfood `
  --to-address planner@mail4agent.dogfood `
  --payload-json '{"task":"triage","request":"Summarize the inbox item and ask reviewer for a short verdict."}'
```

Inspect operator views:

```powershell
python .\client.py thread-summaries --admin-token $env:MAILBOX_ADMIN_TOKEN --to-address planner@mail4agent.dogfood --limit 10
python .\client.py retry-queue --admin-token $env:MAILBOX_ADMIN_TOKEN --project-id mail4agent --limit 10
```

## Launching Medium Codex Agents

Planner:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 planner
```

Reviewer:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 reviewer
```

Each launcher:

- reads the fresh harness token from `.tmp_dogfood\harness.token`
- exports `MAILBOX_HARNESS_ID` and `MAILBOX_PROJECT_ID` from `.tmp_dogfood\bootstrap_summary.json`
- points `MAILBOX_CONFIG` at the role-specific JSON profile, including default `from_address` and `inbox_address`
- performs an explicit `client.py login --output token --project-id mail4agent --role <planner|reviewer> --session dogfood --agent-name dogfood-<role>` first and then switches to `MAILBOX_SESSION_TOKEN`
- sets `CODEX_HOME` to repo-local `.codex_home_dogfood` and bootstraps the minimal auth/config files from the global Codex home
- runs `codex exec` with `gpt-5.4` and `model_reasoning_effort = medium`
- processes at most one mailbox delivery and exits

The same bootstrap can now also launch a bounded code-editing operator:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 operator
```

That operator path uses `gpt-5.4` with `model_reasoning_effort = high` and is documented separately in [dogfood-operator-update-flow-20260324.md](/E:/agent_misc/mail4agent/docs/dogfood-operator-update-flow-20260324.md).

## Suggested Smoke Sequence

1. Start the server.
2. Run `dogfood_smoke_bootstrap.py`.
3. Send one operator message to `planner@mail4agent.dogfood`.
4. Launch the planner agent once.
5. If the planner sends a reviewer handoff, launch the reviewer agent once.
6. If the reviewer reply is addressed back to the planner mailbox, optionally launch the planner once more to close the loop on a revision.
7. Re-run operator `thread-summaries`, `thread`, and `retry-queue` commands to inspect the result.

## What This Smoke Covers

- Static admin-token operator access on normal mailbox routes
- Harness-token login into planner/reviewer agent sessions
- Planner and reviewer mailbox claim/reply/ack flow through the real CLI
- Thread-summary unread tracking
- Retry-queue visibility
- Real multi-hop planner -> reviewer -> planner handoff behavior on the same thread

## Out Of Scope

- Admin page UI
- Long-running autonomous Codex daemons
- Rust-only deployment
- Multi-hop orchestration beyond one planner handoff and one reviewer reply
