from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from codex_mailbox_client import MailboxHTTPClient
from mailbox_worker import ConsumeConfig, IdlePollCallback, run_consume_loop
from oncall_registry import OncallRegistry


@dataclass(frozen=True)
class ClaimedDeliveryExecutionResult:
    exit_code: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ClaimedDeliveryExecutionContext:
    delivery: dict[str, Any]
    thread_assignment: dict[str, Any]
    existing_thread_state: dict[str, Any] | None = None


ClaimedDeliveryExecutor = Callable[[ClaimedDeliveryExecutionContext], ClaimedDeliveryExecutionResult]
WorkerReuseProbe = Callable[[str], bool]
WorkspaceResolver = Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]


@dataclass(frozen=True)
class OncallSupervisorConfig:
    role: str
    watch: bool
    runtime_dir: Path
    consumer_id: str
    claim_addresses: tuple[str, ...]
    execution_metadata: dict[str, Any]
    consume_config: ConsumeConfig
    started_at: str
    can_reuse_worker: WorkerReuseProbe | None = None
    on_idle: IdlePollCallback | None = None
    resolve_workspace: WorkspaceResolver | None = None


def run_oncall_supervisor(
    client: MailboxHTTPClient,
    config: OncallSupervisorConfig,
    registry: OncallRegistry,
    execute_claimed_delivery: ClaimedDeliveryExecutor,
) -> dict[str, Any]:
    registry.mark_started(
        consumer_id=config.consumer_id,
        started_at=config.started_at,
    )
    last_delivery = _empty_last_delivery()
    last_execution_metadata = dict(config.execution_metadata)
    active_thread_state: dict[str, Any] | None = None
    last_thread_assignment: dict[str, Any] = {}

    def _handle_delivery(delivery: dict[str, Any]) -> int:
        nonlocal active_thread_state
        nonlocal last_execution_metadata
        nonlocal last_thread_assignment
        delivery_state = _capture_last_delivery(
            delivery,
            lease_seconds=config.consume_config.lease_seconds,
        )
        last_delivery.update(delivery_state)
        existing_thread_state = registry.load_thread_state(
            mailbox_address=str(delivery_state["last_to_address"] or ""),
            thread_id=str(delivery_state["last_thread_id"] or ""),
        )
        workspace_assignment = _resolve_workspace_assignment(
            execution_metadata=config.execution_metadata,
            delivery=delivery,
            delivery_state=delivery_state,
            existing_thread_state=existing_thread_state,
            resolve_workspace=config.resolve_workspace,
        )
        last_thread_assignment = _resolve_thread_assignment(
            execution_metadata=config.execution_metadata,
            existing_thread_state=existing_thread_state,
            delivery_state=delivery_state,
            workspace_assignment=workspace_assignment,
            can_reuse_worker=config.can_reuse_worker,
        )
        active_thread_state = _build_thread_state(
            config=config,
            delivery_state=delivery_state,
            execution_metadata=config.execution_metadata,
            thread_assignment=last_thread_assignment,
        )
        registry.record_thread_state(
            {
                **active_thread_state,
                "completed_at": delivery_state.get("last_claimed_at"),
            },
            status="running",
        )
        execution_result = execute_claimed_delivery(
            ClaimedDeliveryExecutionContext(
                delivery=delivery,
                thread_assignment=dict(last_thread_assignment),
                existing_thread_state=existing_thread_state,
            )
        )
        last_execution_metadata = {
            **dict(config.execution_metadata),
            **dict(execution_result.metadata),
        }
        last_thread_assignment = _merge_thread_assignment_with_execution_metadata(
            thread_assignment=last_thread_assignment,
            execution_metadata=last_execution_metadata,
        )
        active_thread_state = _build_thread_state(
            config=config,
            delivery_state=delivery_state,
            execution_metadata=last_execution_metadata,
            thread_assignment=last_thread_assignment,
        )
        return int(execution_result.exit_code)

    def _on_delivery_complete(
        delivery: dict[str, Any],
        return_code: int,
        completion_status: str,
    ) -> None:
        nonlocal active_thread_state
        if active_thread_state is None:
            active_thread_state = _build_thread_state(
                config=config,
                delivery_state=_capture_last_delivery(
                    delivery,
                    lease_seconds=config.consume_config.lease_seconds,
                ),
                execution_metadata=last_execution_metadata,
                thread_assignment=last_thread_assignment,
            )
        registry.record_thread_state(
            {
                **active_thread_state,
                "last_exit_code": return_code,
                "completed_at": _utc_now(),
            },
            status=completion_status,
        )
        active_thread_state = None

    try:
        payload = run_consume_loop(
            client,
            config.consume_config,
            _handle_delivery,
            on_delivery_complete=_on_delivery_complete,
            on_idle=config.on_idle,
        )
        result = {
            **payload,
            **last_delivery,
            **last_execution_metadata,
            **last_thread_assignment,
            "ok": True,
            "role": config.role,
            "watch": config.watch,
            "runtime_dir": str(config.runtime_dir),
            "summary_file": str(registry.summary_path),
            "registry_file": str(registry.registry_path),
            "consumer_id": config.consumer_id,
            "claim_addresses": list(config.claim_addresses),
            "lease_seconds": config.consume_config.lease_seconds,
            "started_at": config.started_at,
            "completed_at": _utc_now(),
        }
        registry.record_completion(result)
        return result
    except Exception as exc:
        failure = {
            **last_delivery,
            **last_execution_metadata,
            **last_thread_assignment,
            "ok": False,
            "role": config.role,
            "watch": config.watch,
            "runtime_dir": str(config.runtime_dir),
            "summary_file": str(registry.summary_path),
            "registry_file": str(registry.registry_path),
            "consumer_id": config.consumer_id,
            "claim_addresses": list(config.claim_addresses),
            "lease_seconds": config.consume_config.lease_seconds,
            "started_at": config.started_at,
            "completed_at": _utc_now(),
            "error": str(exc),
        }
        registry.record_failure(failure)
        registry.record_thread_state(failure, status="failed")
        raise


