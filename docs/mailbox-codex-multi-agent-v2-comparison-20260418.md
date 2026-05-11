# Mail4Agent Compared With Codex Multi-Agent V2

Date: `2026-04-18`

## Goal

Extract the parts of Codex `multi_agent_v2` that are worth borrowing for `mail4agent` without importing Codex's process-local assumptions.

## Short Read

- Codex `multi_agent_v2` validates the current `mail4agent` direction of point-to-point mailbox routing on top of a shared causal `thread_id`.
- The most useful ideas to borrow are:
  - explicit queue-vs-wake delivery semantics
  - stable logical task handles distinct from runtime worker ids
  - waiting on mailbox activity rather than backend exit
  - answer-boundary handling so late child mail does not extend an already-finished reply
- `mail4agent` should keep its existing strengths:
  - durable mailbox truth
  - protocol/state ownership in the mailbox server
  - oncall/backend separation
  - point-to-point thread discipline
- `mail4agent` should not copy Codex's in-memory mailbox as the system of record.

## What Looks Similar Already

- Both systems benefit from treating `thread_id` as a causal chain rather than a group-chat roster.
- Both systems work better when one message targets one recipient at a time.
- Both systems need a clean distinction between "status update" traffic and "do work now" traffic.
- Both systems need bounded completion reporting back to the direct parent or upstream owner instead of uncontrolled broadcast.

This reinforces the direction already recorded in:

- [mailbox-thread-design-guidelines-20260405.md](mailbox-thread-design-guidelines-20260405.md)
- [mailbox-group-round-draft-20260405.md](mailbox-group-round-draft-20260405.md)
- [mailbox-language-ir-first-plan-20260328.md](mailbox-language-ir-first-plan-20260328.md)

## What Codex V2 Gets Right

### 1. It splits "queue only" from "wake the target"

Codex `multi_agent_v2` separates:

- `send_message`: queue mail without triggering a new turn
- `followup_task`: send mail and trigger or redirect the target's next turn

At the protocol level this is represented by `InterAgentCommunication.trigger_turn`.

Why this matters for `mail4agent`:

- It gives the transport layer a first-class wake policy.
- It reduces ambiguity around whether a message is just an update, or an actual task handoff.
- It provides a cleaner foundation for suppressing completion ping-pong and no-op follow-ups.

### 2. It gives agents stable logical names

Codex `multi_agent_v2` leans on canonical task names such as `/root/task1/task_3` instead of only ephemeral runtime ids.

Why this matters for `mail4agent`:

- A logical task handle should survive worker churn better than `worker_id`.
- It gives mailbox-visible coordination a stable reference even when the execution backend changes.
- It fits the existing `bindings_by_workspace` and recovery-summary work better than treating worker ids as the primary identity.

### 3. It waits on mailbox activity, not just child process completion

Codex v2 `wait_agent` wakes on mailbox sequence changes rather than only on child thread termination.

Why this matters for `mail4agent`:

- The real event of interest is often "new collaboration state arrived", not "a backend exited".
- This is a better fit for long-lived watchers, sticky workers, and bounded handoff/review flows.
- It would make planner/operator/integrator workflows less coupled to backend lifecycle details.

### 4. It has an answer boundary

Codex explicitly stops folding mailbox messages into the current turn once visible final output has already been emitted. Late mail stays queued for a later turn.

Why this matters for `mail4agent`:

- It is the runtime version of the same instinct behind the current completion ping-pong mitigation.
- It avoids reopening an already-finished user-visible round just because a late completion notice arrived.
- It gives "terminal notice absorption" a principled state boundary instead of only prompt-level guidance.

### 5. It prefers direct-parent completion routing over broad visibility

Codex v2 forwards child completion envelopes to the direct parent instead of treating ordinary threads as a broadcast surface.

Why this matters for `mail4agent`:

- It confirms the current thread-model choice.
- It argues for explicit handoff, child threads, and bounded summary return paths instead of widening ordinary thread semantics.

## Where Mail4Agent Is Already Stronger

### Durable truth

`mail4agent` already treats mailbox storage, thread state, and registry artifacts as durable truth. Codex's mailbox is currently much closer to a session-local queue.

### Protocol and state ownership

`mail4agent` is already moving toward protocol/state validation owned by the mailbox runtime rather than by transient agents. Codex v2 does not yet provide that depth.

### Separation of concerns

The `mailbox server -> oncall supervisor -> execution backend` split in `mail4agent` is clearer and more reusable than coupling collaboration semantics to one app-thread runtime.

### Recovery model

`mail4agent` already has explicit restart, reuse, idle-eviction, and recovery-summary thinking. That is broader than what Codex's current mailbox layer appears to target.

