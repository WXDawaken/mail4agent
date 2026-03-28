from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from codex_mailbox_client import DEFAULT_MAILBOX_BASE_URL, MailboxClientConfig, MailboxHTTPClient, MailboxHTTPError
from mailbox_language_cache import ProtocolRuntimeDiskCache
from mailbox_language_runtime import (
    MailboxRuntimeError,
    compile_protocol_runtime_schema,
    format_protocol_ref,
    normalize_protocol_component,
    parse_protocol_ref,
)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    line_number = 0
    for raw_line in sys.stdin:
        line_number += 1
        stripped = raw_line.strip()
        if not stripped:
            continue
        response = _handle_request_line(stripped, args=args, line_number=line_number)
        print(json.dumps(response, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mailbox language JSONL stdio interpreter")
    parser.add_argument("--base-url", help="mailbox server base URL for run requests")
    parser.add_argument("--admin-token", help="admin token for run requests")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="HTTP timeout for run requests")
    parser.add_argument("--cache-dir", help="optional local protocol compile cache directory")
    return parser


def _handle_request_line(raw_line: str, *, args: argparse.Namespace, line_number: int) -> dict[str, Any]:
    request_id: Any = None
    command: str | None = None
    artifact_kind: str | None = None
    try:
        request = json.loads(raw_line)
        if not isinstance(request, dict):
            raise ValueError("request line must decode to a JSON object")
        request_id = request.get("id")
        command = _require_string(request, "command")
        artifact = _require_object(request, "artifact")
        artifact_kind = _require_string(artifact, "kind")
        result = _execute_request(
            request=request,
            command=command,
            artifact=artifact,
            artifact_kind=artifact_kind,
            args=args,
        )
        return {
            "ok": True,
            "id": request_id,
            "command": command,
            "artifact_kind": artifact_kind,
            **result,
        }
    except MailboxRuntimeError as exc:
        return _error_response(
            request_id=request_id,
            command=command,
            artifact_kind=artifact_kind,
            error=str(exc),
            error_code=exc.code,
            line_number=line_number,
        )
    except MailboxHTTPError as exc:
        error_code = None
        if isinstance(exc.payload, dict):
            payload_code = exc.payload.get("error_code")
            if isinstance(payload_code, str) and payload_code.strip():
                error_code = payload_code.strip()
        return _error_response(
            request_id=request_id,
            command=command,
            artifact_kind=artifact_kind,
            error=str(exc),
            error_code=error_code,
            line_number=line_number,
            status=exc.status,
            payload=exc.payload if isinstance(exc.payload, dict) else None,
        )
    except Exception as exc:  # pragma: no cover - fallback safety net
        return _error_response(
            request_id=request_id,
            command=command,
            artifact_kind=artifact_kind,
            error=str(exc) or exc.__class__.__name__,
            line_number=line_number,
        )


def _execute_request(
    *,
    request: dict[str, Any],
    command: str,
    artifact: dict[str, Any],
    artifact_kind: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if command not in {"check", "lower", "run"}:
        raise ValueError("command must be one of: check, lower, run")
    if artifact_kind == "protocol_schema":
        return _handle_protocol_schema_request(command=command, artifact=artifact, request=request, args=args)
    if artifact_kind == "mailbox_binding":
        return _handle_mailbox_binding_request(command=command, artifact=artifact, request=request, args=args)
    if artifact_kind == "message_envelope":
        return _handle_message_envelope_request(command=command, artifact=artifact, request=request, args=args)
    if artifact_kind == "handoff_event":
        return _handle_handoff_event_request(command=command, artifact=artifact, request=request, args=args)
    raise ValueError("artifact.kind must be one of: protocol_schema, mailbox_binding, message_envelope, handoff_event")


def _handle_protocol_schema_request(
    *,
    command: str,
    artifact: dict[str, Any],
    request: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    protocol_name, protocol_version = parse_protocol_ref(_require_string(artifact, "protocol"))
    schema = _require_object(artifact, "schema")
    compiled_payload = _compile_protocol_schema(
        protocol_name=protocol_name,
        protocol_version=protocol_version,
        schema=schema,
        cache_dir=_resolve_cache_dir(request=request, args=args),
    )
    protocol_ref = format_protocol_ref(protocol_name, protocol_version)
    if command == "check":
        return {
            "protocol": protocol_ref,
            "validated": True,
            "cache_hit": compiled_payload["cache_hit"],
            "source_sha256": compiled_payload["source_sha256"],
            "cache_path": compiled_payload.get("cache_path"),
        }
    if command == "lower":
        return {
            "protocol": protocol_ref,
            "cache_hit": compiled_payload["cache_hit"],
            "source_sha256": compiled_payload["source_sha256"],
            "cache_path": compiled_payload.get("cache_path"),
            "artifact": compiled_payload["artifact"],
        }
    client = _build_admin_client(request=request, args=args)
    result = client.register_protocol(protocol=protocol_ref, schema=schema)
    return {
        "protocol": protocol_ref,
        "cache_hit": compiled_payload["cache_hit"],
        "source_sha256": compiled_payload["source_sha256"],
        "cache_path": compiled_payload.get("cache_path"),
        "registered": result,
    }


def _handle_mailbox_binding_request(
    *,
    command: str,
    artifact: dict[str, Any],
    request: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    address = _require_string(artifact, "address")
    accepts_raw = artifact.get("accepts")
    if not isinstance(accepts_raw, list) or not accepts_raw:
        raise ValueError("artifact.accepts must be a non-empty list of protocol refs")
    accepts: list[str] = []
    seen: set[str] = set()
    for item in accepts_raw:
        protocol_name, protocol_version = parse_protocol_ref(_require_non_empty_string(item, "accepts item"))
        protocol_ref = format_protocol_ref(protocol_name, protocol_version)
        if protocol_ref in seen:
            continue
        seen.add(protocol_ref)
        accepts.append(protocol_ref)
    default_protocol = artifact.get("default_protocol")
    normalized_default: str | None = None
    if default_protocol is not None:
        protocol_name, protocol_version = parse_protocol_ref(_require_non_empty_string(default_protocol, "default_protocol"))
        normalized_default = format_protocol_ref(protocol_name, protocol_version)
        if normalized_default not in accepts:
            raise ValueError("default_protocol must also appear in accepts")
    lowered = {
        "address": address,
        "accepts": accepts,
        "default_protocol": normalized_default,
    }
    if command in {"check", "lower"}:
        return {"artifact": lowered, "validated": True}
    client = _build_admin_client(request=request, args=args)
    result = client.set_mailbox_protocols(
        address=address,
        accepts=accepts,
        default_protocol=normalized_default,
    )
    return {"artifact": lowered, "bindings": result}


def _handle_message_envelope_request(
    *,
    command: str,
    artifact: dict[str, Any],
    request: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    lowered = _lower_message_envelope_artifact(artifact)
    if command in {"check", "lower"}:
        return {"artifact": lowered, "validated": True}
    client = _build_admin_client(request=request, args=args)
    result = client.execute_message_envelope(
        from_address=lowered["from_address"],
        envelope=lowered["envelope"],
        subject=lowered.get("subject"),
        reply_to_address=lowered.get("reply_to_address"),
        correlation_id=lowered.get("correlation_id"),
        workflow_id=lowered.get("workflow_id"),
        idempotency_key=lowered.get("idempotency_key"),
        headers=lowered.get("headers"),
        deliver_after_seconds=int(lowered.get("deliver_after_seconds", 0)),
        expires_in_seconds=lowered.get("expires_in_seconds"),
        max_attempts=int(lowered.get("max_attempts", 8)),
        message_type=lowered.get("message_type"),
    )
    return {"artifact": lowered, "result": result}


def _handle_handoff_event_request(
    *,
    command: str,
    artifact: dict[str, Any],
    request: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    lowered = _lower_handoff_event_artifact(artifact)
    if command in {"check", "lower"}:
        return {"artifact": lowered, "validated": True}
    client = _build_admin_client(request=request, args=args)
    result = client.execute_handoff_event(
        event=lowered["event"],
        actor=lowered.get("actor"),
    )
    return {"artifact": lowered, "handoff": result}


def _compile_protocol_schema(
    *,
    protocol_name: str,
    protocol_version: str,
    schema: dict[str, Any],
    cache_dir: Path | None,
) -> dict[str, Any]:
    if cache_dir is not None:
        cached = ProtocolRuntimeDiskCache(cache_dir).load_or_compile(
            protocol_name=protocol_name,
            protocol_version=protocol_version,
            schema=schema,
        )
        return {
            "artifact": cached.artifact,
            "cache_hit": cached.cache_hit,
            "cache_path": str(cached.cache_path),
            "source_sha256": cached.source_sha256,
        }
    compiled = compile_protocol_runtime_schema(
        schema,
        protocol_name=protocol_name,
        protocol_version=protocol_version,
    )
    protocol_ref = format_protocol_ref(protocol_name, protocol_version)
    return {
        "artifact": compiled,
        "cache_hit": False,
        "cache_path": None,
        "source_sha256": f"nocache:{protocol_ref}",
    }


def _lower_message_envelope_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    op = str(artifact.get("op") or "send").strip()
    if op not in {"send", "spawn"}:
        raise ValueError("artifact.op must be one of: send, spawn")
    protocol_name, protocol_version = parse_protocol_ref(_require_string(artifact, "protocol"))
    message = normalize_protocol_component(_require_string(artifact, "message"), "msg_type")
    payload = _require_object(artifact, "payload")
    from_address = _require_string(artifact, "from_address")
    to_address = _require_string(artifact, "to_address")
    thread_id = _optional_string(artifact, "thread_id")
    parent_thread_id = _optional_string(artifact, "parent_thread_id")
    if op == "spawn":
        if parent_thread_id is None:
            raise ValueError("spawn artifacts require parent_thread_id")
        if thread_id is not None:
            raise ValueError("spawn artifacts must not provide thread_id")
        target_kind = "mailbox"
    else:
        target_kind = "thread" if thread_id is not None else "mailbox"
        if parent_thread_id is not None:
            raise ValueError("parent_thread_id is only valid for spawn artifacts")

    envelope: dict[str, Any] = {
        "op": op,
        "target_kind": target_kind,
        "protocol": protocol_name,
        "version": protocol_version,
        "msg_type": message,
        "payload": payload,
        "to_address": to_address,
    }
    if target_kind == "mailbox":
        envelope["mailbox_address"] = to_address
    if thread_id is not None:
        envelope["thread_id"] = thread_id
    if parent_thread_id is not None:
        envelope["parent_thread_id"] = parent_thread_id

    lowered: dict[str, Any] = {
        "from_address": from_address,
        "envelope": envelope,
        "deliver_after_seconds": _optional_int(artifact, "deliver_after_seconds", default=0),
        "expires_in_seconds": _optional_int_or_none(artifact, "expires_in_seconds"),
        "max_attempts": _optional_int(artifact, "max_attempts", default=8),
    }
    for key in ("subject", "reply_to_address", "correlation_id", "workflow_id", "idempotency_key", "message_type"):
        value = _optional_string(artifact, key)
        if value is not None:
            lowered[key] = value
    headers = artifact.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError("artifact.headers must be a JSON object when provided")
        lowered["headers"] = headers
    return lowered


def _lower_handoff_event_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {
        "op": "handoff",
        "from_thread_id": _require_string(artifact, "from_thread_id"),
        "to_thread_id": _require_string(artifact, "to_thread_id"),
    }
    metadata = artifact.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("artifact.metadata must be a JSON object when provided")
        event["metadata"] = metadata
    lowered: dict[str, Any] = {"event": event}
    actor = _optional_string(artifact, "actor")
    if actor is not None:
        lowered["actor"] = actor
    return lowered


def _build_admin_client(*, request: dict[str, Any], args: argparse.Namespace) -> MailboxHTTPClient:
    base_url = _resolve_base_url(request=request, args=args)
    admin_token = _resolve_admin_token(request=request, args=args)
    timeout_seconds = _resolve_timeout_seconds(request=request, args=args)
    if not admin_token:
        raise ValueError("run requests require admin_token via request, --admin-token, or MAILBOX_ADMIN_TOKEN")
    return MailboxHTTPClient(
        MailboxClientConfig(
            base_url=base_url,
            token=admin_token,
            timeout_seconds=timeout_seconds,
        )
    )


def _resolve_base_url(*, request: dict[str, Any], args: argparse.Namespace) -> str:
    request_value = request.get("base_url")
    if isinstance(request_value, str) and request_value.strip():
        return request_value.strip().rstrip("/")
    if args.base_url:
        return args.base_url.strip().rstrip("/")
    env_base_url = (os.environ.get("MAILBOX_BASE_URL") or "").strip()
    if env_base_url:
        return env_base_url.rstrip("/")
    return DEFAULT_MAILBOX_BASE_URL


def _resolve_admin_token(*, request: dict[str, Any], args: argparse.Namespace) -> str | None:
    request_value = request.get("admin_token")
    if isinstance(request_value, str) and request_value.strip():
        return request_value.strip()
    if args.admin_token and args.admin_token.strip():
        return args.admin_token.strip()
    env_token = (os.environ.get("MAILBOX_ADMIN_TOKEN") or "").strip()
    return env_token or None


def _resolve_timeout_seconds(*, request: dict[str, Any], args: argparse.Namespace) -> float:
    request_value = request.get("timeout_seconds")
    if request_value is not None:
        return float(request_value)
    if args.timeout_seconds is not None:
        return float(args.timeout_seconds)
    env_timeout = (os.environ.get("MAILBOX_TIMEOUT_SECONDS") or "").strip()
    if env_timeout:
        return float(env_timeout)
    return 10.0


def _resolve_cache_dir(*, request: dict[str, Any], args: argparse.Namespace) -> Path | None:
    request_value = request.get("cache_dir")
    if isinstance(request_value, str) and request_value.strip():
        return Path(request_value.strip())
    if args.cache_dir:
        return Path(args.cache_dir)
    env_value = (os.environ.get("MAILBOX_LANGUAGE_CACHE_DIR") or "").strip()
    if env_value:
        return Path(env_value)
    return None


def _error_response(
    *,
    request_id: Any,
    command: str | None,
    artifact_kind: str | None,
    error: str,
    line_number: int,
    error_code: str | None = None,
    status: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "id": request_id,
        "command": command,
        "artifact_kind": artifact_kind,
        "line_number": line_number,
        "error": error,
    }
    if error_code is not None:
        response["error_code"] = error_code
    if status is not None:
        response["status"] = status
    if payload is not None:
        response["payload"] = payload
    return response


def _require_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    return value


def _require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return _require_non_empty_string(value, key)


def _require_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    return _require_non_empty_string(value, key)


def _optional_int(data: dict[str, Any], key: str, *, default: int) -> int:
    value = data.get(key)
    if value is None:
        return default
    return int(value)


def _optional_int_or_none(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    return int(value)


if __name__ == "__main__":
    main()
