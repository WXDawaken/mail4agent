# Dogfood Operator Update Flow

## Goal

- Let a real mailbox-native operator agent receive one bounded repo-local task, make a small change in `E:\agent_misc\mail4agent`, validate it, and reply through mailbox.
- Keep planner and reviewer as medium-effort readers, while giving operator a slightly heavier reasoning budget for bounded maintenance or product follow-up work.

## Baseline

- Repo: `E:\agent_misc\mail4agent`
- Bootstrap entrypoint: [dogfood_smoke_bootstrap.py](/E:/agent_misc/mail4agent/dogfood_smoke_bootstrap.py)
- Generic launcher: [launch_dogfood_agent.ps1](/E:/agent_misc/mail4agent/launch_dogfood_agent.ps1)
- Operator prompt: [dogfood-high-operator-prompt.txt](/E:/agent_misc/mail4agent/docs/dogfood-high-operator-prompt.txt)

## Runtime Assets

After running `python .\dogfood_smoke_bootstrap.py`, `.tmp_dogfood\` now contains:

- `harness.token`
- `operator.mailbox_client.json`
- `planner.mailbox_client.json`
- `reviewer.mailbox_client.json`
- `operator.preview.json`
- `planner.preview.json`
- `reviewer.preview.json`
- `bootstrap_summary.json`

The operator profile is mailbox-native:

- login uses the stored harness token
- the operator session targets `local_part = operator`
- the mailbox type is `group`
- the launcher exports `MAILBOX_HARNESS_ID`, `MAILBOX_PROJECT_ID`, and `MAILBOX_TOKEN` before explicit login

## Role Effort Policy

- planner: `gpt-5.4` with `medium`
- reviewer: `gpt-5.4` with `medium`
- operator: `gpt-5.4` with `high`

That mapping is the default behavior inside [launch_dogfood_agent.ps1](/E:/agent_misc/mail4agent/launch_dogfood_agent.ps1).

## Launch Commands

Planner:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 planner
```

Reviewer:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 reviewer
```

Operator:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_dogfood_agent.ps1 operator
```

The legacy [launch_dogfood_medium_agent.ps1](/E:/agent_misc/mail4agent/launch_dogfood_medium_agent.ps1) remains as a compatibility wrapper, but the generic launcher is now the preferred entry point.

## Recommended Operator Task Shape

Send bounded repo-local change requests to `operator@mail4agent.dogfood`.

Suggested payload shape:

```json
{
  "kind": "operator_update",
  "scope": "bounded_server_change",
  "request": "Make one small mailbox-server improvement in the canonical repo.",
  "acceptance": [
    "keep the change bounded",
    "run focused validation",
    "reply with changed files and validation"
  ]
}
```

Suggested command:

```powershell
python .\client.py send `
  --admin-token dogfood-admin-token `
  --from-address operator@mail4agent.dogfood `
  --to-address operator@mail4agent.dogfood `
  --payload-json '{...operator update payload...}'
```

## Expected Operator Behavior

- read one operator delivery
- inspect the mailbox thread
- make only the bounded repo-local change asked for
- run focused validation
- reply with machine-readable status
- ack the delivery

Recommended reply payload shape:

```json
{
  "ok": true,
  "handled_by": "operator",
  "changed_files": [
    "sqlite_mailbox_http.py",
    "client.py"
  ],
  "validation": [
    "python -m unittest test.test_session_logout_and_expiry_introspection -v"
  ],
  "notes": [
    "kept scope bounded to runtime and CLI"
  ]
}
```

## Out Of Scope

- broad refactors
- admin UI redesign
- multi-delivery queue draining
- automatic git commit unless the mailbox task explicitly requests it
