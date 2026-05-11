# Mail4Agent Feature Extension Test Requirements

## Goal

Define a set of benchmarkable feature-expansion requirements that grow directly out of `mail4agent`'s current Python product surface instead of focusing on language migration.

This line is meant to test whether an agent can extend an already-working local mailbox product with coherent new behavior, tests, and operator ergonomics.

## Current Functional Baseline

Today `mail4agent` already provides:

- SQLite-backed harness, project, mailbox, message, and delivery storage
- HTTP routes for `login`, `whoami`, `resolve`, `message`, `thread`, `send`, `claim`, `ack`, `nack`, and `heartbeat`
- admin bootstrap plus admin UI for harness, project, mailbox, and token setup
- a CLI for login, send, claim, thread, reply, ack, nack, heartbeat, and consume flows
- a worker loop and adapter layer for reply, nack, retry, and heartbeat semantics

The best extension requirements are the ones that stay adjacent to those capabilities instead of inventing a totally different product.

## Design Principles For This Test Line

- Prefer features that reuse the existing auth model, routing model, and SQLite schema style.
- Prefer requirements that can be validated from CLI and unit tests, not only by manual browser clicking.
- Favor additions that deepen mailbox usefulness for local multi-agent work: inspection, recovery, routing clarity, and operational trust.
- Keep each requirement bounded enough that it can become a benchmark task instead of an open-ended roadmap.

## Requirement Matrix

### `small_inbox_list_and_filters`

- Theme: read-oriented mailbox visibility
- Existing surface it extends:
  - `GET /message`
  - `GET /thread`
  - CLI `thread`
- Requirement:
  - add an inbox-list surface that can show visible deliveries or recent messages for one mailbox address
  - support basic filters such as `status`, `limit`, `message_type`, and `since`
  - expose it via both HTTP and `client.py`
- Why this is a good test:
  - it is the most natural missing read path in a mailbox product that already supports single-message and thread fetches
  - it exercises SQL query shaping, visibility rules, JSON output design, and CLI formatting without touching worker concurrency
- Acceptance:
  - an authenticated caller can list only the inboxes visible to the current harness or agent session
  - default ordering is deterministic and documented
  - CLI JSON output is stable enough for scripted checks
  - tests cover visibility filtering and at least one text-format output path

### `small_session_logout_and_expiry_introspection`

- Theme: auth lifecycle clarity
- Existing surface it extends:
  - `POST /login`
  - `GET /whoami`
  - in-memory agent sessions
- Requirement:
  - add a small session-management surface with explicit logout and clearer expiry introspection
  - make `whoami` return enough expiry metadata for clients to know when re-login is needed
  - add a CLI command for logout or session invalidation
- Why this is a good test:
  - it builds directly on the current session model
  - it tests small auth-state edits without dragging in admin features or delivery semantics
- Acceptance:
  - a logged-in session can be invalidated explicitly
  - `whoami` or an adjacent route exposes stable expiry information
  - invalidated sessions lose access immediately while harness-token login still works
  - tests cover logout, expired-session denial, and a clean re-login path

### `small_retry_queue_visibility`

- Theme: failure inspection
- Existing surface it extends:
  - `nack`
  - `retry_after_seconds`
  - `max_attempts`
  - delivery claims
- Requirement:
  - add a read-only way to inspect retry-pending deliveries for a mailbox or project
  - include attempt count, next eligible time, and last error summary
  - support both HTTP and CLI views
- Why this is a good test:
  - current behavior already tracks retries, but operators and agents need visibility into what is waiting
  - it stays read-mostly while proving the agent understands delivery lifecycle fields
- Acceptance:
  - retry-pending deliveries appear only after a nack path
  - last error text is safely truncated and deterministic
  - visibility rules match mailbox ownership and session claim permissions
  - tests cover retry scheduling and hidden deliveries outside the caller scope

### `medium_batch_claim_and_ack`

- Theme: worker throughput
- Existing surface it extends:
  - `claim`
  - `ack`
  - `heartbeat`
  - worker consume loop
- Requirement:
  - add a bounded batch-delivery mode so workers can claim and ack multiple deliveries in one loop
  - keep lease and claim-token behavior explicit for each claimed delivery
  - make the CLI and worker layer support a small batch count without becoming a queue framework
