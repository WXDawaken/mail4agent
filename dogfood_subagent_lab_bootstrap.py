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

    role_addresses = {
        "salvage_run_dev": f"salvage_run_dev@{project_id}.{harness_id}",
        "game_engine_dev": f"game_engine_dev@{project_id}.{harness_id}",
        "coordinator": f"coordinator@{project_id}.{harness_id}",
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

    salvage_preview = _preview_mailbox_identity(
        base_url=base_url,
        admin_token=admin_token,
        harness_id=harness_id,
        project_id=project_id,
        local_part="salvage_run_dev",
        mailbox_type="group",
        agent_name="salvage-run-dev-oncall",
    )
    engine_preview = _preview_mailbox_identity(
        base_url=base_url,
        admin_token=admin_token,
        harness_id=harness_id,
        project_id=project_id,
        local_part="game_engine_dev",
        mailbox_type="group",
        agent_name="game-engine-dev-oncall",
    )
    coordinator_preview = _preview_mailbox_identity(
        base_url=base_url,
        admin_token=admin_token,
        harness_id=harness_id,
        project_id=project_id,
        local_part="coordinator",
        mailbox_type="group",
        agent_name="subagent-lab-coordinator",
    )

    salvage_config = {
        "base_url": base_url,
        "from_address": salvage_preview.get("default_from_address"),
        "inbox_address": (salvage_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "local_part": "salvage_run_dev",
        "mailbox_type": "group",
        "agent_name": "salvage-run-dev-oncall",
        "consumer_id": "salvage-run-dev-oncall",
    }
    engine_config = {
        "base_url": base_url,
        "from_address": engine_preview.get("default_from_address"),
        "inbox_address": (engine_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "local_part": "game_engine_dev",
        "mailbox_type": "group",
        "agent_name": "game-engine-dev-oncall",
        "consumer_id": "game-engine-dev-oncall",
    }
    coordinator_config = {
        "base_url": base_url,
        "from_address": coordinator_preview.get("default_from_address"),
        "inbox_address": (coordinator_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "local_part": "coordinator",
        "mailbox_type": "group",
        "agent_name": "subagent-lab-coordinator",
        "consumer_id": "subagent-lab-coordinator",
    }

    harness_token_path = runtime_dir / "harness.token"
    harness_token_path.write_text(harness_token + "\n", encoding="utf-8")
    salvage_config_path = runtime_dir / "salvage_run_dev.mailbox_client.json"
    engine_config_path = runtime_dir / "game_engine_dev.mailbox_client.json"
    coordinator_config_path = runtime_dir / "coordinator.mailbox_client.json"
    _write_json(salvage_config_path, salvage_config)
    _write_json(engine_config_path, engine_config)
    _write_json(coordinator_config_path, coordinator_config)
    _write_json(runtime_dir / "salvage_run_dev.preview.json", salvage_preview)
    _write_json(runtime_dir / "game_engine_dev.preview.json", engine_preview)
    _write_json(runtime_dir / "coordinator.preview.json", coordinator_preview)

    summary = {
        "ok": True,
        "base_url": base_url,
        "harness_id": harness_id,
        "project_id": project_id,
        "salvage_run_dev_address": role_addresses["salvage_run_dev"],
        "game_engine_dev_address": role_addresses["game_engine_dev"],
        "coordinator_address": role_addresses["coordinator"],
        "runtime_dir": str(runtime_dir.resolve()),
        "harness_token_file": str(harness_token_path.resolve()),
        "salvage_run_dev_config_file": str(salvage_config_path.resolve()),
        "game_engine_dev_config_file": str(engine_config_path.resolve()),
        "coordinator_config_file": str(coordinator_config_path.resolve()),
    }
    _write_json(runtime_dir / "bootstrap_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
