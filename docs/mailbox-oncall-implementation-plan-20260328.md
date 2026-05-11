## Goal

Turn the mailbox/oncall separation note into a concrete first implementation plan.

This plan does not introduce sticky thread agents yet.
It defines the smallest code and state split that makes a future `oncall server` natural instead of forcing another large refactor.

## Current Code Map

### Mailbox-Side Runtime

- [sqlite_mailbox.py](../sqlite_mailbox.py)
  - delivery storage and selection
  - `claim_any(...)`
  - mailbox-thread serialization behavior
- [sqlite_mailbox_http.py](../sqlite_mailbox_http.py)
  - HTTP routes
  - `/claim`, `/ack`, `/nack`, `/heartbeat`
- [codex_mailbox_client.py](../codex_mailbox_client.py)
  - HTTP client wrapper used by CLI and oncall

### Current Worker / Oncall Runtime

- [mailbox_worker.py](../mailbox_worker.py)
  - generic `claim -> heartbeat -> ack/nack` loop
  - subprocess handler execution
- [mailbox_oncall.py](../mailbox_oncall.py)
  - role config
  - claim-address resolution
  - once/watch supervision
  - summary JSON output
- [launch_oncall_agent.ps1](../launch_oncall_agent.ps1)
  - current execution backend adapter for Codex CLI
  - login bootstrap
  - staging claimed delivery into repo-visible runtime files

## Main Observation

The current split is already half-way there:

- `mailbox_worker.py` is the reusable consume primitive
- `mailbox_oncall.py` is the first supervision layer
- `launch_oncall_agent.ps1` is the execution adapter

What is still missing is a clean state boundary.

Right now `mailbox_oncall.py` is still doing all of these at once:

- route/watch configuration
- delivery handling policy
- process launch wiring
- run summary persistence

That makes the next step harder than it needs to be.

## First Concrete Refactor Target

Split the current oncall side into four modules.

### 1. `oncall_registry.py`

Owns:

- last-run summaries
- role-local state directories
- thread-to-worker binding metadata when we add sticky agents later

First version should store:

- `role`
- `consumer_id`
- `runtime_dir`
- `last_run_path`
- `last_delivery_id`
- `last_thread_id`
- `started_at`
- `completed_at`
- `status`

Storage can start as JSON files under:

- `.oncall/registry/`

This keeps the first version simple and inspectable.

### 2. `oncall_supervisor.py`

Owns:

- watch loop
- claim policy
- stop conditions
- delivery result bookkeeping

It should call:

- mailbox client for `claim/ack/nack/heartbeat`
- registry for state persistence
- execution backend adapter for one delivery

This becomes the future home of:

- thread-sticky routing
- idle timeout logic
- worker reuse policy

### 3. `oncall_exec_codex.py`

Owns:

- "run this claimed delivery with Codex"
- login bootstrap
- config staging
- prompt selection
- launcher-specific environment setup

It should hide details currently embedded in:

- [launch_oncall_agent.ps1](../launch_oncall_agent.ps1)

The current PowerShell launcher can remain, but the Python side should treat it as one backend adapter.

### 4. `mailbox_oncall.py`

Should become a thin CLI wrapper only:

- parse args
- build role spec
- build supervisor config
- call `run_oncall_supervisor(...)`

## Recommended State Model

### Now

Persist one bounded run summary per role:

- `.oncall/<role>-last-run.json`

and one registry entry per role:

- `.oncall/registry/<role>.json`

### Next

When sticky agents are introduced, add:

- `.oncall/registry/threads/<mailbox_key>__<thread_id>.json`

with fields like:

- `mailbox_address`
- `thread_id`
- `worker_kind`
- `worker_id`
- `status`
- `lease_until`
- `last_seen_at`
- `last_processed_message_id`
- `handoff_summary`

The important rule is:

- these files are recoverable supervision metadata
- mailbox still remains the source of truth for message and delivery state

## Recommended Execution-Backend Boundary

Define a small execution contract like:

```text
execute_claimed_delivery(role_spec, runtime_dir, delivery) -> exit_code, metadata
```

Inputs:

- role
- runtime config
- claimed delivery JSON

Outputs:

- process exit code
- optional metadata such as:
  - `worker_id`
  - `last_message_path`
  - `staged_delivery_path`
  - `execution_mode`

This is the seam where a future app-server-backed implementation can replace or sit next to the current CLI launcher.

## App Server Integration Plan

Do not make app server the first new state owner.

Instead:

### First app-server phase

- keep mailbox + oncall registry as durable layers
- execution adapter asks app server to run one dedicated worker
- oncall stores the returned `worker_id`
- if the worker disappears, oncall recreates it

### Sticky phase

- oncall registry stores `thread -> worker_id`
- same mailbox-thread prefers the same worker when still healthy
- mailbox delivery state still determines whether there is actual work to process

## First CLI / Operational Surface

### Keep

- `python .\mailbox_oncall.py --role operator --once`
- `python .\mailbox_oncall.py --role operator --watch`

### Add later

- `python .\mailbox_oncall.py --role operator --backend codex-cli`
- `python .\mailbox_oncall.py --role operator --backend app-server`
- `python .\mailbox_oncall.py --inspect-registry`

## Migration Steps

### Step 1

Refactor current code without changing behavior:

- add `oncall_supervisor.py`
- add `oncall_registry.py`
- move current summary-file writes into registry helpers
- keep `launch_oncall_agent.ps1` unchanged

### Step 2

Add execution adapter boundary:

- add `oncall_exec_codex.py`
- make `mailbox_oncall.py` call the adapter instead of directly building handler commands

### Step 3

Add thread-binding metadata without worker reuse:

- persist `last_thread_id`
- persist `last_processed_message_id`
- persist optional bounded handoff summary

This gives observability before it gives stickiness.

### Step 4

Only then evaluate sticky per-thread workers:

- app server or Codex-managed worker ids
- idle timeout
- crash recovery

## Recommendation

The next code change should be **Step 1**, not sticky agents.

That yields immediate benefits:

- clearer oncall server boundary
- easier testing
- easier future app-server backend swap
- no new durability risk yet

It also keeps the current oncall path working while preparing for the more ambitious thread-sticky model.
