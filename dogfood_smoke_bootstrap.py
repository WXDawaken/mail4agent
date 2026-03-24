from __future__ import annotations

"""Bootstrap local dogfood mailbox profiles for Codex agents.

This helper assumes the HTTP server is already running and that an admin token
is available. It creates a small local harness/project, installs a same-project
routing policy, creates a fresh harness token, and writes runtime-only profile
files under `.tmp_dogfood/`.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_HARNESS_ID = "dogfood"
DEFAULT_PROJECT_ID = "mail4agent"
DEFAULT_RUNTIME_DIR = ".tmp_dogfood"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap local dogfood mailbox smoke assets")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=os.environ.get("MAILBOX_ADMIN_TOKEN") or "")
    parser.add_argument("--harness-id", default=DEFAULT_HARNESS_ID)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()

    if not args.admin_token.strip():
        raise SystemExit("missing admin token; pass --admin-token or set MAILBOX_ADMIN_TOKEN")

    base_url = args.base_url.rstrip("/")
    harness_id = args.harness_id.strip().lower()
    project_id = args.project_id.strip().lower()
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/upsert_harness",
        {
            "harness_id": harness_id,
            "display_name": "Dogfood Harness",
            "enabled": True,
        },
    )
    _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/upsert_project",
        {
            "project_id": project_id,
            "harness_id": harness_id,
            "display_name": "mail4agent dogfood",
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

    operator_address = f"operator@{project_id}.{harness_id}"
    planner_address = f"planner@{project_id}.{harness_id}"
    reviewer_address = f"reviewer@{project_id}.{harness_id}"
    for address, mailbox_type in (
        (operator_address, "group"),
        (planner_address, "role"),
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

    token_payload = _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/create_harness_token",
        {
            "harness_id": harness_id,
            "token_name": "dogfood-agents",
        },
    )
    harness_token = str(token_payload["token"])

    operator_preview = _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/preview_agent_session",
        {
            "harness_id": harness_id,
            "project_id": project_id,
            "local_part": "operator",
            "mailbox_type": "group",
            "agent_name": "dogfood-operator",
            "accept_messages": True,
        },
    )["preview"]
    planner_preview = _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/preview_agent_session",
        {
            "harness_id": harness_id,
            "project_id": project_id,
            "role": "planner",
            "session": "dogfood",
            "agent_name": "dogfood-planner",
            "accept_messages": True,
        },
    )["preview"]
    reviewer_preview = _request_json(
        base_url,
        args.admin_token,
        "POST",
        "/admin/preview_agent_session",
        {
            "harness_id": harness_id,
            "project_id": project_id,
            "role": "reviewer",
            "session": "dogfood",
            "agent_name": "dogfood-reviewer",
            "accept_messages": True,
        },
    )["preview"]

    operator_config = {
        "base_url": base_url,
        "from_address": operator_preview.get("default_from_address"),
        "inbox_address": (operator_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "local_part": "operator",
        "mailbox_type": "group",
        "agent_name": "dogfood-operator",
        "consumer_id": "dogfood-operator-high",
    }
    planner_config = {
        "base_url": base_url,
        "from_address": planner_preview.get("default_from_address"),
        "inbox_address": (planner_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "role": "planner",
        "session": "dogfood",
        "agent_name": "dogfood-planner",
        "consumer_id": "dogfood-planner-medium",
    }
    reviewer_config = {
        "base_url": base_url,
        "from_address": reviewer_preview.get("default_from_address"),
        "inbox_address": (reviewer_preview.get("default_claim_addresses") or [None])[0],
        "project_id": project_id,
        "role": "reviewer",
        "session": "dogfood",
        "agent_name": "dogfood-reviewer",
        "consumer_id": "dogfood-reviewer-medium",
    }

    harness_token_path = runtime_dir / "harness.token"
    harness_token_path.write_text(harness_token + "\n", encoding="utf-8")
    operator_config_path = runtime_dir / "operator.mailbox_client.json"
    planner_config_path = runtime_dir / "planner.mailbox_client.json"
    reviewer_config_path = runtime_dir / "reviewer.mailbox_client.json"
    _write_json(operator_config_path, operator_config)
    _write_json(planner_config_path, planner_config)
    _write_json(reviewer_config_path, reviewer_config)
    _write_json(runtime_dir / "operator.preview.json", operator_preview)
    _write_json(runtime_dir / "planner.preview.json", planner_preview)
    _write_json(runtime_dir / "reviewer.preview.json", reviewer_preview)

    summary = {
        "ok": True,
        "base_url": base_url,
        "harness_id": harness_id,
        "project_id": project_id,
        "operator_address": operator_address,
        "planner_address": planner_address,
        "reviewer_address": reviewer_address,
        "runtime_dir": str(runtime_dir.resolve()),
        "harness_token_file": str(harness_token_path.resolve()),
        "operator_config_file": str(operator_config_path.resolve()),
        "planner_config_file": str(planner_config_path.resolve()),
        "reviewer_config_file": str(reviewer_config_path.resolve()),
    }
    _write_json(runtime_dir / "bootstrap_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
