from __future__ import annotations

"""Runnable demo agent built on top of CodexMailboxAdapter.

Supported task types:
    - echo
    - upper_text
    - sum_numbers
    - sleep_echo
    - retry_demo

Usage:
    python codex_mailbox_demo_agent.py --once
    python codex_mailbox_demo_agent.py
"""

import argparse
import json
import sys
import time
from typing import Any

from codex_mailbox_adapter import (
    CodexMailboxAdapter,
    CodexMailboxContext,
    ReplyAction,
    RetryableMailboxError,
)


def demo_handler(ctx: CodexMailboxContext) -> dict[str, Any] | ReplyAction:
    payload = ctx.payload
    task_type = str(payload.get("task_type") or payload.get("task") or "echo").strip().lower()

    if task_type == "echo":
        return {
            "ok": True,
            "task_type": task_type,
            "handled_by": ctx.consumer_id,
            "message_id": ctx.message_id,
            "echo": payload,
        }

    if task_type == "upper_text":
        text = _require_string(payload, "text")
        return {
            "ok": True,
            "task_type": task_type,
            "handled_by": ctx.consumer_id,
            "result": text.upper(),
        }

    if task_type == "sum_numbers":
        numbers = payload.get("numbers")
        if not isinstance(numbers, list) or not numbers:
            raise ValueError("sum_numbers requires a non-empty numbers list")
        total = 0.0
        normalized_numbers: list[float] = []
        for value in numbers:
            if not isinstance(value, (int, float)):
                raise ValueError("sum_numbers accepts only int/float values")
            normalized = float(value)
            normalized_numbers.append(normalized)
            total += normalized
        return {
            "ok": True,
            "task_type": task_type,
            "handled_by": ctx.consumer_id,
            "count": len(normalized_numbers),
            "total": total,
            "numbers": normalized_numbers,
        }

    if task_type == "sleep_echo":
        seconds = int(payload.get("seconds", 5))
        seconds = max(0, min(seconds, 300))
        time.sleep(seconds)
        return {
            "ok": True,
            "task_type": task_type,
            "handled_by": ctx.consumer_id,
            "slept_seconds": seconds,
            "echo": payload,
        }

    if task_type == "retry_demo":
        retry_after_seconds = int(payload.get("retry_after_seconds", 15))
        raise RetryableMailboxError(
            f"demo requested retry for task_type={task_type}",
            retry_after_seconds=max(1, retry_after_seconds),
        )

    return ReplyAction(
        payload={
            "ok": False,
            "task_type": task_type,
            "handled_by": ctx.consumer_id,
            "error": f"unknown task_type: {task_type}",
            "supported_task_types": [
                "echo",
                "upper_text",
                "sum_numbers",
                "sleep_echo",
                "retry_demo",
            ],
        },
        message_type="codex.error",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Mailbox demo agent")
    parser.add_argument("--once", action="store_true", help="claim and process at most one delivery")
    parser.add_argument("--lease-seconds", type=int, default=60, help="claim lease duration")
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=int,
        default=20,
        help="heartbeat cadence; must be less than lease-seconds",
    )
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0, help="idle poll sleep")
    parser.add_argument("--inbox-address", help="override MAILBOX_INBOX_ADDRESS")
    parser.add_argument("--consumer-id", help="override MAILBOX_CONSUMER_ID")
    args = parser.parse_args()

    adapter = CodexMailboxAdapter.from_env(
        inbox_address=args.inbox_address,
        consumer_id=args.consumer_id,
        lease_seconds=args.lease_seconds,
        heartbeat_interval_seconds=args.heartbeat_interval_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        logger=lambda message: print(message, file=sys.stderr),
    )

    if args.once:
        delivery = adapter.process_once(demo_handler)
        if delivery is None:
            print("no delivery available")
        else:
            print(json.dumps({"processed_delivery_id": delivery["delivery_id"]}, ensure_ascii=False))
        return

    adapter.run_forever(demo_handler)


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


if __name__ == "__main__":
    main()
