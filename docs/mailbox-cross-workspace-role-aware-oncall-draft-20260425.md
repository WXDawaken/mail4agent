# Cross-Workspace Role-Aware Oncall Draft

Date: `2026-04-25`

Status: draft

## Summary

`oncall` should stay a runtime supervision capability, not a role.

The role-aware cross-workspace design should be:

```text
mailbox delivery -> role binding -> workspace assignment -> worker binding
```

The stable worker identity should be:

```text
(role, workspace_dir, thread_id)
```

Within one role-specific worker pool, `(workspace_dir, thread_id)` is enough. Across roles, `role` must be part of the key so `plugin_dev`, `core_dev`, `salvage_run_dev`, and `game_engine_dev` never accidentally reuse the same slow-model context just because they are on the same mailbox thread.

## Existing Building Blocks

The current codebase already has most of the lower layers:

- `mailbox_oncall.py` defines role-aware CLI assembly, but currently fixes one `--role` per process.
- `mailbox_oncall_server.py` is the watch-first entrypoint, but still delegates to the same single-role path.
- `oncall_supervisor.py` already supports per-delivery workspace resolution and thread assignment.
- `oncall_registry.py` already persists per-thread state and `bindings_by_workspace`.
- `oncall_exec_app_server.py` already keeps reusable app-server workers and can separate workers for the same mailbox thread across different child workspaces.

The missing layer is a workspace-root oncall router that can load role bindings and route each claimed delivery to the right role, workspace, executor, and worker key.

## Terms

### Role

A role is an ownership and behavior boundary.

Examples:

- `operator`
- `plugin_dev`
- `core_dev`
- `salvage_run_dev`
- `game_engine_dev`

A role owns prompt text, mailbox identity, default reasoning effort, write-scope guidance, and sometimes model/backend preferences.

### Oncall Registration

An oncall registration says that a role is currently supervised for a workspace root.

It is not a role by itself.

Example:

```text
plugin_dev is on call for E:\agent_misc\subagent_lab\anchor_agent
```

### Workspace Root

The workspace root is the largest directory tree one oncall server is allowed to supervise.

Child workspaces under that root do not need separate oncall servers unless they require different runtime trust, credentials, or lifecycle policy.

### Worker Binding

A worker binding maps a logical task context to a live or recoverable execution backend.

Recommended binding key:

```text
role::<normalized_workspace_dir>::thread::<thread_id>
```

Mailbox address can be retained in registry metadata, but role should still be explicit because future shared intake addresses may not map one-to-one to roles.

## Goals

- Keep mailbox server as the durable fact and routing layer.
- Keep oncall server as the supervision and backend lifecycle layer.
- Support one workspace-root oncall server with multiple role bindings.
- Support child workspace routing without starting one oncall server per child workspace.
- Preserve point-to-point mailbox delivery rather than turning ordinary threads into group chat.
- Reuse app-server workers only when role, workspace, and thread all match.
- Make workspace routing explicit enough for audits and smoke tests.

## Non-Goals

- Do not make `oncall` a synthetic role such as `oncall_dev`.
- Do not put role prompt/config resolution into the mailbox server.
- Do not require a broadcast/group-thread model for ordinary cross-role work.
- Do not make textual DSL orchestration mandatory; JSON mailbox payloads remain the backend path.
- Do not turn `mail4agent` into a full workflow engine.

## Proposed Manifest

Introduce an oncall manifest that belongs to the oncall server/runtime directory, not the mailbox server.

Example:

```json
{
  "workspace_root_dir": "E:/agent_misc/subagent_lab",
  "mailbox_client_path": "E:/agent_misc/mail4agent/client.py",
  "runtime_dir": "E:/agent_misc/mail4agent/.tmp_dogfood_live",
  "default_backend": "app-server",
  "roles": {
    "salvage_run_dev": {
      "inbox_address": "salvage_run_dev@subagent_lab.dogfood",
      "config_file": "salvage_run_dev.mailbox_client.json",
      "prompt_file": "docs/salvage-run-dev-oncall-prompt.txt",
      "default_workspace_dir": ".",
      "allowed_workspace_globs": ["."]
    },
    "game_engine_dev": {
      "inbox_address": "game_engine_dev@subagent_lab.dogfood",
      "config_file": "game_engine_dev.mailbox_client.json",
      "prompt_file": "docs/game-engine-dev-oncall-prompt.txt",
      "default_workspace_dir": ".",
      "allowed_workspace_globs": ["."]
    },
    "plugin_dev": {
      "inbox_address": "plugin_dev@anchor_agent.dogfood",
      "config_file": "plugin_dev.mailbox_client.json",
      "prompt_file": "docs/anchor-agent-plugin-dev-oncall-prompt.txt",
      "default_workspace_dir": "anchor_agent",
      "allowed_workspace_globs": ["anchor_agent/**"]
    },
    "core_dev": {
      "inbox_address": "core_dev@anchor_agent.dogfood",
      "config_file": "core_dev.mailbox_client.json",
      "prompt_file": "docs/anchor-agent-core-dev-oncall-prompt.txt",
      "default_workspace_dir": "anchor_agent",
      "allowed_workspace_globs": ["anchor_agent/**"]
    }
  }
}
```

