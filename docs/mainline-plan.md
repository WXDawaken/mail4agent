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
- The canonical repo now also has a first oncall direction: keep `client.py consume` as the generic worker primitive, but move role-aware mailbox supervision into a separate script instead of growing the main CLI indefinitely.
- That first oncall direction is now implemented for `operator`: `mailbox_oncall.py` supervises one claimed delivery at a time, while `launch_dogfood_oncall_agent.ps1` stages runtime assets into a repo-local sandbox-visible directory before launching Codex.
- The current oncall model is still intentionally stateless across supervisor processes, but the server now defaults claims to `mailbox_thread` serialization so one mailbox thread is processed serially even if more than one supervisor watches the same mailbox route.
- A first cross-project dogfood design now also exists in `docs\dogfood-cross-project-supply-demand-scenario-20260326.md`; it treats mailbox as a service channel between a consumer project and a shared-tools supplier project instead of only as same-project coordination.
- The dogfood launchers are now config-driven instead of hardcoding `project_id = mail4agent`; both one-shot and oncall launchers derive login identity from the role runtime config so the same machinery can be reused for cross-project drills.
- The first live cross-project drill is now complete in `docs\dogfood-cross-project-routing-explain-report-20260326.md`; `consumer_app` planner sent one bounded request to `shared_tools` intake, supplier-side oncall replied on the same thread, and the consumer sent acceptance with retry queue left empty.
- The second live cross-project drill is now also complete in `docs\dogfood-cross-project-bounded-patch-request-report-20260326.md`; this time supplier-side oncall made a real bounded repo change (`inbox` sender filtering), local follow-up validation passed, and the consumer sent acceptance on the same mailbox thread.
- The canonical repo now also includes a mailbox-native `client.py handoff` command; it wraps one visible source message into a new handoff message that preserves `thread_id` while carrying source refs and a source payload snapshot for a different target mailbox.
- The third live cross-project drill is now also complete in `docs\dogfood-cross-project-handoff-report-20260326.md`; it proved planner can hand off a supplier reply to integrator without widening server visibility, and integrator can send acceptance on the same thread from its own mailbox.
- A mailbox/oncall architecture note now also exists in `docs\mailbox-oncall-separation-20260328.md`; it defines `mailbox server` as the communication and fact layer, `oncall server` as the supervision layer, and app-server-managed agents as replaceable execution backends rather than durable workflow state.
- A follow-on implementation note now also exists in `docs\mailbox-oncall-implementation-plan-20260328.md`; it maps the current codebase into mailbox-side runtime, oncall supervision, and execution-backend seams, and recommends refactoring toward `oncall_supervisor.py` plus `oncall_registry.py` before adding sticky per-thread agents.
- A mailbox-language implementation note now also exists in `docs\mailbox-language-ir-first-plan-20260328.md`; it evaluates `mailbox_language_spec_v0_2.md` against the current repo, recommends an IR-first rollout inside the mailbox server, and keeps the textual DSL as a separate stdio-capable interpreter layer.
- The first mailbox-language runtime foundation is now also implemented in the mailbox server: protocol registry rows, mailbox `accepts/default` bindings, per-thread protocol/state metadata, admin-recorded thread handoff relations, and thread inspection surfaces now exist without requiring the server to parse the textual DSL.
- A first typed envelope execution surface now also exists above that foundation: the mailbox server can execute normalized `send` / `spawn` message envelopes plus handoff events through protocol-aware runtime validation, including mailbox-accepts checks, protocol-match checks, transition checks, payload-shape checks, and structured runtime error codes.
- A first typed client/CLI surface now also exists above that runtime: `codex_mailbox_client.py` exposes protocol registry and typed envelope helpers, while `client.py` can now run `register-protocol`, `list-protocols`, `set-mailbox-protocols`, `get-mailbox-protocols`, `typed-send`, `typed-spawn`, and `typed-handoff` without making the user hand-write normalized admin JSON.
- The mailbox-language protocol/runtime rules are now also factored into a shared `mailbox_language_runtime.py` module, so future stdio-interpreter work can reuse the same protocol normalization, schema validation, payload validation, and transition resolution logic instead of importing storage-layer internals.
- Those shared mailbox-language rules now also expose explicit protocol compilation and local disk caching via `mailbox_language_cache.py`, giving the future stdio interpreter a place to cache parser/checker outputs without moving live mailbox/thread/runtime decisions out of the server.
- The first standalone mailbox-language stdio interpreter shell now also exists in `mailbox_language_stdio.py`: it speaks JSON lines over stdin/stdout, supports `check` / `lower` / `run` for `protocol_schema`, `mailbox_binding`, `message_envelope`, `handoff_event`, and a first `dsl_program` source artifact, reuses the local protocol compile cache, and calls the same typed admin-backed runtime helpers as `client.py` instead of teaching the mailbox server to parse DSL text directly.
- A new shared source parser/checker module, `mailbox_language_source.py`, now lowers a first bounded textual DSL slice into typed runtime artifacts. The current supported source grammar covers `protocol`, `mailbox`, bounded `let` bindings, nested object values, `send`, `send text`, `spawn`, and `handoff`, still keeps mailbox-address resolution and runtime truth outside the parser layer, and now also rejects bounded primitive/list payload type mismatches plus `let` annotation mismatches during `check` / `lower` instead of waiting for live runtime execution.
- That same DSL layer now also emits bounded source diagnostics: `MailboxRuntimeError` carries optional details, `mailbox_language_source.py` annotates parse/check failures with `source_phase` plus line/column metadata, and `mailbox_language_stdio.py` forwards those fields in structured JSON responses so editor or pipe consumers can distinguish parse vs checker failures without scraping the error string.

