# Dogfood Feedback Update Report: Thread Defaults

Date: `2026-03-24`

## Outcome

- The second mailbox-native maintenance drill passed end to end on the canonical repo.
- Runtime entrypoint: [dogfood_feedback_update_drill.py](/E:/agent_misc/mail4agent/dogfood_feedback_update_drill.py)
- Drill summary JSON: [feedback_update_drill_summary.json](/E:/agent_misc/mail4agent/.tmp_dogfood_update_drill/feedback_update_drill_summary.json)
- This run validated the promoted session-scoped `default_inbox_address` behavior together with restart and re-login continuity.

## What Was Different From The First Drill

- The canonical server now exposes `session.default_inbox_address` through `GET /whoami`.
- The drill script now performs explicit session-token-only checks without relying on:
  - `MAILBOX_CONFIG`
  - `MAILBOX_INBOX_ADDRESS`
  - `MAILBOX_FROM_ADDRESS`
- Those checks call:
  - `python .\client.py whoami`
  - `python .\client.py thread-summaries --limit 10`
  - `python .\client.py mark-thread-read --thread-id <THREAD_ID>`

## Key Evidence

- Shared thread id: `3b65def1-410f-4647-89b6-756c3092d1cb`
- Kickoff message id: `7fc76793-995e-46de-91ff-515f903e5c97`
- Backup DB: [mailbox.pre_update.sqlite](/E:/agent_misc/mail4agent/.tmp_dogfood_update_drill/mailbox.pre_update.sqlite)

### Pre-Restart Session-Only Check

- `planner_whoami.session.default_inbox_address = planner@mail4agent.dogfood`
- `thread-summaries --limit 10` succeeded with only the session token
- No explicit `--to-address` was required

### Post-Restart Session-Only Check

- Old planner and reviewer session tokens both failed with `401`
- Harness-token re-login succeeded for both roles
- The fresh planner session again reported:
  - `default_inbox_address = planner@mail4agent.dogfood`
- `thread-summaries --limit 10` succeeded again with only the fresh session token
- `mark-thread-read --thread-id 3b65def1-410f-4647-89b6-756c3092d1cb` also succeeded without explicit mailbox targeting
- A follow-up `thread-summaries --limit 10` call showed the same thread with `unread: false`

## Planner / Reviewer Behavior

- The planner launcher's own mailbox flow now naturally starts with:
  - `python .\client.py whoami`
  - `python .\client.py thread-summaries --limit 10`
- In this drill, that first planner `thread-summaries` call succeeded directly and the planner output explicitly reported:
  - `default inbox planner@mail4agent.dogfood`
- Reviewer and post-update planner traffic both completed normally on the same thread.

## Pass Readout

- Feedback was mailbox-native.
- Maintenance notices were mailbox-native.
- Server restart on the same DB succeeded.
- Old session tokens were invalid after restart.
- Harness-token login still worked after restart.
- Session-token-only thread-state commands worked before and after restart.
- Same thread remained readable and writable after restart.
- Final retry queue was empty.

## Why This Matters

This is the first maintenance drill that validates a real product improvement, not only continuity:

- the product behavior is better for logged-in planner/reviewer sessions
- the maintenance envelope still works across restart and re-login

That makes the current canonical baseline strong enough to treat mailbox-native dogfood plus bounded maintenance rehearsal as a real operational path, not just a benchmark artifact.