- Why this is a good test:
  - it stretches the current single-delivery worker design in a realistic direction
  - it tests state transitions, claim fairness, and API shape design
- Acceptance:
  - a caller can claim up to `N` visible deliveries deterministically
  - ack and nack still operate per delivery and cannot mix up claim tokens
  - the worker loop can process a small batch without dropping heartbeat safety
  - tests cover partial success, partial retry, and no-duplicate-claim behavior

### `medium_routing_explain_surface`

- Theme: debugging routing policy
- Existing surface it extends:
  - `allow_project_pair`
  - `allow_same_project`
  - `deny_all`
  - `resolve`
  - send authorization
- Requirement:
  - add a routing-explain endpoint and CLI command that tells the operator why a send would be allowed or denied
  - include which policy matched and which caller identity was evaluated
  - keep the result safe for local debugging without leaking hidden mailboxes
- Why this is a good test:
  - policy debugging is a natural next step once a product already has allow and deny rules
  - it measures whether the agent can expose internal decision logic clearly without rewriting the routing model
- Acceptance:
  - an operator can ask for an explain result before attempting a send
  - the output clearly distinguishes `allowed`, `denied`, and `unknown address`
  - matched policy information is present when practical
  - tests cover same-project allow, explicit deny, and invisible-address cases

### `medium_scheduled_delivery_inspector`

- Theme: delayed work visibility
- Existing surface it extends:
  - `deliver_after_seconds`
  - `expires_in_seconds`
  - claim eligibility rules
- Requirement:
  - add a way to inspect scheduled-but-not-yet-claimable deliveries
  - support filters by mailbox, project, and ready-vs-delayed state
  - optionally expose a compact admin UI panel if the implementation stays small
- Why this is a good test:
  - the repo already has delayed delivery semantics but weak visibility into them
  - it combines query logic, CLI output, and maybe light admin UI updates without touching core auth
- Acceptance:
  - delayed deliveries do not show as claimable before their ready time
  - the inspector returns deterministic fields for ready time and expiration
  - CLI and HTTP surfaces agree on filtered counts
  - tests cover ready transitions and expired hidden entries

### `medium_thread_summary_and_unread_state`

- Theme: conversation ergonomics
- Existing surface it extends:
  - `thread`
  - `message`
  - send and reply semantics
- Requirement:
  - add a compact thread-summary surface that includes reply count, latest actor, latest timestamp, and unread-like state per mailbox
  - unread state can remain local to mailbox visibility instead of a full per-user read-receipt system
- Why this is a good test:
  - this keeps the product centered on mailbox conversations
  - it forces thoughtful schema extension and query design without becoming a notification platform
- Acceptance:
  - thread summaries are stable and consistent with full thread payloads
  - unread-like state changes only on explicit message visibility or explicit mark-read actions, whichever design is chosen
  - tests cover summary counts, latest-message rollup, and mailbox-scoped visibility

### `medium_admin_mailbox_access`

- Theme: operator superuser mailbox access
- Existing surface it extends:
  - admin auth
  - `whoami`
  - `resolve`
  - `message`
  - `thread`
  - `send`
  - `claim`
  - `ack`
  - `nack`
  - `heartbeat`
- Requirement:
  - let an admin-authenticated caller use the current mailbox API directly without first minting a harness token or agent session
  - keep the route and payload shapes close to the current mailbox surfaces instead of inventing a second mirrored mailbox control plane
  - add CLI auth support so existing mailbox commands can run with an admin token
  - keep admin UI work out of scope for this task so it stays focused on auth and operator flows
- Why this is a good test:
  - it exercises auth-boundary design instead of new schema invention
  - it is a realistic operator need when a local mailbox service must recover or inspect cross-harness traffic quickly
  - it tests whether an agent can expand superuser capability without accidentally widening ordinary caller visibility
- Acceptance:
  - an admin-authenticated caller can use core mailbox read and write routes directly
  - cross-harness mailbox actions work for admin without first creating a harness token or login session
  - ordinary harness and session callers do not gain extra cross-harness visibility
  - tests cover one direct HTTP flow and one CLI flow using an admin token
  - admin UI changes remain explicitly out of scope

### `large_delivery_audit_timeline`

- Theme: operator trust and recovery
- Existing surface it extends:
  - send
  - claim
  - ack
  - nack
  - heartbeat
  - login
