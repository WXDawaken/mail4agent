# Dogfood Feedback Server Update Drill

## Goal

- Turn the feedback-driven maintenance scenario into a repeatable operator drill.
- Provide a repo-local automation entrypoint so the same sequence can be rerun without manual step stitching.
- Keep the drill mailbox-native from start to finish:
  - workload enters through mailbox
  - feedback is delivered through mailbox
  - maintenance notice is delivered through mailbox
  - post-update verification also uses mailbox traffic

## Automation Entry Point

- Script: [dogfood_feedback_update_drill.py](/E:/agent_misc/mail4agent/dogfood_feedback_update_drill.py)
- Default runtime: `E:\agent_misc\mail4agent\.tmp_dogfood_update_drill`
- Default command:

```powershell
python .\dogfood_feedback_update_drill.py
```

- First successful run report: [dogfood-feedback-server-update-report-20260324.md](/E:/agent_misc/mail4agent/docs/dogfood-feedback-server-update-report-20260324.md)
- Follow-on thread-defaults report: [dogfood-feedback-server-update-thread-defaults-report-20260324.md](/E:/agent_misc/mail4agent/docs/dogfood-feedback-server-update-thread-defaults-report-20260324.md)

The automation entrypoint now also performs a stricter session-only check:

- it runs planner thread-state commands with only `MAILBOX_SESSION_TOKEN`
- it does not rely on `MAILBOX_CONFIG`, `MAILBOX_INBOX_ADDRESS`, or `MAILBOX_FROM_ADDRESS`
- after restart it also verifies `mark-thread-read` works the same way

## Baseline Assumptions

- Repo: `E:\agent_misc\mail4agent`
- Server: `python .\sqlite_mailbox_http.py --db .\.tmp_dogfood\mailbox.sqlite --host 127.0.0.1 --port 8787`
- Admin token: `dogfood-admin-token`
- Runtime bootstrap already available through `dogfood_smoke_bootstrap.py`
- Medium Codex launchers already available through `launch_dogfood_medium_agent.ps1`

## Chosen First Drill Theme

- Feedback topic: session-scoped CLI ergonomics still require too much explicit targeting
- Scope: bounded runtime/server/CLI improvement only
- Out of scope: admin UI, Rust migration, large schema redesign

## Role Addresses

- operator: `operator@mail4agent.dogfood`
- planner: `planner@mail4agent.dogfood`
- reviewer: `reviewer@mail4agent.dogfood`

## Drill Sequence

1. Start the server and bootstrap the runtime.
2. Send the kickoff operator task to the planner.
3. Run planner once.
4. Run reviewer once.
5. Have reviewer send explicit feedback to the operator mailbox.
6. Operator reads and acknowledges the feedback.
7. Operator sends a maintenance notice to planner and reviewer.
8. Back up the SQLite database.
9. Stop the server, apply one bounded server update, restart on the same DB.
10. Planner and reviewer re-login from the stored harness token.
11. Resume the same thread or send one follow-up operator check message.
12. Run post-update operator checks.

## Step 1: Start and Bootstrap

Start the server:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\sqlite_mailbox_http.py --db .\.tmp_dogfood\mailbox.sqlite --host 127.0.0.1 --port 8787
```

Bootstrap runtime assets in another shell:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\dogfood_smoke_bootstrap.py
```

## Step 2: Kickoff Operator Task

Send the initial operator message:

```powershell
python .\client.py send `
  --admin-token dogfood-admin-token `
  --from-address operator@mail4agent.dogfood `
  --to-address planner@mail4agent.dogfood `
  --payload-json '{"task":"triage","request":"Summarize this inbox item, then ask reviewer for a short verdict. If reviewer finds a product or ergonomics issue, have them report it back to operator."}'
```

## Step 3: Run Planner and Reviewer

Planner:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_medium_agent.ps1 planner
```

Reviewer:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_medium_agent.ps1 reviewer
```

## Step 4: Reviewer Feedback Message

If reviewer identifies a bounded product/runtime issue, send the feedback explicitly to operator on the same thread.

Recommended payload template:

```json
{
  "kind": "dogfood_feedback",
  "source_role": "reviewer",
  "observed_behavior": "thread-summaries still required an explicit to-address instead of using the active session mailbox cleanly",
  "expected_behavior": "session-scoped thread-summaries should work without extra mailbox targeting when the default inbox is already known",
  "surface": "mixed",
  "suggested_scope": "bounded runtime/server/CLI fix",
  "severity": "medium"
}
```

