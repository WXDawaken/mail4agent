# Dogfood Feedback Server Update Scenario

## Goal

- Exercise a realistic maintenance loop after live mailbox dogfood uncovers a server-side issue or ergonomic gap.
- Verify that `mail4agent` can survive a bounded server update without losing operator control, mailbox continuity, or client recoverability.

## Why This Scenario Matters

The current dogfood path already proved that real Codex agents can exchange planner/reviewer work through the canonical Python mailbox server. The next operational question is not just "can agents use the mailbox?", but:

- can operators react to dogfood feedback quickly
- can the server be updated safely
- can clients recover cleanly after restart
- can the same thread continue after the update

This scenario is meant to test that full loop.

## Scenario Shape

Use the existing medium dogfood path as the baseline:

- server started with `MAILBOX_ADMIN_TOKEN`
- dogfood harness/project bootstrapped by `dogfood_smoke_bootstrap.py`
- planner and reviewer launched with `launch_dogfood_medium_agent.ps1`

Then inject one bounded feedback item that requires a server-side update. The feedback itself should also travel through the mailbox rather than being captured out-of-band. Good examples:

- mailbox CLI defaults still force an explicit `--to-address` in a path that should be inferable from session context
- operator auth on normal mailbox routes is narrower or broader than intended
- retry-queue or thread-summary output needs a small contract correction
- a server-only response shape or auth-boundary issue needs tightening

Avoid UI work in this scenario. Keep it on runtime/server/CLI semantics.

## Recommended Test Flow

1. Run a normal dogfood smoke.
   Use the existing operator -> planner -> reviewer -> planner loop.

2. Capture one concrete feedback item through mailbox traffic.
   Recommended pattern:
   - planner or reviewer sends a short feedback message to `operator@mail4agent.dogfood`
   - the feedback references the active thread or message being discussed
   - the payload includes:
     - observed behavior
     - expected behavior
     - whether the issue is server-only, CLI-only, or mixed

3. Have the operator receive and acknowledge that feedback through mailbox commands.
   The maintenance loop should start from a real mailbox-delivered feedback item, not an external note.

4. Notify clients about the maintenance window.
   Minimum notification should say:
   - the server will restart
   - current session tokens will become invalid
   - clients must re-run `client.py login`

   This notice should also be sent through mailbox where practical, for example:
   - operator sends a short maintenance notice to the planner and reviewer mailboxes
   - or operator appends a maintenance note on the active thread before restart

5. Back up the SQLite database.
   This scenario should explicitly copy the live database file before the update.

6. Stop the server and apply one bounded server update.
   Prefer a change small enough to review in one pass.

7. Restart the server on the same database.
   Keep the same admin token and port.

8. Re-login the planner and reviewer from their existing harness token.
   This checks the intended recovery path:
   - harness token survives restart
   - session token does not
   - login re-establishes a usable session

9. Resume work on the same thread or on one follow-up operator message.
   The key is that post-update behavior should be visible on real mailbox traffic, not just `healthz`.

10. Run operator post-update checks.
   At minimum:
   - `whoami`
   - `thread`
   - `thread-summaries`
   - `retry-queue`

## Acceptance Criteria

- Pre-update dogfood traffic succeeds.
- Feedback is concrete, tied to one bounded server change, and delivered through mailbox.
- Upgrade notice is issued before restart, preferably through mailbox.
- Server restart succeeds on the same SQLite database.
- Existing harness token still works after restart.
- Existing session token does not work after restart.
- Fresh `client.py login` succeeds after restart.
- The updated behavior is visible in post-update mailbox traffic.
- No unexpected retry backlog remains after the maintenance drill.
- Existing thread history is preserved across the restart.

## What This Scenario Specifically Tests

- operational readiness, not just feature delivery
- mailbox-native feedback collection instead of out-of-band bug notes
- restart and re-login ergonomics
- bounded server maintenance on the canonical Python baseline
- whether dogfood feedback can move cleanly into a production-like update loop

## Suggested First Concrete Drill

Use this as the first explicit maintenance drill:

- baseline: current medium planner/reviewer dogfood smoke
- feedback theme: "session-scoped CLI ergonomics still require too much explicit targeting"
- feedback delivery: reviewer sends the concrete issue summary to `operator@mail4agent.dogfood`
- server update class: bounded auth/context or response-shape improvement
- recovery check: planner and reviewer must re-login from the stored harness token and continue mailbox work

This is intentionally narrow. The first drill should prove the maintenance loop, not maximize feature size.

## Best Small Feature Fit

If this maintenance drill should align with one existing small-feature direction, the best fit is:

- `small_session_logout_and_expiry_introspection`

Why this one fits best:

- it is already adjacent to the restart-and-relogin story that the maintenance drill is trying to prove
- it turns a restart from "clients suddenly get 401" into a clearer lifecycle contract
- it helps both operator notice and client recovery because expiry metadata and explicit logout semantics are easy to verify before and after restart
- it stays bounded to auth/session behavior rather than opening a larger read-model or indexing project

Why the other current small features are weaker fits:

- `small_inbox_list_and_filters`
  - useful product surface, but it is more about mailbox reading ergonomics than maintenance or restart recovery
- `small_retry_queue_visibility`
  - already promoted into the canonical dogfood baseline, so it no longer makes sense as the first new maintenance-drill target

So the current recommendation is:

- use the explicit dogfood feedback item about session-scoped CLI ergonomics as the first tiny server fix
- if we want that maintenance drill to roll directly into one named small feature promotion, choose `small_session_logout_and_expiry_introspection`

## Out Of Scope

- admin page redesign
- Rust-primary deployment
- multi-node or distributed rollout
- destructive schema rewrites
- long-running autonomous agents that stay up across restart without explicit re-login

## Follow-Up Options

Once the first drill is stable, the same pattern can expand into:

- a schema-touching upgrade drill with explicit DB backup plus restore path
- a client-compatibility drill where one old client and one new client both operate after upgrade
- a benchmark task where the agent must implement the bounded server fix and describe the restart/re-login impact