- Requirement:
  - add an audit timeline that records the major lifecycle events for a message or delivery
  - events should be queryable by message id, thread id, or delivery id
  - include enough actor and timing data to reconstruct “what happened” during retries and claims
- Why this is a good test:
  - it stays close to the current product while demanding coherent cross-cutting design
  - it is a strong benchmark for schema evolution, API design, and operator-facing evidence
- Acceptance:
  - timeline events cover at least send, claim, ack, nack, heartbeat, and session-authenticated send or claim identity
  - event ordering is deterministic
  - timeline access respects mailbox visibility or admin scope
  - tests cover a retry cycle and a successful reply cycle end to end

### `large_export_import_environment_bundle`

- Theme: reproducible local setups
- Existing surface it extends:
  - admin setup
  - harness/project/mailbox/token creation
  - preview session config
- Requirement:
  - add a bounded export or import bundle for non-secret mailbox topology
  - let an operator snapshot harnesses, projects, mailboxes, routing policies, and optional placeholder token descriptors
  - support restoring that topology into a fresh SQLite database
- Why this is a good test:
  - local agent testing becomes much more reusable if environments can be recreated quickly
  - it exercises schema understanding and product-boundary judgment
- Acceptance:
  - export output is machine-readable and deterministic enough for diffing
  - import can recreate a clean local topology on an empty database
  - secrets are handled carefully: either excluded, redacted, or regenerated by design
  - tests cover export, import, and post-import smoke checks for login plus one send/claim cycle

### `large_webhook_or_stdio_bridge_delivery`

- Theme: integration beyond the local CLI
- Existing surface it extends:
  - consume worker flow
  - adapter and reply semantics
  - send and claim behavior
- Requirement:
  - add one bounded integration bridge so mailbox deliveries can invoke an external command or local webhook and map the result back into reply or nack behavior
  - keep it local-first and dependency-light
- Why this is a good test:
  - it exercises the repo as integration infrastructure for agent-to-agent systems, not only as a message store
  - it rewards careful error handling, timeout choices, and operator-facing docs
- Acceptance:
  - one bridge mode can hand a delivery payload to an external process or local HTTP endpoint
  - success, retryable failure, and fatal failure map cleanly onto reply, nack, or ack semantics
  - logs or result payloads are concise and scriptable
  - tests cover one success path and one retry path

### `large_operator_console_and_smoke_bundle`

- Theme: product finish and operability
- Existing surface it extends:
  - admin UI
  - setup-admin flow
  - server startup
  - CLI smoke paths
- Requirement:
  - add a more deliberate operator surface for local health, setup completion, and quick smoke actions
  - this can be an expanded admin page, a guided setup page, or a small operator CLI bundle
  - keep the focus on making the service easier to bootstrap and trust during local testing
- Why this is a good test:
  - it measures finish quality instead of only backend correctness
  - it is especially relevant if `mail4agent` becomes shared local infrastructure for other agent experiments
- Acceptance:
  - a fresh operator can tell whether setup is complete, whether login is working, and whether one end-to-end message cycle succeeds
  - operator-facing error messages are actionable
  - docs and smoke commands are short enough for another agent to reuse
  - tests cover the operator-facing happy path and at least one missing-setup or auth-failure path

## Recommended Test Order

If we want a staged extension benchmark line, this is the best sequence:

1. `small_inbox_list_and_filters`
2. `small_retry_queue_visibility`
3. `small_session_logout_and_expiry_introspection`
4. `medium_scheduled_delivery_inspector`
5. `medium_routing_explain_surface`
6. `medium_batch_claim_and_ack`
7. `medium_admin_mailbox_access`
8. `large_delivery_audit_timeline`
9. `large_export_import_environment_bundle`
10. `large_operator_console_and_smoke_bundle`
11. `large_webhook_or_stdio_bridge_delivery`

That order starts with read and inspection features, then moves into worker and policy extensions, and only then asks the agent to add cross-cutting operator or integration capabilities.

## Best First Batch

If we only want a practical first batch for benchmark testing, use these three:

- `small_inbox_list_and_filters`
- `medium_routing_explain_surface`
- `large_delivery_audit_timeline`

Together they cover:

- query and visibility design
- policy/debug explainability
- cross-cutting lifecycle evidence

That is a strong “product extension” complement to the existing Rust migration line.
