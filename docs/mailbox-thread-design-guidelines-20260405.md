# Mailbox Thread Design Guidelines

## Intent

- Keep mailbox threads lightweight and predictable for A2A experiments.
- Treat `thread_id` as a shared causal chain, not as a group-chat membership primitive.

## Rules

- One message should target one mailbox address.
- A thread may involve multiple mailboxes over time, but only through point-to-point hops such as `reply`, `send` on the same `thread_id`, or `handoff`.
- Do not model a thread as a broadcast room or durable participant roster.
- Use explicit `task_status`, owner/waiting metadata, and oncall registry state to track responsibility instead of assuming every participant should react to every thread message.
- When work can proceed independently, open a child thread or a sibling thread instead of widening one thread into a many-party conversation.
- When work returns from another mailbox, summarize it back into the main thread with bounded context rather than replaying the other mailbox's full history.
- Prefer mailbox-native handoff and bounded recovery summaries over hidden long-lived shared state.

## Why

- Group-chat semantics make responsibility ambiguous.
- Slow models can fall behind when every update is broadcast to every participant.
- Completion noise and no-op replies become much harder to suppress in many-party threads.
- Point-to-point thread hops keep causality visible while preserving bounded context and clearer ownership.