Suggested command shape:

```powershell
python .\client.py send `
  --to-address operator@mail4agent.dogfood `
  --thread-id <THREAD_ID> `
  --in-reply-to-message-id <REVIEWER_MESSAGE_ID> `
  --payload-json '{...feedback payload...}'
```

## Step 5: Operator Reads Feedback

Inspect the operator mailbox:

```powershell
python .\client.py thread-summaries --admin-token dogfood-admin-token --to-address operator@mail4agent.dogfood --limit 10
python .\client.py --format text thread --admin-token dogfood-admin-token --thread-id <THREAD_ID>
```

Operator acknowledgement template:

```json
{
  "kind": "maintenance_ack",
  "ok": true,
  "accepted_feedback": true,
  "planned_action": "bounded server update",
  "note": "Server will restart; clients must re-login after the update."
}
```

## Step 6: Maintenance Notice Through Mailbox

Send a maintenance notice to planner:

```powershell
python .\client.py send `
  --admin-token dogfood-admin-token `
  --from-address operator@mail4agent.dogfood `
  --to-address planner@mail4agent.dogfood `
  --thread-id <THREAD_ID> `
  --payload-json '{"kind":"maintenance_notice","message":"Server will restart for a bounded update. Current session tokens will expire. Re-run client.py login after restart."}'
```

Send the same notice to reviewer:

```powershell
python .\client.py send `
  --admin-token dogfood-admin-token `
  --from-address operator@mail4agent.dogfood `
  --to-address reviewer@mail4agent.dogfood `
  --thread-id <THREAD_ID> `
  --payload-json '{"kind":"maintenance_notice","message":"Server will restart for a bounded update. Current session tokens will expire. Re-run client.py login after restart."}'
```

## Step 7: Backup and Restart

Back up the database before the update:

```powershell
Copy-Item .\.tmp_dogfood\mailbox.sqlite .\.tmp_dogfood\mailbox.pre_update.sqlite -Force
```

Then:

1. stop the server
2. apply the bounded server change
3. restart the server with the same DB path and admin token

## Step 8: Re-Login Recovery Check

Planner re-login:

```powershell
$env:MAILBOX_CONFIG = ".\.tmp_dogfood\planner.mailbox_client.json"
$env:MAILBOX_TOKEN = (Get-Content .\.tmp_dogfood\harness.token -Raw).Trim()
python .\client.py login --output token --project-id mail4agent --role planner --session dogfood --agent-name dogfood-planner
```

Reviewer re-login:

```powershell
$env:MAILBOX_CONFIG = ".\.tmp_dogfood\reviewer.mailbox_client.json"
$env:MAILBOX_TOKEN = (Get-Content .\.tmp_dogfood\harness.token -Raw).Trim()
python .\client.py login --output token --project-id mail4agent --role reviewer --session dogfood --agent-name dogfood-reviewer
```

Expected outcome:

- old session token no longer works
- stored harness token still works
- fresh login succeeds

## Step 9: Post-Update Verification

Operator checks:

```powershell
python .\client.py whoami --admin-token dogfood-admin-token
python .\client.py --format text thread --admin-token dogfood-admin-token --thread-id <THREAD_ID>
python .\client.py thread-summaries --admin-token dogfood-admin-token --to-address planner@mail4agent.dogfood --limit 10
python .\client.py thread-summaries --admin-token dogfood-admin-token --to-address reviewer@mail4agent.dogfood --limit 10
python .\client.py retry-queue --admin-token dogfood-admin-token --project-id mail4agent --limit 10
```

Optional follow-up operator message to confirm post-update behavior:

```powershell
python .\client.py send `
  --admin-token dogfood-admin-token `
  --from-address operator@mail4agent.dogfood `
  --to-address planner@mail4agent.dogfood `
  --thread-id <THREAD_ID> `
  --payload-json '{"kind":"post_update_check","request":"Confirm whether the targeted behavior now matches expectation."}'
```

## Pass Criteria

- Reviewer feedback reaches operator through mailbox.
- Operator maintenance notice reaches planner and reviewer through mailbox.
- Server restarts on the same database.
- Harness token survives restart.
- Session token requires refresh after restart.
- Same thread remains readable after restart.
- Post-update mailbox traffic succeeds.
- Retry queue stays empty or explainable.

## Notes

- This drill is intentionally small. The first success criterion is operational continuity, not maximum feature scope.
- The same pattern can later be extended into schema-touching upgrades, compatibility drills, or benchmarkable maintenance tasks.
