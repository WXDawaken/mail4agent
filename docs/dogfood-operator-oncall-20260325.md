# Dogfood Operator Oncall

## Goal

- Let a small supervisor process watch the operator mailbox, claim one bounded delivery, launch a fresh Codex operator run, and ack or nack based on the child exit code.
- Keep `client.py` as the generic mailbox CLI while moving role-aware orchestration into a separate script.

## Entry Points

- Supervisor: [mailbox_oncall.py](/E:/agent_misc/mail4agent/mailbox_oncall.py)
- Role launcher: [launch_dogfood_oncall_agent.ps1](/E:/agent_misc/mail4agent/launch_dogfood_oncall_agent.ps1)
- Operator prompt: [dogfood-high-operator-oncall-prompt.txt](/E:/agent_misc/mail4agent/docs/dogfood-high-operator-oncall-prompt.txt)
- Shared worker primitive: [mailbox_worker.py](/E:/agent_misc/mail4agent/mailbox_worker.py)

## Current Scope

- First version is operator-only.
- The supervisor defaults to one once-off attempt.
- `--watch` turns it into a polling worker.
- The child operator run handles exactly one already-claimed delivery.

## Commands

Run one once-off operator oncall attempt:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood
```

Run a longer-lived watch loop:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood --watch
```

Override the claim target explicitly when needed:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood --to-address operator@mail4agent.dogfood
```

Or resolve a session-style address from the bootstrap summary:

```powershell
python .\mailbox_oncall.py --role operator --runtime-dir .tmp_dogfood --session dogfood
```

## Flow

1. The supervisor loads `harness.token`, `bootstrap_summary.json`, and `operator.mailbox_client.json`.
2. It logs in lazily through the normal client config path.
3. It claims one delivery and keeps the lease alive with heartbeats.
4. It launches a fresh Codex operator run with the claimed delivery JSON on stdin.
5. The child operator replies through mailbox but does not ack or nack.
6. The supervisor acks exit code `0` and nacks any non-zero exit code.

## First Smoke Result

- A temporary local server on `127.0.0.1:8797` was used to validate the full loop.
- The supervisor claimed one `operator_update` delivery, launched a `high` Codex operator run, and the child sent exactly one mailbox reply on the claimed thread.
- The supervisor acked the delivery and the final retry queue was empty.
- The launcher now stages runtime config and claimed-delivery JSON into repo-local `.tmp_dogfood_live\` so Codex sandboxed runs can read them even when the actual runtime dir lives outside the workspace.

## Notes

- Use stable role or group mailbox addresses for the main oncall route.
- Session or custom local-part addresses are supported, but only after they are normalized into concrete mailbox addresses.
- This is intentionally separate from `client.py` so the main CLI stays protocol-focused.
