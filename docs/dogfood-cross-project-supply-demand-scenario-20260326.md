# Cross-Project Supply-Demand Dogfood Scenario

## Goal

Define a mailbox-native dogfood scenario where two different projects have a real supply-demand relationship instead of only same-project coordination.

The point of this scenario is to validate that `mail4agent` is useful when:

- one project needs bounded help, artifacts, or decisions from another project
- requests are naturally asynchronous
- the supplier side benefits from an oncall mailbox instead of a permanently blocked interactive session
- thread continuity, retry behavior, and cross-project routing matter

## Scenario Summary

Use two projects:

- `consumer_app`
  - owns the user-facing or workload-facing project
  - creates requests for help or artifacts
- `shared_tools`
  - owns specialized tooling, adapters, or operator support
  - supplies answers, artifacts, or bounded patch work back to `consumer_app`

The supply-demand relationship is:

- `consumer_app` needs bounded integration help from `shared_tools`
- `shared_tools` acts like a service project with an intake mailbox plus an oncall operator

This is a better dogfood fit than same-project triage because it exercises:

- explicit cross-project routing policy
- project identity in `whoami`, `send`, and `thread`
- request / clarification / delivery / acceptance loops across project boundaries
- the value of mailbox threads as a durable coordination channel rather than a local queue

## Recommended Concrete Theme

The best first concrete version is:

- `consumer_app` requests an integration bundle from `shared_tools`
- `shared_tools` returns a bounded, machine-readable integration answer

Examples of the supplied output:

- a routing-explain result for a target address
- a compact environment/export bundle
- a bridge configuration snippet
- a small operator verdict with exact next steps

The output should stay text-first and structured, not binary-heavy.

## Projects, Roles, and Mailboxes

### `consumer_app`

- `planner@consumer_app.codex`
  - writes the initial request
  - can forward supplier replies to `integrator` through mailbox-native handoff
- `integrator@consumer_app.codex`
  - validates the supplied answer and sends acceptance or revision feedback

### `shared_tools`

- `intake@shared_tools.codex`
  - stable service mailbox that receives incoming work
- `operator@shared_tools.codex`
  - primary oncall worker for bounded execution
- `reviewer@shared_tools.codex`
  - optional second-hop reviewer for quality checks

## Routing Policy

Do not rely on broad same-project permissions for this scenario.

Instead, configure explicit cross-project routes so the test is meaningful:

- allow `consumer_app -> intake@shared_tools.codex`
- allow `shared_tools -> planner@consumer_app.codex`
- allow `shared_tools -> integrator@consumer_app.codex`

If the supplier side needs to fan out internally, keep that inside `shared_tools`.

This makes it easy to verify:

- the request path is truly cross-project
- replies are authorized intentionally, not by accidental global openness

## Message Types

Use a small typed protocol so the thread is scriptable.

Suggested request and reply types:

- `integration_request`
- `integration_clarification`
- `integration_offer`
- `integration_revision_request`
- `integration_acceptance`
- `integration_complete`

Suggested payload fields:

- `request_kind`
  - for example `routing_explain`, `export_bundle`, `bridge_help`
- `request_id`
- `consumer_project_id`
- `supplier_project_id`
- `target`
  - address, route, or artifact target
- `constraints`
- `acceptance_checks`
- `artifact_summary`
- `follow_up_required`

## Happy Path

1. `planner@consumer_app.codex` sends `integration_request` to `intake@shared_tools.codex`.
2. `shared_tools` oncall claims the delivery.
3. The supplier operator reads the thread and either:
   - replies directly with `integration_offer`, or
   - asks one `integration_clarification`.
4. `planner@consumer_app.codex` forwards the supplier reply to `integrator@consumer_app.codex` with `client.py handoff`.
5. `integrator@consumer_app.codex` verifies the answer.
6. The consumer side replies with either:
   - `integration_acceptance`, or
   - `integration_revision_request`.
7. Supplier responds with `integration_complete`.

This keeps the same thread alive across both projects and gives a real reason for cross-project mailbox use.

Why the handoff matters:

- supplier replies to `planner@consumer_app.codex` are not automatically visible to `integrator@consumer_app.codex`
- `client.py handoff` keeps the same `thread_id`, points back to the original message via `in_reply_to_message_id`, and embeds a source snapshot so the reviewer mailbox can continue work without broader server-side visibility changes

## Failure and Retry Path

The same scenario should also support one bounded retry.

Recommended failure shape:

- `shared_tools` oncall claims the request
- the child operator exits non-zero
- supervisor `nack`s the delivery with a short `last_error`
- the request reappears in the retry queue
- a later oncall pass reclaims it and completes successfully

This validates that cross-project work is still recoverable without losing thread history.

## Why This Scenario Is Strong

This scenario tests several things at once without becoming too open-ended:

- cross-project routing is intentional
- oncall behavior is useful on the supplier side
- mailbox threads carry multi-round contract negotiation
- retry and re-claim behavior matter
- the final result is still small enough to validate from CLI and HTTP

It also matches a realistic organizational shape:

- one project produces specialized help
- another project consumes it on demand

## Recommended First Drill

For the first real cross-project drill, keep it bounded:

- request kind: `routing_explain`
- consumer side: `planner@consumer_app.codex`
- supplier side: `intake@shared_tools.codex` handled by `operator` oncall
- one clarification allowed
- one acceptance reply required

This is a good first step because it uses existing mailbox semantics heavily without forcing binary artifact transport or large repo changes.

## Follow-On Variants

After the first drill works, the next two variants should be:

1. `export_bundle`
   - consumer asks `shared_tools` for a machine-readable environment snapshot
2. `bounded_patch_request`
   - consumer asks `shared_tools` operator to make one tightly scoped repo change and reply with the resulting commit or diff summary

## Live Status

Completed live runs:

- text-first `routing_explain`: [E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-report-20260326.md](E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-report-20260326.md)
- code-changing `bounded_patch_request`: [E:\agent_misc\mail4agent\docs\dogfood-cross-project-bounded-patch-request-report-20260326.md](E:\agent_misc\mail4agent\docs\dogfood-cross-project-bounded-patch-request-report-20260326.md)
- consumer-side `planner -> integrator` handoff: [E:\agent_misc\mail4agent\docs\dogfood-cross-project-handoff-report-20260326.md](E:\agent_misc\mail4agent\docs\dogfood-cross-project-handoff-report-20260326.md)

Current follow-up capability:

- consumer-side planner to integrator review can now use `python .\client.py handoff --message-id <MESSAGE_ID> --to-address integrator@consumer_app.dogfood ...`

Those variants increase the value of mailbox threads while keeping the same supply-demand shape.