def _empty_last_delivery() -> dict[str, Any]:
    return {
        "last_delivery_id": None,
        "last_message_id": None,
        "last_processed_message_id": None,
        "last_thread_id": None,
        "last_from_address": None,
        "last_to_address": None,
        "last_claimed_at": None,
        "lease_until": None,
    }


def _capture_last_delivery(
    delivery: dict[str, Any],
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    claimed_at = _utc_now()
    return {
        "last_delivery_id": _optional_int(delivery.get("delivery_id")),
        "last_message_id": _optional_str(delivery.get("message_id")),
        "last_processed_message_id": _optional_str(delivery.get("message_id")),
        "last_thread_id": _optional_str(delivery.get("thread_id")),
        "last_from_address": _optional_str(delivery.get("from")),
        "last_to_address": _optional_str(delivery.get("to")),
        "last_claimed_at": claimed_at,
        "lease_until": _lease_until(claimed_at, lease_seconds),
    }


def _build_thread_state(
    *,
    config: OncallSupervisorConfig,
    delivery_state: dict[str, Any],
    execution_metadata: dict[str, Any],
    thread_assignment: dict[str, Any],
) -> dict[str, Any]:
    return {
        **delivery_state,
        **dict(execution_metadata),
        **dict(thread_assignment),
        "role": config.role,
        "consumer_id": config.consumer_id,
        "runtime_dir": str(config.runtime_dir),
        "mailbox_address": delivery_state.get("last_to_address"),
        "thread_id": delivery_state.get("last_thread_id"),
        "lease_seconds": config.consume_config.lease_seconds,
        "started_at": config.started_at,
    }


def _resolve_thread_assignment(
    *,
    execution_metadata: dict[str, Any],
    existing_thread_state: dict[str, Any] | None,
    delivery_state: dict[str, Any],
    workspace_assignment: dict[str, Any],
    can_reuse_worker: WorkerReuseProbe | None = None,
) -> dict[str, Any]:
    worker_kind = _metadata_str(execution_metadata, "worker_kind") or _metadata_str(execution_metadata, "execution_mode") or "worker"
    backend_name = _metadata_str(execution_metadata, "backend_name") or _metadata_str(execution_metadata, "execution_mode") or "unknown"
    supports_worker_reuse = bool(execution_metadata.get("supports_worker_reuse") is True)
    workspace_thread_state = _workspace_thread_state(
        existing_thread_state=existing_thread_state,
        workspace_assignment=workspace_assignment,
    )
    workspace_resolution_error = _assignment_str(workspace_assignment, "workspace_resolution_error")
    if workspace_resolution_error is not None:
        workspace_thread_state = None
    previous_worker_id = _state_str(workspace_thread_state, "worker_id")
    reused_worker = False
    recovery_reason: str | None = None
    worker_id = _metadata_str(execution_metadata, "worker_id")
    reusable_worker_id, unreusable_reason = _reusable_worker_id(
        existing_thread_state=workspace_thread_state,
        worker_kind=worker_kind,
    )

    if worker_id is None:
        if workspace_resolution_error is not None:
            worker_id = _new_worker_id(worker_kind)
            recovery_reason = "workspace_resolution_error"
        elif supports_worker_reuse and reusable_worker_id is not None:
            if can_reuse_worker is None or _safe_can_reuse_worker(can_reuse_worker, reusable_worker_id):
                worker_id = reusable_worker_id
                reused_worker = True
            else:
                worker_id = _new_worker_id(worker_kind)
                recovery_reason = "previous_worker_not_available"
        else:
            worker_id = _new_worker_id(worker_kind)
            recovery_reason = _recovery_reason(
                existing_thread_state=existing_thread_state,
                workspace_thread_state=workspace_thread_state,
                workspace_assignment=workspace_assignment,
                supports_worker_reuse=supports_worker_reuse,
                unreusable_reason=unreusable_reason,
            )

    return {
        **dict(workspace_assignment),
        "backend_name": backend_name,
        "worker_kind": worker_kind,
        "worker_id": worker_id,
        "previous_worker_id": previous_worker_id,
        "supports_worker_reuse": supports_worker_reuse,
        "reused_worker": reused_worker,
        "recovery_reason": recovery_reason,
        "binding_started_at": delivery_state.get("last_claimed_at"),
    }


def _merge_thread_assignment_with_execution_metadata(
    *,
    thread_assignment: dict[str, Any],
    execution_metadata: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(thread_assignment)
    for key in ("backend_name", "worker_kind", "worker_id"):
        value = _metadata_str(execution_metadata, key)
        if value is not None:
            merged[key] = value
    if execution_metadata.get("supports_worker_reuse") is True:
        merged["supports_worker_reuse"] = True
    return merged


def _reusable_worker_id(
    *,
    existing_thread_state: dict[str, Any] | None,
    worker_kind: str,
) -> tuple[str | None, str | None]:
    if not existing_thread_state:
        return None, None
    if _state_str(existing_thread_state, "worker_kind") != worker_kind:
        return None, "previous_binding_not_reusable"
    worker_id = _state_str(existing_thread_state, "worker_id")
    if worker_id is None:
        return None, "previous_binding_not_reusable"
    status = _state_str(existing_thread_state, "status")
    if status not in {"running", "acked", "retry_pending"}:
        return None, "previous_binding_not_reusable"
    if status == "running" and _is_stale_running_binding(existing_thread_state):
        return None, "replaced_stale_running_binding"
    return worker_id, None


def _safe_can_reuse_worker(can_reuse_worker: WorkerReuseProbe, worker_id: str) -> bool:
    try:
        return bool(can_reuse_worker(worker_id))
    except Exception:
        return False


def _recovery_reason(
    *,
    existing_thread_state: dict[str, Any] | None,
    workspace_thread_state: dict[str, Any] | None,
    workspace_assignment: dict[str, Any],
    supports_worker_reuse: bool,
    unreusable_reason: str | None,
) -> str | None:
    if _assignment_str(workspace_assignment, "workspace_resolution_error") is not None:
        return "workspace_resolution_error"
    if not existing_thread_state:
        return None
    if supports_worker_reuse and workspace_thread_state is None and _assignment_str(workspace_assignment, "workspace_dir") is not None:
        return "workspace_binding_not_found"
    if not supports_worker_reuse:
        return "backend_does_not_support_worker_reuse"
    if unreusable_reason is not None:
        return unreusable_reason
    return "previous_binding_not_reusable"


def _is_stale_running_binding(existing_thread_state: dict[str, Any] | None) -> bool:
    if not existing_thread_state:
        return False
    if _state_str(existing_thread_state, "status") != "running":
        return False
    lease_until = _state_str(existing_thread_state, "lease_until")
    if lease_until is None:
        return False
    parsed = _parse_timestamp(lease_until)
    return parsed is not None and parsed < datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _new_worker_id(worker_kind: str) -> str:
    prefix = "".join(char if char.isalnum() else "-" for char in worker_kind).strip("-") or "worker"
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _assignment_str(assignment: dict[str, Any], key: str) -> str | None:
    value = assignment.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _state_str(state: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _resolve_workspace_assignment(
    *,
    execution_metadata: dict[str, Any],
    delivery: dict[str, Any],
    delivery_state: dict[str, Any],
    existing_thread_state: dict[str, Any] | None,
    resolve_workspace: WorkspaceResolver | None,
) -> dict[str, Any]:
    assignment = dict(resolve_workspace(delivery, existing_thread_state) or {}) if resolve_workspace is not None else {}
    workspace_dir = (
        _assignment_str(assignment, "workspace_dir")
        or _metadata_str(execution_metadata, "workspace_dir")
        or _state_str(existing_thread_state, "workspace_dir")
    )
    workspace_root_dir = (
        _assignment_str(assignment, "workspace_root_dir")
        or _metadata_str(execution_metadata, "workspace_root_dir")
        or _metadata_str(execution_metadata, "workspace_dir")
        or workspace_dir
    )
    if workspace_dir is not None:
        assignment["workspace_dir"] = workspace_dir
    if workspace_root_dir is not None:
        assignment["workspace_root_dir"] = workspace_root_dir
    if _assignment_str(assignment, "workspace_source") is None:
        if resolve_workspace is not None and workspace_dir is not None:
            assignment["workspace_source"] = "resolved_workspace"
        elif workspace_dir is not None:
            assignment["workspace_source"] = "execution_metadata.workspace_dir"
    if _assignment_str(assignment, "worker_binding_key") is None and workspace_dir is not None:
        mailbox_address = _optional_str(delivery_state.get("last_to_address"))
        thread_id = _optional_str(delivery_state.get("last_thread_id"))
        if mailbox_address is not None and thread_id is not None:
            assignment["worker_binding_key"] = _worker_binding_key(
                mailbox_address=mailbox_address,
                thread_id=thread_id,
                workspace_dir=workspace_dir,
            )
    return assignment


def _workspace_thread_state(
    *,
    existing_thread_state: dict[str, Any] | None,
    workspace_assignment: dict[str, Any],
) -> dict[str, Any] | None:
    if not existing_thread_state:
        return None
    workspace_dir = _assignment_str(workspace_assignment, "workspace_dir")
    if workspace_dir is None:
        return existing_thread_state
    normalized_workspace_key = _workspace_key(workspace_dir)
    bindings_by_workspace = existing_thread_state.get("bindings_by_workspace")
    if isinstance(bindings_by_workspace, dict):
        binding = bindings_by_workspace.get(normalized_workspace_key)
        if isinstance(binding, dict):
            return binding
    existing_workspace_dir = _state_str(existing_thread_state, "workspace_dir")
    if existing_workspace_dir is None:
        return existing_thread_state
    if _workspace_key(existing_workspace_dir) == normalized_workspace_key:
        return existing_thread_state
    return None


def _worker_binding_key(*, mailbox_address: str, thread_id: str, workspace_dir: str) -> str:
    return f"{mailbox_address.strip()}::{thread_id.strip()}::{_workspace_key(workspace_dir)}"


def _workspace_key(workspace_dir: str) -> str:
    return workspace_dir.strip().replace("\\", "/").lower()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lease_until(claimed_at: str, lease_seconds: int) -> str | None:
    if not claimed_at:
        return None
    claimed_at_dt = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    return (claimed_at_dt + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
