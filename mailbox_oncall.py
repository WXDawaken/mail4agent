from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_mailbox_client import MailboxClientConfig, MailboxHTTPClient
from mailbox_worker import ConsumeConfig
from oncall_exec_app_server import AppServerOncallExecutor
from oncall_exec_codex import CodexOncallExecutor
from oncall_registry import OncallRegistry
from oncall_supervisor import OncallSupervisorConfig, run_oncall_supervisor


@dataclass(frozen=True)
class OncallRoleSpec:
    config_file: str
    default_reasoning_effort: str


ROLE_SPECS: dict[str, OncallRoleSpec] = {
    "operator": OncallRoleSpec(
        config_file="operator.mailbox_client.json",
        default_reasoning_effort="high",
    ),
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_oncall(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def run_oncall(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    runtime_dir = _resolve_runtime_dir(root, args.runtime_dir)
    role_spec = ROLE_SPECS[args.role]
    bootstrap_summary_path = runtime_dir / "bootstrap_summary.json"
    harness_token_path = runtime_dir / "harness.token"
    config_path = runtime_dir / role_spec.config_file
    started_at = _utc_now()
    registry = OncallRegistry.create(
        runtime_dir=runtime_dir,
        role=args.role,
        raw_summary_file=args.summary_file,
    )
    if args.inspect_registry:
        return registry.inspect_state()
    try:
        bootstrap_summary = _load_json_file(bootstrap_summary_path, label="bootstrap summary")
        role_config = _load_json_file(config_path, label=f"{args.role} runtime config")
        harness_token = harness_token_path.read_text(encoding="utf-8").strip()
        if not harness_token:
            raise ValueError(f"empty harness token file: {harness_token_path}")

        client = _build_client(
            harness_token=harness_token,
            role_config=role_config,
        )
        claim_addresses = _resolve_claim_addresses(
            args=args,
            bootstrap_summary=bootstrap_summary,
            role_config=role_config,
        )
        heartbeat_interval_seconds = (
            float(args.heartbeat_interval_seconds)
            if args.heartbeat_interval_seconds is not None
            else max(15.0, float(args.lease_seconds) / 3.0)
        )
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat-interval-seconds must be greater than zero")
        if heartbeat_interval_seconds >= args.lease_seconds:
            raise ValueError("heartbeat-interval-seconds must be less than lease-seconds")
        if args.max_deliveries is not None and args.max_deliveries <= 0:
            raise ValueError("max-deliveries must be greater than zero")
        if args.max_empty_polls is not None and args.max_empty_polls <= 0:
            raise ValueError("max-empty-polls must be greater than zero")
        worker_idle_timeout_seconds = _normalize_worker_policy_seconds(
            args.worker_idle_timeout_seconds,
            label="worker-idle-timeout-seconds",
        )
        worker_max_age_seconds = _normalize_worker_policy_seconds(
            args.worker_max_age_seconds,
            label="worker-max-age-seconds",
        )

        consumer_id = str(
            args.consumer_id
            or role_config.get("consumer_id")
            or f"mailbox-oncall-{args.role}"
        )
        executor = _build_executor(
            backend=args.backend,
            root=root,
            role=args.role,
            runtime_dir=runtime_dir,
            reasoning_effort=args.reasoning_effort or role_spec.default_reasoning_effort,
            bootstrap_summary=bootstrap_summary,
            codex_workspace_dir=args.codex_workspace_dir,
            codex_home_dir=args.codex_home_dir,
            worker_idle_timeout_seconds=worker_idle_timeout_seconds,
            worker_max_age_seconds=worker_max_age_seconds,
        )
        consume_config = ConsumeConfig(
            to_address=claim_addresses[0] if len(claim_addresses) == 1 else None,
            to_addresses=tuple(claim_addresses) if len(claim_addresses) > 1 else (),
            consumer_id=consumer_id,
            serialization_scope=str(args.serialization_scope),
            lease_seconds=int(args.lease_seconds),
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            poll_interval_seconds=float(args.poll_interval_seconds),
            retry_after_seconds=int(args.retry_after_seconds),
            ack_exit_codes=frozenset({0}),
            once=not bool(args.watch) and args.max_deliveries is None,
            max_deliveries=args.max_deliveries,
            max_empty_polls=args.max_empty_polls,
        )
        supervisor_config = OncallSupervisorConfig(
            role=args.role,
            watch=bool(args.watch),
            runtime_dir=runtime_dir,
            consumer_id=consumer_id,
            claim_addresses=tuple(claim_addresses),
            execution_metadata=dict(executor.execution_metadata),
            consume_config=consume_config,
            started_at=started_at,
            can_reuse_worker=getattr(executor, "can_reuse_worker", None),
            on_idle=getattr(executor, "on_idle", None),
            resolve_workspace=getattr(executor, "resolve_workspace_assignment", None),
        )
    except Exception as exc:
        failure = {
            "ok": False,
            "role": args.role,
            "backend": args.backend,
            "runtime_dir": str(runtime_dir),
            "summary_file": str(registry.summary_path),
            "registry_file": str(registry.registry_path),
            "max_empty_polls": args.max_empty_polls,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "error": str(exc),
        }
        registry.record_failure(failure)
        raise

    try:
        return run_oncall_supervisor(
            client,
            supervisor_config,
            registry,
            executor.execute_claimed_delivery,
        )
    finally:
        close_executor = getattr(executor, "close", None)
        if callable(close_executor):
            close_executor()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Role-aware mailbox oncall supervisor")
    parser.add_argument("--role", choices=sorted(ROLE_SPECS.keys()), default="operator")
    parser.add_argument("--runtime-dir", default=".tmp_dogfood")
    parser.add_argument("--backend", choices=("codex-cli", "app-server"), default="codex-cli")
    parser.add_argument(
        "--inspect-registry",
        action="store_true",
        help="print current role and thread registry state for this runtime dir, then exit",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep polling for more work; default behavior is a single once-off attempt",
    )
    parser.add_argument("--max-deliveries", type=int, help="stop after this many claimed deliveries")
    parser.add_argument("--max-empty-polls", type=int, help="stop after this many consecutive empty polls while watching")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=None)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--retry-after-seconds", type=int, default=60)
    parser.add_argument("--consumer-id")
    parser.add_argument(
        "--serialization-scope",
        choices=("delivery", "mailbox_thread"),
        default="mailbox_thread",
        help="delivery: claim any queued delivery; mailbox_thread: serialize claims within one mailbox thread",
    )
    parser.add_argument("--to-address")
    parser.add_argument("--to-addresses", help="comma-separated claim addresses")
    parser.add_argument("--local-part", help="resolve <local_part>@<project>.<harness> from bootstrap summary")
    parser.add_argument("--session", help="resolve session_<session>@<project>.<harness> from bootstrap summary")
    parser.add_argument("--mailbox-type", help="optional metadata only for local-part based addressing")
    parser.add_argument("--reasoning-effort", choices=("medium", "high", "xhigh"))
    parser.add_argument("--codex-workspace-dir", help="optional workspace root for codex-cli backend runs")
    parser.add_argument("--codex-home-dir", help="optional CODEX_HOME dir for codex-cli backend runs")
    parser.add_argument(
        "--worker-idle-timeout-seconds",
        type=float,
        default=900.0,
        help="for reusable backends, close an idle worker after this many seconds; use 0 to disable",
    )
    parser.add_argument(
        "--worker-max-age-seconds",
        type=float,
        default=3600.0,
        help="for reusable backends, retire a worker after this total lifetime; use 0 to disable",
    )
    parser.add_argument("--summary-file", help="write the last oncall run summary JSON here")
    return parser


def _resolve_runtime_dir(root: Path, raw_runtime_dir: str) -> Path:
    runtime_dir = Path(raw_runtime_dir)
    if not runtime_dir.is_absolute():
        runtime_dir = root / runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir.resolve()


def _load_json_file(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing {label}: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return payload


def _build_client(*, harness_token: str, role_config: dict[str, Any]) -> MailboxHTTPClient:
    return MailboxHTTPClient(
        MailboxClientConfig(
            base_url=str(role_config.get("base_url") or "http://127.0.0.1:8787").rstrip("/"),
            token=harness_token,
            from_address=_optional_str(role_config, "from_address"),
            inbox_address=_optional_str(role_config, "inbox_address"),
            project_id=_optional_str(role_config, "project_id"),
            role=_optional_str(role_config, "role"),
            roles=tuple(_optional_str_list(role_config, "roles")),
            session=_optional_str(role_config, "session"),
            agent_name=_optional_str(role_config, "agent_name"),
            local_part=_optional_str(role_config, "local_part"),
            mailbox_type=_optional_str(role_config, "mailbox_type"),
            consumer_id=_optional_str(role_config, "consumer_id"),
            timeout_seconds=float(role_config.get("timeout_seconds") or 15.0),
        )
    )


def _resolve_claim_addresses(
    *,
    args: argparse.Namespace,
    bootstrap_summary: dict[str, Any],
    role_config: dict[str, Any],
) -> list[str]:
    if args.to_address and args.to_addresses:
        raise ValueError("provide either --to-address or --to-addresses, not both")
    if args.session and args.local_part:
        raise ValueError("provide either --session or --local-part, not both")

    if args.to_addresses:
        addresses = _split_csv(args.to_addresses)
        if not addresses:
            raise ValueError("--to-addresses must not be empty")
        return addresses
    if args.to_address:
        return [str(args.to_address).strip()]

    project_id = str(bootstrap_summary.get("project_id") or "").strip()
    harness_id = str(bootstrap_summary.get("harness_id") or "").strip()
    if not project_id or not harness_id:
        raise ValueError("bootstrap summary is missing project_id or harness_id")

    if args.session:
        return [f"session_{args.session.strip()}@{project_id}.{harness_id}"]
    if args.local_part:
        return [f"{args.local_part.strip()}@{project_id}.{harness_id}"]

    inbox_address = _optional_str(role_config, "inbox_address")
    if inbox_address:
        return [inbox_address]
    raise ValueError("unable to resolve claim address; provide --to-address, --to-addresses, --session, or --local-part")


def _build_executor(
    *,
    backend: str,
    root: Path,
    role: str,
    runtime_dir: Path,
    reasoning_effort: str,
    bootstrap_summary: dict[str, Any],
    codex_workspace_dir: str | None,
    codex_home_dir: str | None,
    worker_idle_timeout_seconds: float | None,
    worker_max_age_seconds: float | None,
) -> Any:
    if backend == "codex-cli":
        workspace_dir = _resolve_optional_path(root, codex_workspace_dir)
        home_dir = _resolve_optional_path(root, codex_home_dir)
        return CodexOncallExecutor.build(
            root=root,
            role=role,
            runtime_dir=runtime_dir,
            reasoning_effort=reasoning_effort,
            bootstrap_summary=bootstrap_summary,
            workspace_dir=workspace_dir,
            codex_home_dir=home_dir,
        )
    if backend == "app-server":
        workspace_dir = _resolve_optional_path(root, codex_workspace_dir)
        home_dir = _resolve_optional_path(root, codex_home_dir)
        return AppServerOncallExecutor.build(
            root=root,
            role=role,
            runtime_dir=runtime_dir,
            reasoning_effort=reasoning_effort,
            bootstrap_summary=bootstrap_summary,
            workspace_dir=workspace_dir,
            codex_home_dir=home_dir,
            worker_idle_timeout_seconds=worker_idle_timeout_seconds,
            worker_max_age_seconds=worker_max_age_seconds,
        )
    raise ValueError(f"unsupported backend: {backend}")


def _split_csv(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    return [item.strip() for item in str(raw_value).split(",") if item.strip()]


def _resolve_optional_path(root: Path, raw_path: str | None) -> Path | None:
    if raw_path is None:
        return None
    value = str(raw_path).strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _normalize_worker_policy_seconds(raw_value: float | None, *, label: str) -> float | None:
    if raw_value is None:
        return None
    value = float(raw_value)
    if value < 0:
        raise ValueError(f"{label} must be greater than or equal to zero")
    if value == 0:
        return None
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_str_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return _split_csv(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    raise ValueError(f"{key} must be a string or list of strings")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
