# Mainline Plan

## Goal

Use `mail4agent` as a lightweight, dependency-free local mailbox server and CLI fixture for A2A collaboration tests.

## Scope

- Preserve the HTTP server, admin UI, SQLite storage layer, Python client, and CLI as the stable baseline.
- Prefer changes that directly unlock A2A experiments, reduce experiment cost, or improve observability.
- Keep repo-public content focused on reusable mailbox/oncall mechanics rather than local experiment runbooks.
- Keep local operator prompts, one-off smoke scripts, progress notes, and workspace-specific collaboration instructions untracked.
- Avoid turning this repo into a general workflow product unless an experiment needs that capability.

## Current State

- The repo is on `dev` as the integration branch.
- The current implementation is Python-first and standard-library-only.
- The server exposes durable mailbox facts: harnesses, projects, mailboxes, messages, deliveries, claims, retry state, thread reads, and typed runtime metadata.
- The CLI supports login/logout, send, handoff, claim, ack/nack/heartbeat, reply, inbox, retry queue, thread views, thread summaries, mark-read, generic consume, and typed mailbox-language commands.
- The mailbox-language direction is JSON IR first: protocol/runtime schemas and normalized message envelopes are the backend contract, while textual DSL remains optional frontend sugar through `mailbox_language_stdio.py`.
- The oncall direction keeps `mailbox server -> oncall supervisor -> execution backend` as separate layers.
- `mailbox_oncall.py`, `mailbox_oncall_server.py`, `oncall_supervisor.py`, `oncall_registry.py`, and the app-server/codex executors provide the current supervised-worker surface.
- App-server oncall can reuse process-local workers for matching mailbox thread/workspace bindings and can cold-recover from mailbox plus registry metadata when the prior worker is gone.
- The thread-model decision is explicit: one message targets one mailbox, and `thread_id` is a causal chain rather than a group-chat roster.
- If many-party synchronization becomes necessary, use a separate `group_round` or barrier layer with participant ack state and round locking rather than changing ordinary thread semantics.
- A Codex `multi_agent_v2` comparison memo identifies the collaboration semantics worth borrowing without changing durable mailbox truth: queue-vs-wake delivery semantics, stable logical task handles, mailbox-revision waiting, and answer-boundary gating.
- A cross-workspace role-aware oncall draft keeps `oncall` as a supervision capability rather than a role, resolves delivery targets through role bindings, and recommends reusable worker binding by `(role, workspace_dir, thread_id)`.

## Milestones

- Keep the repo smoke-testable with standard-library validation.
- Preserve Python as the product source of truth for mailbox behavior and CLI UX.
- Keep the mailbox server DSL-agnostic while growing typed IR/runtime validation where it helps experiments.
- Keep oncall supervision recoverable from mailbox-visible facts and bounded registry summaries.
- Keep cross-project and cross-workspace coordination explicit through point-to-point handoff, child threads, task status, and role-aware oncall routing.

## Risks

- Local experiment helpers can obscure the reusable product surface if they are tracked as first-class repo files.
- A premature workflow-engine layer could shift effort away from A2A task design and evaluation.
- Sticky workers can hide state unless mailbox and registry artifacts remain the durable source of truth.
- Queue-vs-wake, task handles, and revision waiting should be introduced as small semantics, not as a broad architecture replacement.
- Any future Rust or alternate backend work should keep Python behavior as an oracle until a specific capability proves stable.

## Decisions

- Keep this repo lightweight and Python-standard-library-only.
- Use `dev` as the integration branch.
- Keep local progress, AGENTS instructions, and local experiment runbooks out of tracked public content.
- Treat `mail4agent` primarily as an A2A experiment fixture; expand it only when that directly enables collaboration tests or materially improves debugging.
- Keep mailbox and oncall separable even while they live in the same repository.
- Keep JSON IR as the mailbox-language mainline backend contract.
- Keep textual DSL optional and stdio-driven.
- Keep ordinary mailbox threads point-to-point; use handoff, child threads, task-status metadata, or an explicit future `group_round` layer for wider coordination.

## Open Questions

- Should the next oncall implementation slice add a manifest-backed workspace-root role router, or keep Codex app/CLI as the temporary coordinator until another experiment proves manual routing is too costly?
- Should deterministic role/workspace policy failures send a mailbox-visible deferred reply directly from the supervisor, or only record registry failure and ack?
- Should role model settings come from an oncall manifest, `.codex/agents/<role>.toml`, or both with a strict precedence rule?
- Should the registry path be migrated immediately to include role, or only when shared intake addresses become necessary?
- Which next A2A workloads require mailbox-server changes, and which should stay in the benchmark/task layer?

## Key Docs

- Mailbox/oncall separation note: `docs/mailbox-oncall-separation-20260328.md`
- Mailbox/oncall implementation plan: `docs/mailbox-oncall-implementation-plan-20260328.md`
- Mailbox language IR-first plan: `docs/mailbox-language-ir-first-plan-20260328.md`
- Thread design guidelines: `docs/mailbox-thread-design-guidelines-20260405.md`
- Group round draft: `docs/mailbox-group-round-draft-20260405.md`
- Codex multi-agent v2 comparison: `docs/mailbox-codex-multi-agent-v2-comparison-20260418.md`
- Cross-workspace role-aware oncall draft: `docs/mailbox-cross-workspace-role-aware-oncall-draft-20260425.md`
- Rust-port scenario notes: `docs/rust-port-test-scenarios-20260322.md`
- Feature-extension scenario notes: `docs/feature-extension-test-requirements-20260322.md`
