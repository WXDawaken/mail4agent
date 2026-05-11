## Goal

Clarify how `mailbox` and `oncall` should separate as the system grows from one-shot claimed-delivery runs into reusable thread-sticky supervision.

## Short Answer

Yes: `mailbox` and `oncall` should be separable.

The recommended direction is:

- `mailbox server` remains the system of record for identity, routing, messages, threads, deliveries, leases, and claim serialization
- `oncall server` becomes a separate supervision layer that watches mailbox routes, binds mailbox threads to agent workers, and manages retries, timeouts, and handoff logs
- `app server` or Codex runtime remains an execution backend, not the primary source of truth for thread ownership

The near-term recommendation is not "split repos now", but "split responsibilities now, keep process boundaries optional".

## Layer Boundaries

### Mailbox Server

Owns:

- auth and session lifecycle
- address resolution and routing
- message, thread, and delivery storage
- `claim / ack / nack / heartbeat`
- serialization rules such as `mailbox_thread`

Should not own:

- Codex launch policy
- role prompt selection
- long-running agent supervision
- run-log retention beyond mailbox-native facts

### Oncall Server

Owns:

- route watching
- mailbox-thread to worker assignment
- worker lifecycle
- idle timeout and crash recovery
- handoff/run metadata
- retry policy beyond basic `nack`

Should not own:

- canonical message storage
- canonical thread visibility rules
- direct database truth for delivery state

### App Server / Agent Runtime

Owns:

- one concrete agent execution environment
- temporary working memory for an active run
- tool/runtime configuration for that role

Should not own:

- long-term truth for mailbox thread ownership
- durable delivery state
- the only copy of handoff or audit information

## Why Separation Is Worth It

### Better Fault Isolation

If the oncall layer crashes, mailbox delivery state still exists.
If the mailbox server restarts, oncall can re-login and resume from durable delivery facts.

### Easier Runtime Swaps

The same mailbox can later drive:

- Codex CLI workers
- Codex app-server-managed agents
- human operator fallback
- another automation runtime

without changing message or delivery semantics.

### Cleaner Product Scope

Mailbox stays a communication substrate.
Oncall becomes a workflow/supervision product.

That keeps future feature discussions clearer:

- thread visibility, addressing, auth, retry queue, summaries -> mailbox
- sticky workers, leases, crash recovery, role routing, agent pooling -> oncall

## Recommended Evolution

### Phase 1: Logical Separation

Keep one repo, but make boundaries explicit:

- mailbox-facing primitives stay under the current mailbox modules
- worker/supervision logic stays in `mailbox_worker.py` and `mailbox_oncall.py`
- launcher logic remains role-aware but execution-backend-specific

This is the current target state.

### Phase 2: Process Separation

Run two local processes:

- `sqlite_mailbox_http.py`
- `mailbox_oncall_server.py` or equivalent watcher entrypoint

The oncall process polls mailbox routes and launches workers, but does not embed mailbox storage.

### Phase 3: Sticky Thread Agents

Add a durable registry so one mailbox thread can prefer one active agent instance.

Suggested durable fields:

- `mailbox_id`
- `thread_id`
- `agent_id`
- `consumer_id`
- `status`
- `lease_until`
- `last_seen_at`
- `metadata_json`

Mailbox remains the durable truth.
Agent/session bindings are recoverable metadata, not the primary thread record.

## Concurrency Recommendation

The preferred scope for serialization is:

- serialize within one `mailbox + thread`
- allow concurrency across different threads in the same mailbox
- allow concurrency across different mailboxes even if they share a thread id semantically

This matches the current `mailbox_thread` claim model and avoids turning mailbox into a global workflow lock manager.

## Memory and Persistence Guidance

Do not make long-lived agent private memory the only source of truth.

Recommended durable state:

- mailbox thread itself
- delivery lease state
- last processed message id
- bounded handoff summary
- thread-to-agent binding metadata

Not recommended as first-class durable truth:

- hidden agent-only chain-of-thought state
- large private scratchpads as workflow truth
- app-server-only sticky state with no mailbox mirror

## Codex App Server Fit

Codex app server is a good fit as the execution backend for dedicated oncall agents.

Recommended use:

- oncall server asks app server to create or resume a worker for one mailbox-thread binding
- mailbox/oncall still keep the durable lease and routing facts
- if the app server loses the worker, oncall recreates it from mailbox-visible state plus bounded handoff metadata

So the app server should be treated as:

- execution layer

not as:

- the only workflow state layer

## Suggested Next Step

Before implementing sticky per-thread agents, first refactor the current oncall code into clearer boundaries:

- mailbox-facing API client and claim loop
- oncall supervision registry
- execution backend adapter

That keeps future app-server integration incremental instead of forcing a full redesign.
