from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OncallRegistryPaths:
    runtime_dir: Path
    role: str
    summary_path: Path
    registry_path: Path


class OncallRegistry:
    def __init__(self, paths: OncallRegistryPaths) -> None:
        self._paths = paths

    @classmethod
    def create(
        cls,
        *,
        runtime_dir: Path,
        role: str,
        raw_summary_file: str | None = None,
    ) -> OncallRegistry:
        if raw_summary_file:
            summary_path = Path(raw_summary_file)
            if not summary_path.is_absolute():
                summary_path = runtime_dir / summary_path
        else:
            summary_path = runtime_dir / ".oncall" / f"{role}-last-run.json"
        registry_path = runtime_dir / ".oncall" / "registry" / f"{role}.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(
            OncallRegistryPaths(
                runtime_dir=runtime_dir.resolve(),
                role=role,
                summary_path=summary_path.resolve(),
                registry_path=registry_path.resolve(),
            )
        )

    @property
    def role(self) -> str:
        return self._paths.role

    @property
    def runtime_dir(self) -> Path:
        return self._paths.runtime_dir

    @property
    def summary_path(self) -> Path:
        return self._paths.summary_path

    @property
    def registry_path(self) -> Path:
        return self._paths.registry_path

    @property
    def thread_registry_dir(self) -> Path:
        return self.runtime_dir / ".oncall" / "registry" / "threads"

    def load_registry_entry(self) -> dict[str, Any] | None:
        return _read_json_file(self.registry_path)

    def load_summary(self) -> dict[str, Any] | None:
        return _read_json_file(self.summary_path)

    def mark_started(
        self,
        *,
        consumer_id: str | None,
        started_at: str,
    ) -> dict[str, Any]:
        payload = self._build_registry_payload(
            consumer_id=consumer_id,
            started_at=started_at,
            completed_at=None,
            status="running",
            last_delivery_id=None,
            last_thread_id=None,
        )
        _write_json_file(self.registry_path, payload)
        return payload

    def record_completion(self, summary: dict[str, Any]) -> dict[str, Any]:
        _write_json_file(self.summary_path, summary)
        payload = self._build_registry_payload(
            consumer_id=_optional_str(summary, "consumer_id"),
            started_at=_optional_str(summary, "started_at"),
            completed_at=_optional_str(summary, "completed_at"),
            status="succeeded",
            last_delivery_id=_optional_int(summary, "last_delivery_id"),
            last_thread_id=_optional_str(summary, "last_thread_id"),
        )
        _write_json_file(self.registry_path, payload)
        return payload

    def record_failure(self, summary: dict[str, Any]) -> dict[str, Any]:
        _write_json_file(self.summary_path, summary)
        payload = self._build_registry_payload(
            consumer_id=_optional_str(summary, "consumer_id"),
            started_at=_optional_str(summary, "started_at"),
            completed_at=_optional_str(summary, "completed_at"),
            status="failed",
            last_delivery_id=_optional_int(summary, "last_delivery_id"),
            last_thread_id=_optional_str(summary, "last_thread_id"),
        )
        _write_json_file(self.registry_path, payload)
        return payload

    def thread_registry_path(self, *, mailbox_address: str, thread_id: str) -> Path:
        mailbox_key = _safe_filename_fragment(mailbox_address)
        thread_key = _safe_filename_fragment(thread_id)
        return (self.thread_registry_dir / f"{mailbox_key}__{thread_key}.json").resolve()

    def load_thread_state(self, *, mailbox_address: str, thread_id: str) -> dict[str, Any] | None:
        return _read_json_file(
            self.thread_registry_path(
                mailbox_address=mailbox_address,
                thread_id=thread_id,
            )
        )

    def list_thread_states(self) -> list[dict[str, Any]]:
        if not self.thread_registry_dir.exists():
            return []
        payloads: list[dict[str, Any]] = []
        for path in sorted(self.thread_registry_dir.glob("*.json")):
            payload = _read_json_file(path)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def inspect_state(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "runtime_dir": str(self.runtime_dir),
            "summary_file": str(self.summary_path),
            "registry_file": str(self.registry_path),
            "summary": self.load_summary(),
            "role_registry": self.load_registry_entry(),
            "threads": self.list_thread_states(),
        }

    def record_thread_state(
        self,
        summary: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any] | None:
        mailbox_address = _optional_str(summary, "mailbox_address") or _optional_str(summary, "last_to_address")
        thread_id = _optional_str(summary, "thread_id") or _optional_str(summary, "last_thread_id")
        if not mailbox_address or not thread_id:
            return None

        path = self.thread_registry_path(
            mailbox_address=mailbox_address,
            thread_id=thread_id,
        )
        existing_payload = _read_json_file(path) or {}
        payload = {
            "role": self.role,
            "consumer_id": _optional_str(summary, "consumer_id"),
            "runtime_dir": str(self.runtime_dir),
            "summary_path": str(self.summary_path),
            "mailbox_address": mailbox_address,
            "thread_id": thread_id,
            "backend_name": _optional_str(summary, "backend_name")
            or _optional_str(summary, "execution_mode"),
            "worker_kind": _optional_str(summary, "worker_kind")
            or _optional_str(summary, "execution_mode"),
            "worker_id": _optional_str(summary, "worker_id")
            or _optional_str(summary, "consumer_id"),
            "previous_worker_id": _optional_str(summary, "previous_worker_id"),
            "status": status,
            "supports_worker_reuse": _optional_bool(summary, "supports_worker_reuse"),
            "reused_worker": _optional_bool(summary, "reused_worker"),
            "recovery_reason": _optional_str(summary, "recovery_reason"),
            "binding_started_at": _optional_str(summary, "binding_started_at")
            or _optional_str(summary, "last_claimed_at"),
            "lease_until": _optional_str(summary, "lease_until"),
            "last_seen_at": _optional_str(summary, "completed_at")
            or _optional_str(summary, "last_claimed_at")
            or _optional_str(summary, "started_at"),
            "last_delivery_id": _optional_int(summary, "last_delivery_id"),
            "last_processed_message_id": _optional_str(summary, "last_processed_message_id")
            or _optional_str(summary, "last_message_id"),
            "last_exit_code": _optional_int(summary, "last_exit_code"),
            "handoff_summary": _bounded_optional_str(summary, "handoff_summary"),
            "last_message_path": _optional_str(summary, "last_message_path"),
            "handler_cwd": _optional_str(summary, "handler_cwd"),
            "workspace_dir": _optional_str(summary, "workspace_dir"),
            "workspace_root_dir": _optional_str(summary, "workspace_root_dir"),
            "workspace_source": _optional_str(summary, "workspace_source"),
            "requested_workspace_dir": _optional_str(summary, "requested_workspace_dir"),
            "workspace_resolution_error": _optional_str(summary, "workspace_resolution_error"),
            "worker_binding_key": _optional_str(summary, "worker_binding_key"),
            "codex_home_dir": _optional_str(summary, "codex_home_dir"),
            "prompt_path": _optional_str(summary, "prompt_path"),
            "runtime_delivery_path": _optional_str(summary, "runtime_delivery_path"),
            "mailbox_client_path": _optional_str(summary, "mailbox_client_path"),
            "app_server_thread_id": _optional_str(summary, "app_server_thread_id"),
            "app_server_turn_id": _optional_str(summary, "app_server_turn_id"),
            "app_server_turn_status": _optional_str(summary, "app_server_turn_status"),
            "recovered_handoff_summary_used": _optional_bool(summary, "recovered_handoff_summary_used"),
            "recovered_recovery_reason": _optional_str(summary, "recovered_recovery_reason"),
            "recovered_last_processed_message_id": _optional_str(summary, "recovered_last_processed_message_id"),
            "recovered_previous_worker_id": _optional_str(summary, "recovered_previous_worker_id"),
            "reasoning_effort": _optional_str(summary, "reasoning_effort"),
        }
        bindings_by_workspace = _load_workspace_bindings(existing_payload)
        workspace_binding = _build_workspace_binding_payload(payload)
        workspace_dir = _optional_str(payload, "workspace_dir")
        if workspace_dir is not None and workspace_binding is not None:
            bindings_by_workspace[_workspace_key(workspace_dir)] = workspace_binding
        payload["bindings_by_workspace"] = bindings_by_workspace
        payload["workspace_binding_count"] = len(bindings_by_workspace)
        _write_json_file(path, payload)
        return payload

    def _build_registry_payload(
        self,
        *,
        consumer_id: str | None,
        started_at: str | None,
        completed_at: str | None,
        status: str,
        last_delivery_id: int | None,
        last_thread_id: str | None,
    ) -> dict[str, Any]:
        return {
            "role": self.role,
            "consumer_id": consumer_id,
            "runtime_dir": str(self.runtime_dir),
            "last_run_path": str(self.summary_path),
            "last_delivery_id": last_delivery_id,
            "last_thread_id": last_thread_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
        }


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _load_workspace_bindings(existing_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bindings_by_workspace: dict[str, dict[str, Any]] = {}
    existing_bindings = existing_payload.get("bindings_by_workspace")
    if isinstance(existing_bindings, dict):
        for key, value in existing_bindings.items():
            if isinstance(key, str) and isinstance(value, dict):
                bindings_by_workspace[key] = dict(value)
    legacy_binding = _build_workspace_binding_payload(existing_payload)
    legacy_workspace_dir = _optional_str(existing_payload, "workspace_dir")
    if legacy_binding is not None and legacy_workspace_dir is not None:
        bindings_by_workspace.setdefault(_workspace_key(legacy_workspace_dir), legacy_binding)
    return bindings_by_workspace


def _build_workspace_binding_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    workspace_dir = _optional_str(payload, "workspace_dir")
    if workspace_dir is None:
        return None
    return {
        "workspace_dir": workspace_dir,
        "workspace_root_dir": _optional_str(payload, "workspace_root_dir"),
        "workspace_source": _optional_str(payload, "workspace_source"),
        "requested_workspace_dir": _optional_str(payload, "requested_workspace_dir"),
        "workspace_resolution_error": _optional_str(payload, "workspace_resolution_error"),
        "worker_binding_key": _optional_str(payload, "worker_binding_key"),
        "backend_name": _optional_str(payload, "backend_name"),
        "worker_kind": _optional_str(payload, "worker_kind"),
        "worker_id": _optional_str(payload, "worker_id"),
        "previous_worker_id": _optional_str(payload, "previous_worker_id"),
        "status": _optional_str(payload, "status"),
        "supports_worker_reuse": _optional_bool(payload, "supports_worker_reuse"),
        "reused_worker": _optional_bool(payload, "reused_worker"),
        "recovery_reason": _optional_str(payload, "recovery_reason"),
        "binding_started_at": _optional_str(payload, "binding_started_at"),
        "lease_until": _optional_str(payload, "lease_until"),
        "last_seen_at": _optional_str(payload, "last_seen_at"),
        "last_delivery_id": _optional_int(payload, "last_delivery_id"),
        "last_processed_message_id": _optional_str(payload, "last_processed_message_id"),
        "last_exit_code": _optional_int(payload, "last_exit_code"),
        "handoff_summary": _bounded_optional_str(payload, "handoff_summary"),
        "last_message_path": _optional_str(payload, "last_message_path"),
        "handler_cwd": _optional_str(payload, "handler_cwd"),
        "codex_home_dir": _optional_str(payload, "codex_home_dir"),
        "prompt_path": _optional_str(payload, "prompt_path"),
        "runtime_delivery_path": _optional_str(payload, "runtime_delivery_path"),
        "mailbox_client_path": _optional_str(payload, "mailbox_client_path"),
        "app_server_thread_id": _optional_str(payload, "app_server_thread_id"),
        "app_server_turn_id": _optional_str(payload, "app_server_turn_id"),
        "app_server_turn_status": _optional_str(payload, "app_server_turn_status"),
        "recovered_handoff_summary_used": _optional_bool(payload, "recovered_handoff_summary_used"),
        "recovered_recovery_reason": _optional_str(payload, "recovered_recovery_reason"),
        "recovered_last_processed_message_id": _optional_str(payload, "recovered_last_processed_message_id"),
        "recovered_previous_worker_id": _optional_str(payload, "recovered_previous_worker_id"),
        "reasoning_effort": _optional_str(payload, "reasoning_effort"),
    }


def _bounded_optional_str(
    payload: dict[str, Any],
    key: str,
    *,
    max_chars: int = 1200,
) -> str | None:
    value = _optional_str(payload, key)
    if value is None:
        return None
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 14].rstrip() + " [truncated]"


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return None


def _workspace_key(workspace_dir: str) -> str:
    return workspace_dir.strip().replace("\\", "/").lower()


def _safe_filename_fragment(value: str, *, fallback: str = "unknown") -> str:
    pieces: list[str] = []
    last_was_separator = False
    for char in value.strip():
        if char.isalnum() or char in {".", "-"}:
            pieces.append(char)
            last_was_separator = False
            continue
        if not last_was_separator:
            pieces.append("_")
            last_was_separator = True
    result = "".join(pieces).strip("._")
    return result or fallback


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload
