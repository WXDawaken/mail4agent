# Mainline Plan

## Goal

- Use `mail4agent` as a reusable local mailbox server and CLI repo for future agent communication tests.

## Scope

- Keep the repo itself lightweight and dependency-free.
- Preserve the existing HTTP server, admin UI, SQLite storage, and CLI flows as the baseline.
- Add future test-specific notes here before making broader integration changes.

## Current State

- The repo is cloned locally at `E:\agent_misc\mail4agent`.
- The canonical dogfood branch now carries the first Wave 1 Python feature merge instead of only the initial scaffold.
- The project exposes a local HTTP mailbox server plus CLI and adapter helpers, now including admin direct mailbox access, retry-queue visibility, and mailbox-scoped thread summaries/unread state.
- The canonical repo now also includes explicit session logout plus expiry introspection, so maintenance and restart drills can validate re-login behavior against real runtime semantics.
- The canonical repo now also includes session-scoped `default_inbox_address` handling for thread-state surfaces, so logged-in planner/reviewer runs no longer need explicit `--to-address` recovery on `thread-summaries` and `mark-thread-read`.
- The current shared benchmark evidence now supports a `Python-first, Rust-selective` language split rather than a full Rust rewrite.
- The canonical repo now also has a bounded dogfood smoke/update profile for real Codex-agent mailbox trials: medium planner/reviewer plus a high-effort operator path for bounded repo-local updates.

## Milestones

- Keep the repo smoke-testable with repo-local standard-library validation.
- Preserve Python as the product source of truth for new mailbox capabilities.
- Promote benchmark-proven Python feature slices into the canonical repo in bounded waves.
- Keep auth/session lifecycle observable enough for bounded maintenance drills and re-login recovery.
- Use Rust selectively for bounded backend slices once Python behavior is stabilized.
- Keep a lightweight repo-local dogfood smoke path so real mailbox usage can be exercised outside the benchmark harness.

## Risks

- Future benchmark glue could accidentally drift the repo away from its simple standard-library baseline.
- If test harness assumptions are added ad hoc, agent communication results may become hard to compare across runs.
- A premature full-language migration would blur product ownership and weaken comparability while Rust still depends on Python as a semantic oracle for many feature-port validations.
- The new retry-queue and thread-summary surfaces currently compute from live SQLite rows without heavier indexing or pagination beyond simple limits; keep later scale work explicit.

## Decisions

- Keep the repo as a separate workspace under `E:\agent_misc\mail4agent`.
- Treat Python as the primary home for product semantics, auth policy, operator workflows, CLI UX, admin flows, and future UI work.
- Treat Rust as a selective backend path for server slices, bridge-heavy integrations, export/import, delivery-audit, and other bounded backend capabilities with a clear Python oracle.
- Migrate by capability, not by repo-wide rewrite.
- For dogfood, use this canonical repo as the baseline root rather than a benchmark seed or a per-run benchmark workspace.
- Promote Wave 1 Python mailbox features directly into the canonical repo rather than keeping them in benchmark result workspaces.
- Keep admin page UI work out of scope for the first Wave 1 merge; only runtime/server/CLI/docs/validation moved.
- Keep the first dogfood smoke bounded to medium-effort planner/reviewer runs, but allow a separate high-effort operator lane for bounded repo-local update tasks driven by `dogfood_smoke_bootstrap.py` plus `launch_dogfood_agent.ps1`.

## Open Questions

- Should future experiments run against this repo in place, or against seeded copies inside benchmark workspaces?
- Do we want benchmark tasks that modify the mailbox server itself, or only tasks that consume it as external infrastructure?
- Which backend capabilities are now mature enough to move from `Python primary` to `Rust primary` under the promotion criteria?
- Should direct mailbox access eventually accept admin-account Basic auth on normal mailbox routes, or remain static-admin-token-only?

## Language Strategy

- Source-of-truth memo: `E:\agent_misc\docs\mail4agent-python-rust-split-plan-20260324.md`
- Actionable follow-up backlog: `E:\agent_misc\docs\mail4agent-language-backlog-20260324.md`
- Dogfood baseline note: `E:\agent_misc\docs\mail4agent-dogfood-baseline-20260324.md`
- Dogfood smoke runbook: `E:\agent_misc\mail4agent\docs\dogfood-medium-smoke-20260324.md`
- Dogfood operator update flow: `E:\agent_misc\mail4agent\docs\dogfood-operator-update-flow-20260324.md`
- Dogfood maintenance scenario: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-scenario-20260324.md`
- Dogfood maintenance drill playbook: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-drill-20260324.md`
- First maintenance continuity report: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-report-20260324.md`
- Thread-defaults target note: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-20260324.md`
- Thread-defaults maintenance result: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-report-20260324.md`
- Dogfood maintenance drill: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-scenario-20260324.md`
- Dogfood maintenance drill playbook: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-drill-20260324.md`
- Dogfood maintenance drill report: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-report-20260324.md`
- Latest real-change maintenance target note: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-20260324.md`
