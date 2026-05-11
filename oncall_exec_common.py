from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def build_handler_base_env(
    *,
    bootstrap_summary: dict[str, Any],
    role: str,
    runtime_dir: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["MAILBOX_ONCALL_MODE"] = "1"
    env["MAILBOX_ONCALL_ROLE"] = role
    env["MAILBOX_ONCALL_RUNTIME_DIR"] = str(runtime_dir)
    harness_id = str(bootstrap_summary.get("harness_id") or "").strip()
    project_id = str(bootstrap_summary.get("project_id") or "").strip()
    if harness_id:
        env["MAILBOX_HARNESS_ID"] = harness_id
    if project_id:
        env["MAILBOX_PROJECT_ID"] = project_id
    return env


def read_bounded_text(path: Path, *, max_chars: int = 1200) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 14].rstrip() + " [truncated]"