## Milestones

- Keep the repo smoke-testable with repo-local standard-library validation.
- Preserve Python as the product source of truth for new mailbox capabilities.
- Promote benchmark-proven Python feature slices into the canonical repo in bounded waves.
- Keep auth/session lifecycle observable enough for bounded maintenance drills and re-login recovery.
- Use Rust selectively for bounded backend slices once Python behavior is stabilized.
- Keep a lightweight repo-local dogfood smoke path so real mailbox usage can be exercised outside the benchmark harness.
- Keep a lightweight repo-local oncall path so claimed mailbox work can be supervised without a permanently blocked interactive Codex session.
- Keep cross-project consumer-side review bounded by explicit mailbox handoff instead of widening server-side thread visibility between unrelated mailboxes.

## Risks

- Future benchmark glue could accidentally drift the repo away from its simple standard-library baseline.
- If test harness assumptions are added ad hoc, agent communication results may become hard to compare across runs.
- A premature full-language migration would blur product ownership and weaken comparability while Rust still depends on Python as a semantic oracle for many feature-port validations.
- The new retry-queue and thread-summary surfaces currently compute from live SQLite rows without heavier indexing or pagination beyond simple limits; keep later scale work explicit.
- The current oncall path still has no global mailbox lock across all routes; `mailbox_thread` serialization is intentionally local to one mailbox, so the same thread can still be processed independently by different route mailboxes when that is semantically desired.
- Cross-mailbox handoff still duplicates only the chosen source message snapshot, not the sender mailbox's entire historical thread context; deeper review flows should keep summaries explicit.
- If we later introduce sticky per-thread agents, mailbox-visible facts and bounded handoff summaries should stay recoverable without depending on one surviving long-lived agent session.

## Decisions

- Keep the repo as a separate workspace under `E:\agent_misc\mail4agent`.
- Treat Python as the primary home for product semantics, auth policy, operator workflows, CLI UX, admin flows, and future UI work.
- Treat Rust as a selective backend path for server slices, bridge-heavy integrations, export/import, delivery-audit, and other bounded backend capabilities with a clear Python oracle.
- Migrate by capability, not by repo-wide rewrite.
- For dogfood, use this canonical repo as the baseline root rather than a benchmark seed or a per-run benchmark workspace.
- Promote Wave 1 Python mailbox features directly into the canonical repo rather than keeping them in benchmark result workspaces.
- Keep admin page UI work out of scope for the first Wave 1 merge; only runtime/server/CLI/docs/validation moved.
- Keep the first dogfood smoke bounded to medium-effort planner/reviewer runs, but allow a separate high-effort operator lane for bounded repo-local update tasks driven by `dogfood_smoke_bootstrap.py` plus `launch_dogfood_agent.ps1`.
- Keep `mailbox` and `oncall` logically separable even while they live in the same repo; prefer explicit module and process boundaries over embedding all supervision logic into the mailbox server itself.
- Keep the mailbox server DSL-agnostic; implement `mailbox_language_spec` via typed IR/runtime changes first, and add the textual language as a separate interpreter with native stdio support.

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
- Dogfood operator oncall flow: `E:\agent_misc\mail4agent\docs\dogfood-operator-oncall-20260325.md`
- Cross-project supply-demand scenario: `E:\agent_misc\mail4agent\docs\dogfood-cross-project-supply-demand-scenario-20260326.md`
- Cross-project routing-explain drill: `E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-drill-20260326.md`
- Cross-project routing-explain report: `E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-report-20260326.md`
- Cross-project bounded patch report: `E:\agent_misc\mail4agent\docs\dogfood-cross-project-bounded-patch-request-report-20260326.md`
- Cross-project handoff report: `E:\agent_misc\mail4agent\docs\dogfood-cross-project-handoff-report-20260326.md`
- Mailbox/oncall separation note: `E:\agent_misc\mail4agent\docs\mailbox-oncall-separation-20260328.md`
- Mailbox/oncall implementation plan: `E:\agent_misc\mail4agent\docs\mailbox-oncall-implementation-plan-20260328.md`
- Mailbox language IR-first plan: `E:\agent_misc\mail4agent\docs\mailbox-language-ir-first-plan-20260328.md`
- Dogfood maintenance scenario: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-scenario-20260324.md`
- Dogfood maintenance drill playbook: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-drill-20260324.md`
- First maintenance continuity report: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-report-20260324.md`
- Thread-defaults target note: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-20260324.md`
- Thread-defaults maintenance result: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-report-20260324.md`
- Dogfood maintenance drill: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-scenario-20260324.md`
- Dogfood maintenance drill playbook: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-drill-20260324.md`
- Dogfood maintenance drill report: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-report-20260324.md`
- Latest real-change maintenance target note: `E:\agent_misc\mail4agent\docs\dogfood-feedback-server-update-thread-defaults-20260324.md`