Manifest notes:

- `inbox_address` is the primary role routing key.
- `default_workspace_dir` is resolved relative to `workspace_root_dir`.
- `allowed_workspace_globs` constrains explicit delivery workspace hints.
- `config_file` stays runtime-local because mailbox sessions and tokens are runtime facts.
- `prompt_file` is resolved under the target workspace root unless explicitly absolute.
- Model and reasoning settings can be added here later, or derived from `.codex/agents/<role>.toml` when present.

## Routing Rules

### Role Resolution

Resolve role from delivery destination first:

```text
delivery.to -> RoleBinding.inbox_address -> role
```

Optional payload fields such as `role` may be accepted only as consistency checks. They should not override the address binding by default.

If no role binding matches:

- do not spawn a worker
- record `role_resolution_error`
- preferably send a bounded deferred/system reply if a safe reply address is available
- ack rather than retry if the failure is clearly non-transient configuration drift

### Workspace Resolution

Resolve workspace in this order:

1. explicit delivery workspace hint, such as `payload.workspace_dir`, `payload.workspace.root`, or `metadata.repo_root`
2. existing thread registry binding for the same `(role, thread_id)` if it still exists
3. role binding `default_workspace_dir`
4. error

Every resolved workspace must be inside `workspace_root_dir` and match the role's allowed workspace policy.

If a workspace hint points outside the allowed root:

- do not spawn a worker
- record `workspace_resolution_error`
- avoid retry loops for deterministic policy failures

### Worker Resolution

Resolve worker by:

```text
role + normalized_workspace_dir + thread_id
```

Then:

- reuse the live worker only if backend liveness probe succeeds
- replace stale `running` bindings after lease expiry
- cold-start with bounded handoff summary if the previous worker is gone
- preserve prior per-workspace bindings for audit even after replacement

## Runtime Flow

1. Load the oncall manifest.
2. Build an address-to-role map from all role bindings.
3. Claim deliveries for the union of all configured role inbox addresses.
4. For each delivery, resolve `role` from `to_address`.
5. Load the role-specific registry state for `(role, mailbox_address, thread_id)`.
6. Resolve `workspace_dir` from delivery hint, registry, or role default.
7. Build `worker_binding_key = role::workspace::thread`.
8. Select or create the role-specific executor.
9. Execute the claimed delivery with the role prompt, role config, workspace env, and recovered context.
10. Record result metadata under both role-level summary and thread-level registry.
11. Ack/nack according to the existing supervisor exit-code policy, with deterministic config errors treated as non-retryable once a safe reply or registry entry has been written.

## Registry Shape

The registry should make role explicit even if mailbox addresses are currently unique.

Recommended thread key:

```text
role::<safe_mailbox_address>::thread::<safe_thread_id>
```

Recommended persisted fields:

```json
{
  "role": "plugin_dev",
  "mailbox_address": "plugin_dev@anchor_agent.dogfood",
  "thread_id": "thread_123",
  "workspace_dir": "E:/agent_misc/subagent_lab/anchor_agent",
  "workspace_root_dir": "E:/agent_misc/subagent_lab",
  "worker_binding_key": "plugin_dev::e:/agent_misc/subagent_lab/anchor_agent::thread_123",
  "worker_id": "app-server-thread-...",
  "backend_name": "app-server",
  "supports_worker_reuse": true,
  "reused_worker": true,
  "task_status": "waiting_on_peer",
  "handoff_summary": "bounded continuity summary"
}
```

