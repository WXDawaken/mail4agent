# Mailbox Group Round Draft

## Intent

- Preserve the current mailbox default: one message targets one mailbox and `thread_id` remains a causal chain.
- If a future workload truly needs group-chat-like coordination, add it as an explicit `group_round` layer instead of changing ordinary thread semantics.

## Non-Goals

- Do not turn every thread into a broadcast room.
- Do not require every participant to send a reply.
- Do not let one round implicitly start another without an explicit close/open transition.

## Core Model

- A `group_round` belongs to one existing `thread_id`.
- At most one `group_round` may be active for a thread at a time.
- While a `group_round` is active, the thread is round-locked: no new normal work round should begin until the active round is closed.
- The round has an explicit participant set fixed at open time.
- Each participant has independent acknowledgement and response state.

## Ack Versus Reply

- `ack` means the participant has seen the round and incorporated it into its local view.
- `reply` means the participant has additional information, a decision, or a produced artifact worth sending into the thread.
- A participant may `ack` without `reply`.
- A participant should not be forced to emit a no-op reply just to let the round close.

## Participant State

- `pending`: the participant has not yet acknowledged the round.
- `acked`: the participant has acknowledged the round but has not sent a substantive reply.
- `replied`: the participant has acknowledged the round and sent at least one substantive reply for this round.
- `skipped`: the participant explicitly indicates no response is needed from it for this round.
- `timed_out`: the round waited past that participant's acknowledgement deadline.

## Recommended Round Fields

- `round_id`
- `thread_id`
- `opened_by`
- `opened_at`
- `purpose`
- `participants`
- `required_participants`
- `optional_participants`
- `ack_timeout_seconds`
- `close_policy`
- `closed_at`
- `closed_reason`
- `owner_address`

## Recommended Close Policies

- `all_required_acked`
- `all_participants_acked`
- `owner_after_timeout`

Default recommendation:

- Required participants must `ack`.
- Optional participants may remain silent until timeout.
- The round owner may close the round after timeout if every required participant is no longer `pending`.

## Lock Semantics

- Opening a round should create a thread-visible lock record.
- While the lock is active, new deliveries on that thread should be treated as part of the active round or rejected/deferred.
- Follow-up work that does not belong to the active round should go to a child thread or sibling thread.
- Closing the round should release the thread lock and record the closing summary.

## Failure Handling

- Slow or unavailable participants must not block the thread forever.
- A round should support timeout-based closure with explicit `timed_out` markers.
- The owner should be able to close a timed-out round when policy allows it.
- Recovery after watcher restart should use mailbox-visible round state, not hidden in-memory membership.

## Suggested Reply Discipline

- Only send a reply when there is new information, a produced artifact, a decision, or an explicit clarification request.
- Pure "seen" behavior should prefer `ack` over reply.
- Terminal notices after the round is already closed should be absorbed rather than echoed back into the thread.

## Suggested Runtime Surface

- Keep this separate from ordinary `send` / `reply`.
- Add explicit operations such as:
  - `open_group_round`
  - `ack_group_round`
  - `reply_in_group_round`
  - `close_group_round`
  - `inspect_group_round`

## Why This Shape

- It preserves the current point-to-point mailbox model as the default.
- It gives slow models one synchronized round boundary instead of uncontrolled broadcast chatter.
- It separates "everyone has seen this" from "everyone must speak".
- It avoids completion ping-pong and other no-op traffic.
- It keeps recovery inspectable through mailbox-visible state and explicit close policies.

## Current Recommendation

- Do not implement this yet unless a workload clearly needs synchronized many-party coordination on one thread.
- Until then, prefer handoff, child threads, bounded summaries, and explicit `task_status`.
