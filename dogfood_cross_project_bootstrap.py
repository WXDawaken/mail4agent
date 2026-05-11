from __future__ import annotations

"""Bootstrap a cross-project dogfood mailbox runtime.

This helper creates two projects in one harness:

- consumer_app
- shared_tools

It then installs bounded cross-project routing so consumer planners can send
requests into the shared-tools intake mailbox, while shared-tools can reply
back to consumer role mailboxes.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_HARNESS_ID = "dogfood"
DEFAULT_CONSUMER_PROJECT_ID = "consumer_app"
DEFAULT_SUPPLIER_PROJECT_ID = "shared_tools"
DEFAULT_RUNTIME_DIR = ".tmp_dogfood_cross_project"


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


def _preview(
    *,
    base_url: str,
    admin_token: str,
    harness_id: str,
    project_id: str,
    role: str | None = None,
    session: str | None = None,
    local_part: str | None = None,
    mailbox_type: str | None = None,
    agent_name: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "harness_id": harness_id,
        "project_id": project_id,
        "agent_name": agent_name,
        "accept_messages": True,
    }
    if role is not None:
        body["role"] = role
    if session is not None:
        body["session"] = session
    if local_part is not None:
        body["local_part"] = local_part
    if mailbox_type is not None:
        body["mailbox_type"] = mailbox_type
    return _request_json(
        base_url,
        admin_token,
        "POST",
        "/admin/preview_agent_session",
        body,
    )["preview"]


def _build_runtime_config(
    *,
    base_url: str,
    harness_id: str,
    project_id: str,
    preview: dict[str, Any],
    consumer_id: str,
    role: str | None = None,
    session: str | None = None,
    local_part: str | None = None,
    mailbox_type: str | None = None,
    agent_name: str,
) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "harness_id": harness_id,
        "project_id": project_id,
        "from_address": preview.get("default_from_address"),
        "inbox_address": (preview.get("default_claim_addresses") or [None])[0],
        "role": role,
        "session": session,
        "local_part": local_part,
        "mailbox_type": mailbox_type,
        "agent_name": agent_name,
        "consumer_id": consumer_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap cross-project dogfood mailbox assets")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=os.environ.get("MAILBOX_ADMIN_TOKEN") or "")
    parser.add_argument("--harness-id", default=DEFAULT_HARNESS_ID)
    parser.add_argument("--consumer-project-id", default=DEFAULT_CONSUMER_PROJECT_ID)
    parser.add_argument("--supplier-project-id", default=DEFAULT_SUPPLIER_PROJECT_ID)
    parser.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()

    if not args.admin_token.strip():
        raise SystemExit("missing admin token; pass --admin-token or set MAILBOX_ADMIN_TOKEN")

    base_url = args.base_url.rstrip("/")
    harness_id = args.harness_id.strip().lower()
    consumer_project_id = args.consumer_project_id.strip().lower()
    supplier_project_id = args.supplier_project_id.strip().lower()
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/upsert_harness",
        {
            "harness_id": harness_id,
            "display_name": "Cross-project Dogfood Harness",
            "enabled": True,
        },
    )
    for project_id, display_name in (
        (consumer_project_id, "consumer app"),
        (supplier_project_id, "shared tools"),
    ):
        _request_json(
            base_url,
            args.admin_token,
            "POST",
            "/admin/upsert_project",
            {
                "project_id": project_id,
                "harness_id": harness_id,
                "display_name": display_name,
                "enabled": True,
            },
        )
        _request_json(
            base_url,
            args.admin_token,
            "POST",
            "/admin/allow_same_project",
            {
                "project_id": project_id,
                "harness_id": harness_id,
                "priority": 100,
            },
        )

    planner_address = f"planner@{consumer_project_id}.{harness_id}"
    integrator_address = f"integrator@{consumer_project_id}.{harness_id}"
    intake_address = f"intake@{supplier_project_id}.{harness_id}"
    reviewer_address = f"reviewer@{supplier_project_id}.{harness_id}"

    for address, mailbox_type in (
        (planner_address, "role"),
        (integrator_address, "role"),
        (intake_address, "group"),
        (reviewer_address, "role"),
    ):
        _request_json(
            base_url,
            args.admin_token,
            "POST",
            "/admin/upsert_mailbox",
            {
                "address": address,
                "mailbox_type": mailbox_type,
                "enabled": True,
                "accept_messages": True,
            },
        )

    _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/add_routing_policy",
        {
            "effect": "allow",
            "priority": 120,
            "from_harness_id": harness_id,
            "from_project_id": consumer_project_id,
            "to_harness_id": harness_id,
            "to_project_id": supplier_project_id,
            "to_mailbox_type": "group",
            "description": "consumer_app can reach shared_tools intake mailboxes",
            "enabled": True,
        },
    )
    _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/add_routing_policy",
        {
            "effect": "allow",
            "priority": 120,
            "from_harness_id": harness_id,
            "from_project_id": supplier_project_id,
            "to_harness_id": harness_id,
            "to_project_id": consumer_project_id,
            "to_mailbox_type": "role",
            "description": "shared_tools can reply to consumer_app role mailboxes",
            "enabled": True,
        },
    )

    token_payload = _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/create_harness_token",
        {
            "harness_id": harness_id,
            "token_name": "cross-project-dogfood",
        },
    )
    harness_token = str(token_payload["token"])

    planner_preview = _preview(
        base_url=base_url,
        admin_token=args.admin_token,
        harness_id=harness_id,
        project_id=consumer_project_id,
        role="planner",
        session="consumer-dogfood",
        agent_name="consumer-planner",
    )
    integrator_preview = _preview(
        base_url=base_url,
        admin_token=args.admin_token,
        harness_id=harness_id,
        project_id=consumer_project_id,
        role="integrator",
        session="consumer-dogfood",
        agent_name="consumer-integrator",
    )
    operator_preview = _preview(
        base_url=base_url,
        admin_token=args.admin_token,
        harness_id=harness_id,
        project_id=supplier_project_id,
        local_part="intake",
        mailbox_type="group",
        agent_name="shared-tools-intake-operator",
    )
    reviewer_preview = _preview(
        base_url=base_url,
        admin_token=args.admin_token,
        harness_id=harness_id,
        project_id=supplier_project_id,
        role="reviewer",
        session="supplier-dogfood",
        agent_name="shared-tools-reviewer",
    )

    planner_config = _build_runtime_config(
        base_url=base_url,
        harness_id=harness_id,
        project_id=consumer_project_id,
        preview=planner_preview,
        consumer_id="consumer-planner-medium",
        role="planner",
        session="consumer-dogfood",
        agent_name="consumer-planner",
    )
    integrator_config = _build_runtime_config(
        base_url=base_url,
        harness_id=harness_id,
        project_id=consumer_project_id,
        preview=integrator_preview,
        consumer_id="consumer-integrator-medium",
        role="integrator",
        session="consumer-dogfood",
        agent_name="consumer-integrator",
    )
    operator_config = _build_runtime_config(
        base_url=base_url,
        harness_id=harness_id,
        project_id=supplier_project_id,
        preview=operator_preview,
        consumer_id="shared-tools-intake-operator-high",
        local_part="intake",
        mailbox_type="group",
        agent_name="shared-tools-intake-operator",
    )
    reviewer_config = _build_runtime_config(
        base_url=base_url,
        harness_id=harness_id,
        project_id=supplier_project_id,
        preview=reviewer_preview,
        consumer_id="shared-tools-reviewer-medium",
        role="reviewer",
        session="supplier-dogfood",
        agent_name="shared-tools-reviewer",
    )

    harness_token_path = runtime_dir / "harness.token"
    harness_token_path.write_text(harness_token + "\n", encoding="utf-8")

    planner_config_path = runtime_dir / "planner.mailbox_client.json"
    integrator_config_path = runtime_dir / "integrator.mailbox_client.json"
    operator_config_path = runtime_dir / "operator.mailbox_client.json"
    reviewer_config_path = runtime_dir / "reviewer.mailbox_client.json"
    _write_json(planner_config_path, planner_config)
    _write_json(integrator_config_path, integrator_config)
    _write_json(operator_config_path, operator_config)
    _write_json(reviewer_config_path, reviewer_config)

    _write_json(runtime_dir / "planner.preview.json", planner_preview)
    _write_json(runtime_dir / "integrator.preview.json", integrator_preview)
    _write_json(runtime_dir / "operator.preview.json", operator_preview)
    _write_json(runtime_dir / "reviewer.preview.json", reviewer_preview)

    summary = {
        "ok": True,
        "base_url": base_url,
        "harness_id": harness_id,
        "project_id": supplier_project_id,
        "consumer_project_id": consumer_project_id,
        "supplier_project_id": supplier_project_id,
        "planner_address": planner_address,
        "integrator_address": integrator_address,
        "intake_address": intake_address,
        "reviewer_address": reviewer_address,
        "runtime_dir": str(runtime_dir.resolve()),
        "harness_token_file": str(harness_token_path.resolve()),
        "planner_config_file": str(planner_config_path.resolve()),
        "integrator_config_file": str(integrator_config_path.resolve()),
        "operator_config_file": str(operator_config_path.resolve()),
        "reviewer_config_file": str(reviewer_config_path.resolve()),
    }
    _write_json(runtime_dir / "bootstrap_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
