from __future__ import annotations

"""CLI helper for sending demo tasks to a mailbox worker.

Usage:
    python codex_mailbox_demo_send.py --task-type echo --payload-json '{"hello":"world"}'
    python codex_mailbox_demo_send.py --task-type upper_text --text "hello"
    python codex_mailbox_demo_send.py --task-type sum_numbers --numbers "1,2,3"
    python codex_mailbox_demo_send.py --task-type sleep_echo --seconds 6 --wait-for-reply
"""

import argparse
import json
import time
from typing import Any

from codex_mailbox_client import MailboxHTTPClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Send demo tasks through mailbox HTTP")
    parser.add_argument("--to-address", help="override target mailbox; defaults to MAILBOX_INBOX_ADDRESS")
    parser.add_argument(
        "--task-type",
        default="echo",
        choices=("echo", "upper_text", "sum_numbers", "sleep_echo", "retry_demo"),
        help="demo task type",
    )
    parser.add_argument("--subject", help="custom subject")
    parser.add_argument("--text", help="input text for upper_text or echo")
    parser.add_argument("--numbers", help="comma-separated numbers for sum_numbers")
    parser.add_argument("--seconds", type=int, default=5, help="sleep seconds for sleep_echo")
    parser.add_argument(
        "--retry-after-seconds",
        type=int,
        default=15,
        help="retry delay for retry_demo",
    )
    parser.add_argument("--payload-json", help="extra JSON object merged into the payload")
    parser.add_argument("--message-type", default="codex.task", help="message_type sent to mailbox")
    parser.add_argument("--wait-for-reply", action="store_true", help="wait and claim a matching reply")
    parser.add_argument(
        "--reply-address",
        help="mailbox to claim replies from; defaults to MAILBOX_FROM_ADDRESS",
    )
    parser.add_argument(
        "--reply-consumer-id",
        default="codex-demo-sender",
        help="consumer id used while waiting for a reply",
    )
    parser.add_argument("--reply-timeout-seconds", type=float, default=30.0, help="max wait for reply")
    parser.add_argument("--reply-poll-interval-seconds", type=float, default=1.0, help="idle wait between claims")
    parser.add_argument("--reply-lease-seconds", type=int, default=20, help="lease used while waiting for reply")
    args = parser.parse_args()

    client = MailboxHTTPClient.from_env()
    payload = _build_payload(args)
    subject = args.subject or f"demo:{args.task_type}"
    to_address = args.to_address or getattr(client, "_effective_inbox_address", lambda: client.config.inbox_address)()
    if not to_address:
        raise ValueError("--to-address is required unless MAILBOX_INBOX_ADDRESS is configured")

    sent = client.send(
        to_address=to_address,
        payload=payload,
        subject=subject,
        message_type=args.message_type,
    )
    result: dict[str, Any] = {"sent": sent, "payload": payload}

    if not args.wait_for_reply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    reply_address = args.reply_address or getattr(client, "_effective_from_address", lambda: client.config.from_address)()
    if not reply_address:
        raise ValueError("reply_address is required unless MAILBOX_FROM_ADDRESS is configured")

    original_message = client.get_message(sent["message_id"])
    original_thread_id = original_message["thread_id"] if original_message else None
    deadline = time.time() + args.reply_timeout_seconds

    while time.time() < deadline:
        delivery = client.claim(
            to_address=reply_address,
            consumer_id=args.reply_consumer_id,
            lease_seconds=args.reply_lease_seconds,
        )
        if delivery is None:
            time.sleep(args.reply_poll_interval_seconds)
            continue

        matches = (
            delivery.get("in_reply_to_message_id") == sent["message_id"]
            or (original_thread_id is not None and delivery.get("thread_id") == original_thread_id)
        )
        if not matches:
            client.nack(
                delivery_id=int(delivery["delivery_id"]),
                claim_token=str(delivery["claim_token"]),
                retry_after_seconds=1,
                last_error="demo sender waiting for a different reply",
                actor=args.reply_consumer_id,
            )
            time.sleep(0.2)
            continue

        client.ack(
            delivery_id=int(delivery["delivery_id"]),
            claim_token=str(delivery["claim_token"]),
            actor=args.reply_consumer_id,
        )
        result["reply"] = delivery
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    raise TimeoutError(f"timed out waiting for reply after {args.reply_timeout_seconds} seconds")


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_type": args.task_type}

    if args.task_type == "upper_text":
        if not args.text:
            raise ValueError("--text is required for upper_text")
        payload["text"] = args.text

    if args.task_type == "sum_numbers":
        if not args.numbers:
            raise ValueError("--numbers is required for sum_numbers")
        payload["numbers"] = [_parse_number(value) for value in args.numbers.split(",") if value.strip()]
        if not payload["numbers"]:
            raise ValueError("--numbers must include at least one numeric value")

    if args.task_type == "sleep_echo":
        payload["seconds"] = max(0, args.seconds)
        if args.text:
            payload["text"] = args.text

    if args.task_type == "retry_demo":
        payload["retry_after_seconds"] = max(1, args.retry_after_seconds)
        if args.text:
            payload["reason"] = args.text

    if args.task_type == "echo" and args.text:
        payload["text"] = args.text

    if args.payload_json:
        extra_payload = json.loads(args.payload_json)
        if not isinstance(extra_payload, dict):
            raise ValueError("--payload-json must decode to a JSON object")
        payload.update(extra_payload)

    return payload


def _parse_number(value: str) -> int | float:
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty numeric value")
    if "." in stripped or "e" in stripped.lower():
        return float(stripped)
    return int(stripped)


if __name__ == "__main__":
    main()
