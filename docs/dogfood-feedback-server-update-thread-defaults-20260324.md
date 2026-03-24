# Dogfood Feedback Update Target: Session-Scoped Thread Defaults

Date: `2026-03-24`

Status: implemented in the canonical baseline on `2026-03-24`; the next use of this note is to drive a before/after maintenance rehearsal around the promoted change.

## Why This Is The Next Drill

The first mailbox-native maintenance drill already exposed a concrete small ergonomics gap during a real planner run:

- `python .\client.py whoami` succeeded under a valid planner session
- the next planned command, `python .\client.py thread-summaries --limit 10`, did **not** work on the first try
- the planner had to recover by adding `--to-address planner@mail4agent.dogfood`

That makes this a strong next maintenance target because:

- it was observed in real dogfood, not invented afterward
- it is small and bounded
- it is visible to both agents and operators
- the same planner prompt can serve as a before/after acceptance check

## Problem Statement

Today, an agent-session login can expose multiple claimable addresses, for example:

- a role mailbox like `planner@mail4agent.dogfood`
- a session mailbox like `session_dogfood@mail4agent.dogfood`

The current client-side inbox inference only auto-selects when there is exactly one default claim address. In this dogfood profile that means:

- the session is valid
- the mailbox identity is known
- but `thread-summaries` and `mark-thread-read` still need an explicit `--to-address`

This is awkward for the medium dogfood launcher because the prompt intentionally starts with:

1. `python .\client.py whoami`
2. `python .\client.py thread-summaries --limit 10`

So the current behavior creates avoidable recovery work in the very first user-visible read path.

## Proposed Change

Introduce a session-scoped **default inbox address** and use it consistently for thread-state surfaces.

### Runtime / Server

- Extend the in-memory agent-session payload with `default_inbox_address`.
- For role-based logins, prefer the role mailbox as the default inbox when both:
  - a role mailbox is claimable
  - a session mailbox is also claimable
- Surface `default_inbox_address` in `GET /whoami`.

### HTTP Semantics

- `GET /thread-summaries`
  - for `agent_session` callers, allow `to_address` to be omitted when `default_inbox_address` is available
- `POST /mark-thread-read`
  - for `agent_session` callers, allow `to_address` to be omitted when `default_inbox_address` is available
- If no unambiguous session default exists, return a clear error instead of a generic missing-field failure.

### Client / CLI

- Teach `codex_mailbox_client.py` to prefer `session.default_inbox_address` when inferring inbox context.
- Keep `--to-address` override support unchanged.
- Preserve current explicit behavior for admin and harness-token callers.

## Why This Is Better Than A Broader Change

This target is intentionally tighter than “general CLI ergonomics”:

- no schema migration
- no UI work
- no auth-policy expansion
- no message delivery semantics change

It only improves how already-authenticated session callers address thread-state surfaces.

## Expected Code Touch Points

- [sqlite_mailbox.py](/E:/agent_misc/mail4agent/sqlite_mailbox.py)
  - derive and persist `default_inbox_address` inside the session payload builder
- [sqlite_mailbox_http.py](/E:/agent_misc/mail4agent/sqlite_mailbox_http.py)
  - include the new field in `whoami`
  - infer thread-state target mailbox for session callers when omitted
- [codex_mailbox_client.py](/E:/agent_misc/mail4agent/codex_mailbox_client.py)
  - prefer `default_inbox_address` in `_effective_inbox_address()`
- [client.py](/E:/agent_misc/mail4agent/client.py)
  - mostly documentation/help-text alignment, not a large parser change
- `test/`
  - add a repo-local regression that proves `thread-summaries` and `mark-thread-read` work without explicit `to_address` after session login

## Suggested Acceptance

### Repo-Local

- A logged-in planner session can run:
  - `python .\client.py thread-summaries --limit 10`
  - `python .\client.py mark-thread-read --thread-id <THREAD_ID>`
  without `--to-address`
- The same commands still allow explicit `--to-address`
- Harness-token and admin-token semantics are unchanged

### Dogfood Drill

- Re-run the same medium planner prompt unchanged
- `thread-summaries --limit 10` should succeed on the first try
- No recovery step with `--to-address planner@mail4agent.dogfood` should be needed
- After restart and re-login, the same thread-state commands should still work without explicit addressing

## Recommended Drill Shape

Use the existing [dogfood_feedback_update_drill.py](/E:/agent_misc/mail4agent/dogfood_feedback_update_drill.py) flow as the envelope, but split the next run into an explicit before/after:

1. Run current dogfood kickoff and capture the initial planner failure/recovery.
2. Pause at the maintenance step.
3. Apply the bounded `default_inbox_address` change.
4. Restart on the same DB.
5. Re-login planner/reviewer.
6. Send a post-update operator check.
7. Re-run planner and confirm the unchanged prompt now succeeds without explicit mailbox targeting.

## What Success Would Mean

If this works, we gain two things at once:

- a real product improvement for session-scoped mailbox UX
- a much stronger maintenance drill, because the drill will validate an actual behavior change instead of only restart continuity

That makes this the best next bounded server-side update for the dogfood line.