## What Not To Copy

### 1. Do not make the mailbox in-memory-only

Codex's mailbox is useful as a collaboration primitive, but `mail4agent` should continue treating durable mailbox/thread state as the source of truth.

### 2. Do not collapse mailbox identity into task-path identity

Codex can rely heavily on canonical task paths because all agents live inside one collaboration runtime. `mail4agent` still needs mailbox addresses, sessions, and route policy as first-class concerns.

The right model for `mail4agent` is:

- mailbox address for transport and visibility
- `thread_id` for causal chain
- logical task handle for ownership
- `worker_id` for runtime binding

### 3. Do not turn ordinary threads into broadcast rooms

Codex v2 does not argue for that, and `mail4agent` should keep resisting that temptation.

If many-party synchronization is ever needed, it should still land as an explicit `group_round` layer rather than by changing base thread semantics.

## Recommended Changes For Mail4Agent

## Recommendation 1: Add an explicit delivery wake policy

Add a first-class field to mailbox-visible task traffic that expresses whether a message is:

- `queue_only`
- `wake_target`

Possible shape:

```json
{
  "task_status": "in_progress",
  "delivery_mode": "queue_only",
  "trigger_turn": false
}
```

Notes:

- `trigger_turn` can remain the low-level boolean if that is the simplest runtime primitive.
- `delivery_mode` is still worth exposing because it is easier to read in logs, registry files, and client output.
- This should apply to task-oriented envelopes, not only to internal supervisor state.

Expected benefits:

- cleaner separation between notification and assignment
- less prompt-level ambiguity for oncall roles
- easier terminal-notice absorption

## Recommendation 2: Introduce a stable logical task handle

Add a mailbox-visible logical task identifier that is separate from:

- mailbox address
- `thread_id`
- `worker_id`

Suggested metadata:

- `task_handle`
- `parent_task_handle`
- `owner_address`
- `bound_worker_id` as optional runtime state, not primary identity

Expected benefits:

- cleaner continuity across worker reuse and worker replacement
- easier direct-parent completion routing
- clearer coordination across workspace-specific bindings

## Recommendation 3: Add mailbox revision waiting

Add a mailbox or thread revision counter that increments when thread-visible collaboration state changes.

Possible future surface:

- `GET /thread?wait_for_revision_gt=<N>`
- CLI `thread-watch --since-revision <N>`
- oncall watcher logic that waits for thread revision changes instead of only polling backend lifecycle

Expected benefits:

- better support for mailbox-native review and acceptance flows
- less backend-specific watch logic
- a clean primitive for "wake me when collaboration state changes"

## Recommendation 4: Promote answer-boundary gating into runtime state

The current completion ping-pong mitigation is good, but it is still too prompt- and supervisor-shaped.

Promote the idea into explicit runtime state:

- if a thread round has already emitted a terminal result for the current owner, late queue-only status notices should remain queued or be absorbed
- only explicit wake-worthy follow-up work should reopen the round

Expected benefits:

- fewer accidental no-op spawns
- more principled handling of terminal notices
- clearer thread-round lifecycle

## Recommended First Patch Queue

### Patch 1: Envelope metadata

Add bounded task-envelope metadata for:

- `delivery_mode`
- `trigger_turn`
- `task_handle`
- `parent_task_handle`

Keep backward compatibility with the current JSON-first runtime.

### Patch 2: Thread registry and inspection

Teach thread registry artifacts to persist:

- logical task handle
- parent task handle
- last thread revision
- last wake-worthy message id

### Patch 3: Wait and watch surface

Add a simple mailbox/thread revision counter and one bounded wait surface on top of it.

This should be enough to validate whether mailbox-activity waiting actually improves operator and cross-project flows before doing anything more ambitious.

### Patch 4: Supervisor adoption

Teach oncall handling to distinguish:

- queue-only notices
- wake-worthy follow-up tasks

This should replace some current prompt-only discipline with runtime-visible semantics.

## Current Recommendation

Adopt the semantics, not the storage model.

`mail4agent` should borrow from Codex `multi_agent_v2` in the following order:

1. queue-vs-wake delivery semantics
2. stable logical task handles
3. mailbox-revision waiting
4. answer-boundary gating

It should explicitly not borrow:

- in-memory mailbox truth
- task-path-only identity
- implicit group-chat expansion of ordinary threads

## Decision

Treat Codex `multi_agent_v2` as confirmation that `mail4agent` is on the right thread model, plus a useful source of collaboration-runtime refinements.

The next `mail4agent` lift should be semantic tightening, not architectural replacement.
