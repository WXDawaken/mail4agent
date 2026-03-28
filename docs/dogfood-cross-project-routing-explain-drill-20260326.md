# Cross-Project Routing-Explain Drill

## Goal

Run the first concrete cross-project dogfood drill:

- `consumer_app` asks `shared_tools` for a bounded integration answer
- `shared_tools` handles the request through mailbox oncall
- the whole interaction stays on one mailbox thread

This first drill keeps the request type small:

- `request_kind = routing_explain`

Latest completed run report:

- [E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-report-20260326.md](E:\agent_misc\mail4agent\docs\dogfood-cross-project-routing-explain-report-20260326.md)

## Baseline

- Repo: [E:\agent_misc\mail4agent](E:\agent_misc\mail4agent)
- Bootstrap: [E:\agent_misc\mail4agent\dogfood_cross_project_bootstrap.py](E:\agent_misc\mail4agent\dogfood_cross_project_bootstrap.py)
- Oncall supervisor: [E:\agent_misc\mail4agent\mailbox_oncall.py](E:\agent_misc\mail4agent\mailbox_oncall.py)
- Oncall launcher: [E:\agent_misc\mail4agent\launch_dogfood_oncall_agent.ps1](E:\agent_misc\mail4agent\launch_dogfood_oncall_agent.ps1)
- Scenario note: [E:\agent_misc\mail4agent\docs\dogfood-cross-project-supply-demand-scenario-20260326.md](E:\agent_misc\mail4agent\docs\dogfood-cross-project-supply-demand-scenario-20260326.md)

## One-Time Setup Per Server Run

Start the local server:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\sqlite_mailbox_http.py --db .\.tmp_dogfood_cross_project\mailbox.sqlite --host 127.0.0.1 --port 8787
```

Bootstrap the cross-project runtime:

```powershell
$env:MAILBOX_ADMIN_TOKEN = "dogfood-admin-token"
python .\dogfood_cross_project_bootstrap.py
```

That writes runtime assets under `.tmp_dogfood_cross_project\`:

- `harness.token`
- `planner.mailbox_client.json`
- `integrator.mailbox_client.json`
- `operator.mailbox_client.json`
- `reviewer.mailbox_client.json`
- `planner.preview.json`
- `integrator.preview.json`
- `operator.preview.json`
- `reviewer.preview.json`
- `bootstrap_summary.json`

## Step 1: Consumer Planner Sends The Request

Open a shell in [E:\agent_misc\mail4agent](E:\agent_misc\mail4agent):

```powershell
$token = (Get-Content .\.tmp_dogfood_cross_project\harness.token -Raw).Trim()
$env:MAILBOX_TOKEN = $token
$env:MAILBOX_CONFIG = (Resolve-Path .\.tmp_dogfood_cross_project\planner.mailbox_client.json)
$env:MAILBOX_SESSION_TOKEN = (python .\client.py login --output token --project-id consumer_app --role planner --session consumer-dogfood --agent-name consumer-planner).Trim()
Remove-Item Env:MAILBOX_TOKEN
python .\client.py whoami
```

Then send one bounded request into the supplier intake mailbox:

```powershell
python .\client.py send `
  --to-address intake@shared_tools.dogfood `
  --subject "routing explain request" `
  --message-type integration_request `
  --payload-json '{"request_kind":"routing_explain","request_id":"req-routing-001","consumer_project_id":"consumer_app","supplier_project_id":"shared_tools","target":{"from":"planner@consumer_app.dogfood","to":"intake@shared_tools.dogfood"},"constraints":["keep the answer machine-readable","no broad server changes"],"acceptance_checks":["say whether the route is allowed","cite the practical route class","keep the answer short"]}'
```

Save the returned `thread_id` and `message_id`.

## Step 2: Supplier Oncall Handles The Delivery

Run the operator oncall supervisor once:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood_cross_project --consumer-id shared-tools-intake-oncall-1
```

This uses the shared-tools operator runtime config, which logs the child into `shared_tools` and claims the `intake@shared_tools.dogfood` mailbox.

The expected result is:

- one delivery claimed
- one operator reply on the same thread
- supervisor ack
- no retry entry

## Step 3: Consumer Planner Reads The Supplier Reply

Back in the consumer shell:

```powershell
python .\client.py --format text thread --message-id <INITIAL_MESSAGE_ID>
python .\client.py thread-summaries --limit 10
python .\client.py inbox --thread-id <THREAD_ID> --limit 10
```

The planner should now see the supplier reply on the same thread.

## Step 4: Consumer Sends Acceptance

Reply back to the supplier intake mailbox using the same thread:

```powershell
python .\client.py send `
  --to-address intake@shared_tools.dogfood `
  --subject "routing explain accepted" `
  --message-type integration_acceptance `
  --thread-id <THREAD_ID> `
  --in-reply-to-message-id <SUPPLIER_REPLY_MESSAGE_ID> `
  --payload-json '{"request_kind":"routing_explain","accepted":true,"notes":["answer was clear","routing scope looked correct"]}'
```

This closes the first real cross-project request / delivery / acceptance loop.

## Optional Retry Path

To exercise retry behavior, temporarily make the supplier task intentionally fail and rerun the supervisor:

1. send a request that the current prompt cannot safely handle
2. let the child exit non-zero
3. inspect:

```powershell
python .\client.py retry-queue --admin-token dogfood-admin-token --project-id shared_tools --limit 10
```

4. rerun:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood_cross_project --consumer-id shared-tools-intake-oncall-2
```

## What This Drill Proves

- explicit cross-project routing is enough for real request/response work
- mailbox threads are viable for supplier-style service interaction
- supplier-side oncall can process one bounded request without a long-lived interactive session
- request, reply, and acceptance can all stay mailbox-native

## Current Limits

- the first drill still uses a human-driven consumer send and acceptance step
- the supplier side is operator-only for now
- the request type is intentionally small and text-first
- cross-project routing here is project/type scoped, not exact-address scoped
