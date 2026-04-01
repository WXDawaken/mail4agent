from __future__ import annotations

"""Bootstrap a mailbox runtime for subagent_lab cross-project oncall roles."""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_HARNESS_ID = "subagentdogfood"
DEFAULT_PROJECT_ID = "subagentlab"
DEFAULT_RUNTIME_DIR = ".tmp_subagent_lab_oncall"


def _request_json(
    base_url: str,
    admin_token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {admin_token}",
    }
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: {exc.code} {detail}") from exc
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"{method} {path} returned error: {data}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _preview_mailbox_identity(
    *,
    base_url: str,
    admin_token: str,
    harness_id: str,
    project_id: str,
    local_part: str,
    mailbox_type: str,
    agent_name: str,
) -> dict[str, Any]:
    return _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/preview_agent_session",
        {
            "harness_id": harness_id,
            "project_id": project_id,
            "local_part": local_part,
            "mailbox_type": mailbox_type,
            "agent_name": agent_name,
            "accept_messages": True,
        },
    )["preview"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap mailbox runtime for subagent_lab dev oncall roles")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=os.environ.get("MAILBOX_ADMIN_TOKEN") or "")
    parser.add_argument("--harness-id", default=DEFAULT_HARNESS_ID)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()

    admin_token = args.admin_token.strip()
    if not admin_token:
        raise SystemExit("missing admin token; pass --admin-token or set MAILBOX_ADMIN_TOKEN")

    base_url = args.base_url.rstrip("/")
    harness_id = args.harness_id.strip().lower()
    project_id = args.project_id.strip().lower()
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/upsert_harness",
        {
            "harness_id": harness_id,
            "display_name": "Subagent Lab Dogfood Harness",
            "enabled": True,
        },
    )
    _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/upsert_project",
        {
            "project_id": project_id,
            "harness_id": harness_id,
            "display_name": "subagent_lab dogfood",
            "enabled": True,
        },
    )
    _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/allow_same_project",
        {
            "project_id": project_id,
            "harness_id": harness_id,
            "priority": 100,
        },
    )

    role_specs = {
        "plugin_dev": {
            "local_part": "plugin_dev",
            "agent_name": "anchor-agent-plugin-dev-oncall",
            "consumer_id": "anchor-agent-plugin-dev-oncall",
        },
        "core_dev": {
            "local_part": "core_dev",
            "agent_name": "anchor-agent-core-dev-oncall",
            "consumer_id": "anchor-agent-core-dev-oncall",
        },
        "salvage_run_dev": {
            "local_part": "salvage_run_dev",
            "agent_name": "salvage-run-dev-oncall",
            "consumer_id": "salvage-run-dev-oncall",
        },
        "game_engine_dev": {
            "local_part": "game_engine_dev",
            "agent_name": "game-engine-dev-oncall",
            "consumer_id": "game-engine-dev-oncall",
        },
        "coordinator": {
            "local_part": "coordinator",
            "agent_name": "subagent-lab-coordinator",
            "consumer_id": "subagent-lab-coordinator",
        },
    }
    role_addresses = {
        role: f"{spec['local_part']}@{project_id}.{harness_id}"
        for role, spec in role_specs.items()
    }
    for address in role_addresses.values():
        _request_json(
            base_url,
            admin_token,
            "POST",
            "/admin/upsert_mailbox",
            {
                "address": address,
                "mailbox_type": "group",
                "enabled": True,
                "accept_messages": True,
            },
        )

    token_payload = _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/create_harness_token",
        {
            "harness_id": harness_id,
            "token_name": "subagent-lab-dev-oncall",
        },
    )
    harness_token = str(token_payload["token"])

    previews: dict[str, dict[str, Any]] = {}
    configs: dict[str, dict[str, Any]] = {}
    for role, spec in role_specs.items():
        preview = _preview_mailbox_identity(
            base_url=base_url,
            admin_token=admin_token,
            harness_id=harness_id,
            project_id=project_id,
            local_part=str(spec["local_part"]),
            mailbox_type="group",
            agent_name=str(spec["agent_name"]),
        )
        previews[role] = preview
        configs[role] = {
            "base_url": base_url,
            "from_address": preview.get("default_from_address"),
            "inbox_address": (preview.get("default_claim_addresses") or [None])[0],
            "project_id": project_id,
            "local_part": str(spec["local_part"]),
            "mailbox_type": "group",
            "agent_name": str(spec["agent_name"]),
            "consumer_id": str(spec["consumer_id"]),
        }

    harness_token_path = runtime_dir / "harness.token"
    harness_token_path.write_text(harness_token + "\n", encoding="utf-8")
    config_paths: dict[str, Path] = {}
    for role, config in configs.items():
        config_path = runtime_dir / f"{role}.mailbox_client.json"
        preview_path = runtime_dir / f"{role}.preview.json"
        config_paths[role] = config_path
        _write_json(config_path, config)
        _write_json(preview_path, previews[role])

    summary = {
        "ok": True,
        "base_url": base_url,
        "harness_id": harness_id,
        "project_id": project_id,
        "plugin_dev_address": role_addresses["plugin_dev"],
        "core_dev_address": role_addresses["core_dev"],
        "salvage_run_dev_address": role_addresses["salvage_run_dev"],
        "game_engine_dev_address": role_addresses["game_engine_dev"],
        "coordinator_address": role_addresses["coordinator"],
        "runtime_dir": str(runtime_dir.resolve()),
        "harness_token_file": str(harness_token_path.resolve()),
        "plugin_dev_config_file": str(config_paths["plugin_dev"].resolve()),
        "core_dev_config_file": str(config_paths["core_dev"].resolve()),
        "salvage_run_dev_config_file": str(config_paths["salvage_run_dev"].resolve()),
        "game_engine_dev_config_file": str(config_paths["game_engine_dev"].resolve()),
        "coordinator_config_file": str(config_paths["coordinator"].resolve()),
    }
    _write_json(runtime_dir / "bootstrap_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
