# Mailbox Language IR-First Plan

Date: `2026-03-28`

## Goal

Implement the runtime core of [`mailbox_language_spec_v0_2.md`](E:\agent_misc\mail4agent\docs\mailbox_language_spec_v0_2.md) without coupling the mailbox server to the textual DSL.

## Direction

- Keep the mailbox server focused on transport, storage, routing, auth, claim/ack, and typed runtime validation.
- Treat the textual mailbox language as a separate interpreter/compiler layer.
- Lower DSL programs into a typed IR close to the spec's `MessageEnvelope` and `HandoffEvent`.
- Make the first interpreter usable over native stdio streams so it can act as a pipe-friendly tool in local agent workflows.

## Why IR-First Fits This Repo

The current repo already has strong primitives for:

- durable messages and deliveries
- mailbox-visible thread history
- claim/ack/nack supervision
- mailbox-native handoff
- app-server and oncall execution backends

The main missing pieces are not transport. They are:

- protocol registry and mailbox protocol binding
- per-thread protocol/version/state persistence
- typed validation for payloads and transitions
- protocol-aware spawn and handoff relations
- a frontend for parsing and lowering DSL syntax

That makes IR-first the shortest path to a working `mailbox_language_spec` MVP.

## Target Layering

### 1. Mailbox server

Owns:

- mailbox transport and auth
- durable storage
- protocol registry lookup
- mailbox `accepts` / `default` validation
- typed send/spawn/handoff execution
- thread protocol/state transitions
- runtime error codes

Does not own:

- textual DSL parsing
- source-level type-checking UX
- language-specific syntax sugar

### 2. Language interpreter

Owns:

- parsing DSL source
- declaration checking
- symbol resolution
- lowering to IR
- stdio request/response streaming for local tools

Does not own:

- message durability
- delivery queues
- thread lifecycle truth

### 3. Existing clients and oncall

Continue to call the mailbox server directly for generic mailbox operations.

Later they can optionally call the interpreter when they want:

- `send text`
- typed `send to mailbox using P.Msg`
- typed `send to thread using Msg`
- `spawn`
- `handoff`

## Proposed Runtime Workstreams

## Phase 1: Runtime Schema and IR MVP

Goal: support typed runtime execution without any textual DSL.

Checklist:

- Add protocol-definition persistence or loadable registry artifacts.
- Add mailbox protocol bindings:
  - `accepts`
  - optional `default`
- Add thread metadata persistence:
  - `protocol_name`
  - `protocol_version`
  - `state`
  - optional `parent_thread_id`
- Add explicit handoff-relation persistence between threads.
- Add typed runtime entrypoints for:
  - `send envelope -> new thread`
  - `send envelope -> existing thread`
  - `spawn envelope`
  - `handoff event`
- Add runtime error codes aligned with the spec.
- Keep legacy `/send` and generic `message_type` behavior working during migration.

Notes:

- This is the highest-value first slice.
- It reuses almost all of the current mailbox server infrastructure.

## Phase 2: Typed HTTP/CLI Surface

Goal: make the IR callable without requiring the future DSL interpreter.

Checklist:

- Add typed HTTP routes or a typed mode on top of existing routes.
- Add client helpers for typed envelopes and handoff events.
- Add admin/session visibility for protocol/state in thread inspection.
- Add tests that cover:
  - mailbox protocol rejection
  - thread protocol mismatch
  - invalid state transition
  - invalid payload schema
  - protocol-less ingress default behavior

Notes:

- This phase gives us a stable programmatic API.
- It also acts as the contract for the future stdio interpreter.

## Phase 3: Stdio Interpreter MVP

Goal: provide a separate tool that reads mailbox language input and emits or executes IR.

Checklist:

- Add a standalone interpreter entrypoint with native stdio support.
- Support at least one machine-friendly stdio mode:
  - JSON lines request/response
  - or simple framed stdin/stdout protocol
- Support two execution modes:
  - `check`: parse and validate only
  - `run`: lower to IR and call the mailbox server
- Start with a minimal frontend:
  - `protocol`
  - `mailbox`
  - `send`
  - `send text`
  - `spawn`
  - `handoff`
- Emit structured diagnostics rather than Python tracebacks.

Notes:

- This interpreter should be replaceable and repo-local.
- The mailbox server should not need to know whether a typed request came from DSL source, JSON IR, or some future UI.

## Phase 4: Full Checker and Source-Level UX

Goal: reach the spec's declaration and static-semantics expectations.

Checklist:

- Add AST and symbol tables.
- Add declaration checks for protocol/mailbox definitions.
- Add source-level type checking for payload fields and thread handle types.
- Add better spans and diagnostics.
- Add lowering tests that compare source snippets to canonical IR.

## Difficulty Assessment

- Transport and delivery reuse: low
- Runtime typed execution on top of the existing mailbox server: medium
- Schema/state/protocol persistence and migration: medium-high
- Full DSL parser/checker/lowering pipeline: high
- Native stdio interpreter shell and piping support: low-medium

Overall:

- IR-first MVP: medium-high
- Full spec including polished textual DSL frontend: high

## Suggested Implementation Order

1. Add protocol/mailbox/thread/handoff persistence and runtime validation in the server.
2. Add a typed IR API and client helpers.
3. Add a standalone stdio interpreter that targets that IR API.
4. Add the richer checker, shorthand sugar, and better diagnostics.

## First Concrete Deliverables

- `mailbox_protocol_registry.py` or equivalent registry module
- thread metadata and handoff schema migration in `sqlite_mailbox.py`
- typed runtime methods in `sqlite_mailbox.py` and `sqlite_mailbox_http.py`
- typed client helpers in `codex_mailbox_client.py`
- standalone stdio interpreter command
- focused tests for protocol/state/runtime errors

## Non-Goals For The First Slice

- making the mailbox server parse DSL text directly
- replacing existing generic mailbox send/reply/handoff flows all at once
- moving workflow truth into app-server or the interpreter
- introducing long-lived protocol state outside mailbox durability