For backward compatibility, the first implementation can keep the existing thread file path and add role to the binding key. Before supporting shared intake addresses, migrate the path or add a role namespace to avoid collisions.

## Implementation Slices

### Slice 1: Manifest Parser

Add `oncall_role_registry.py`.

Responsibilities:

- load and validate the manifest
- normalize workspace paths
- build `address -> role` and `role -> binding` maps
- expose safe role lookup and workspace policy checks

Tests:

- duplicate inbox addresses fail
- unknown role lookup fails cleanly
- relative workspace defaults resolve under root
- absolute workspace outside root fails

### Slice 2: Role-Aware Worker Key

Update thread assignment metadata to include role in worker binding keys.

Expected behavior:

- same role, same workspace, same thread may reuse worker
- same role, different workspace, same thread must not reuse worker
- different role, same workspace, same thread must not reuse worker

### Slice 3: Multi-Role Oncall Server

Extend `mailbox_oncall_server.py` with a manifest mode:

```powershell
python .\mailbox_oncall_server.py --role-manifest .tmp_dogfood_live\oncall_roles.json --backend app-server --watch
```

The first version can run one role supervisor per role in the same process. A later version can centralize the claim loop over the union of addresses if that reduces duplicated polling.

### Slice 4: Executor Role Overrides

Stop relying on module-level `ROLE_SPECS` inside `oncall_exec_app_server.py` for manifest-driven runs.

Instead pass a resolved role binding into the executor:

```text
role
config_file
prompt_file
default_workspace_dir
reasoning_effort
backend
```

The current global role specs can remain as a compatibility fallback for existing CLI calls.

### Slice 5: Cross-Workspace Smoke

Run an isolated smoke with:

- temporary runtime dir
- temporary `CODEX_HOME`
- two child workspaces under one root
- one mailbox thread with three deliveries:
  - role A in workspace A
  - role A in workspace B
  - role A in workspace A again
- expected result: A1 and A3 reuse the same worker, B gets a different worker

Then add a role separation smoke:

- same mailbox thread
- same workspace
- role A then role B
- expected result: no worker reuse across roles

## Suggested API Shape

Minimal dataclasses:

```python
@dataclass(frozen=True)
class OncallRoleBinding:
    role: str
    inbox_address: str
    config_file: str
    prompt_file: str
    default_workspace_dir: Path
    allowed_workspace_globs: tuple[str, ...]
    backend: str
    reasoning_effort: str

@dataclass(frozen=True)
class OncallWorkspaceManifest:
    workspace_root_dir: Path
    runtime_dir: Path
    mailbox_client_path: Path
    roles: dict[str, OncallRoleBinding]
    addresses: dict[str, str]
```

Possible resolver call:

```python
role_binding = manifest.resolve_role(delivery["to"])
workspace = manifest.resolve_workspace(role_binding, delivery, existing_thread_state)
worker_key = build_worker_binding_key(role_binding.role, workspace, delivery["thread_id"])
```

## Interaction With Thread Semantics

This draft does not change mailbox thread semantics.

Ordinary threads remain causal point-to-point chains. A thread may contain messages to different role mailboxes, but each delivery still has one concrete target. The oncall router only decides which role runtime handles that target.

If a future workload needs true many-party synchronization, that should stay in the separate `group_round` layer:

- round is locked while waiting for participant acks
- participants may ack without replying
- timeout can close a round
- ordinary mailbox delivery semantics remain unchanged

## Open Questions

- Should deterministic role/workspace policy failures send a mailbox-visible deferred reply directly from the supervisor, or only record registry failure and ack?
- Should role model settings come from the manifest, `.codex/agents/<role>.toml`, or both with a strict precedence rule?
- Should the registry path be migrated immediately to include role, or only when shared intake addresses become necessary?
- Should multi-role watch mode run one supervisor loop per role first, or should it implement a single central claim loop from the start?
- Should workspace hints be accepted from arbitrary payload locations, or only from a small documented metadata envelope?

## Recommendation

Implement the manifest parser and role-aware worker key first. That is the smallest useful change and gives tests a solid contract.

Then extend `mailbox_oncall_server.py` to run multiple role bindings in one workspace-root process while preserving the existing single-role CLI path.

Do not move role resolution into the mailbox server. The mailbox server should continue to own durable messages, claims, sessions, protocol metadata, and routing facts. The oncall server should own role prompt/config selection, workspace resolution, and backend worker lifecycle.
