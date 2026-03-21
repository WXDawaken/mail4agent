from __future__ import annotations

"""Mailbox worker loop for Codex-style harness consumers.

Examples:
    python codex_mailbox_worker.py --once
    python codex_mailbox_worker.py --mode echo-reply
"""

import argparse
import json
import os
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional

from codex_mailbox_client import MailboxHTTPClient, MailboxHTTPError


@dataclass(frozen=True)
class WorkerDecision:
    action: str
    retry_after_seconds: int = 30
    last_error: str | None = None


def ack_decision() -> WorkerDecision:
    return WorkerDecision(action="ack")


def nack_decision(retry_after_seconds: int = 30, last_error: str | None = None) -> WorkerDecision:
    return WorkerDecision(action="nack", retry_after_seconds=retry_after_seconds, last_error=last_error)


def noop_decision() -> WorkerDecision:
    return WorkerDecision(action="noop")


Handler = Callable[[dict[str, Any], MailboxHTTPClient], WorkerDecision | None]


class CodexMailboxWorker:
    def __init__(
        self,
        client: MailboxHTTPClient,
        *,
        inbox_address: str | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: int | None = None,
        poll_interval_seconds: float = 1.0,
        logger: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.inbox_address = inbox_address or getattr(client, "_effective_inbox_address", lambda: client.config.inbox_address)()
        self.consumer_id = consumer_id or getattr(client, "default_consumer_id", None) or client.config.consumer_id or _default_consumer_id()
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(5, lease_seconds // 3)
        self.poll_interval_seconds = poll_interval_seconds
        self.logger = logger or print
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_interval_seconds must be less than lease_seconds")

    def claim_once(self) -> dict[str, Any] | None:
        if self.inbox_address:
            return self.client.claim(
                to_address=self.inbox_address,
                consumer_id=self.consumer_id,
                lease_seconds=self.lease_seconds,
            )
        return self.client.claim(
            consumer_id=self.consumer_id,
            lease_seconds=self.lease_seconds,
        )

    def process_once(self, handler: Handler) -> dict[str, Any] | None:
        delivery = self.claim_once()
        if not delivery:
            return None

        heartbeat_stop = threading.Event()
        heartbeat_errors: list[BaseException] = []
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(delivery, heartbeat_stop, heartbeat_errors),
            daemon=True,
        )
        heartbeat_thread.start()
        decision: WorkerDecision | None = None

        try:
            decision = handler(delivery, self.client)
            if decision is None:
                decision = ack_decision()
        except Exception:
            last_error = _truncate_text(traceback.format_exc(limit=8), 1500)
            self.logger(f"handler failed for delivery {delivery['delivery_id']}:\n{last_error}")
            decision = nack_decision(last_error=last_error)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.heartbeat_interval_seconds + 1.0))

        if heartbeat_errors:
            heartbeat_error = heartbeat_errors[0]
            self.logger(f"heartbeat issue for delivery {delivery['delivery_id']}: {heartbeat_error}")

        self._apply_decision(delivery, decision or ack_decision())
        return delivery

    def run_forever(self, handler: Handler, *, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                delivery = self.process_once(handler)
            except MailboxHTTPError as exc:
                if exc.status in {401, 403}:
                    raise
                self.logger(str(exc))
                stop_event.wait(self.poll_interval_seconds)
                continue
            except Exception as exc:
                self.logger(f"worker loop error: {exc}")
                stop_event.wait(self.poll_interval_seconds)
                continue

            if delivery is None:
                stop_event.wait(self.poll_interval_seconds)

    def _heartbeat_loop(
        self,
        delivery: dict[str, Any],
        stop_event: threading.Event,
        heartbeat_errors: list[BaseException],
    ) -> None:
        while not stop_event.wait(self.heartbeat_interval_seconds):
            try:
                ok = self.client.heartbeat(
                    delivery_id=int(delivery["delivery_id"]),
                    claim_token=str(delivery["claim_token"]),
                    lease_seconds=self.lease_seconds,
                )
                if not ok:
                    heartbeat_errors.append(RuntimeError("heartbeat rejected by mailbox server"))
                    return
            except Exception as exc:
                heartbeat_errors.append(exc)
                return

    def _apply_decision(self, delivery: dict[str, Any], decision: WorkerDecision) -> None:
        delivery_id = int(delivery["delivery_id"])
        claim_token = str(delivery["claim_token"])
        if decision.action == "ack":
            ok = self.client.ack(
                delivery_id=delivery_id,
                claim_token=claim_token,
                actor=self.consumer_id,
            )
            if not ok:
                raise RuntimeError(f"ack rejected for delivery {delivery_id}")
            self.logger(f"acked delivery {delivery_id}")
            return

        if decision.action == "nack":
            ok = self.client.nack(
                delivery_id=delivery_id,
                claim_token=claim_token,
                retry_after_seconds=decision.retry_after_seconds,
                last_error=decision.last_error,
                actor=self.consumer_id,
            )
            if not ok:
                raise RuntimeError(f"nack rejected for delivery {delivery_id}")
            self.logger(f"nacked delivery {delivery_id}")
            return

        if decision.action == "noop":
            self.logger(f"leaving delivery {delivery_id} untouched")
            return

        raise ValueError(f"unknown worker decision: {decision.action}")


def _default_consumer_id() -> str:
    return f"{socket.gethostname().lower()}-{os.getpid()}"


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _print_handler(delivery: dict[str, Any], _client: MailboxHTTPClient) -> WorkerDecision:
    print(json.dumps(delivery, ensure_ascii=False, indent=2))
    return ack_decision()


def _echo_reply_handler(delivery: dict[str, Any], client: MailboxHTTPClient) -> WorkerDecision:
    print(json.dumps(delivery, ensure_ascii=False, indent=2))
    reply_payload = {
        "ok": True,
        "handled_by": client.config.consumer_id or _default_consumer_id(),
        "echo_message_id": delivery.get("message_id"),
        "echo_payload": delivery.get("payload"),
    }
    client.send_reply(
        delivery,
        payload=reply_payload,
        message_type="codex.reply",
    )
    return ack_decision()


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex mailbox worker")
    parser.add_argument("--once", action="store_true", help="claim and process at most one delivery")
    parser.add_argument(
        "--mode",
        choices=("print", "echo-reply"),
        default="print",
        help="demo handler mode",
    )
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

    client = MailboxHTTPClient.from_env()
    worker = CodexMailboxWorker(
        client,
        inbox_address=args.inbox_address,
        consumer_id=args.consumer_id,
        lease_seconds=args.lease_seconds,
        heartbeat_interval_seconds=args.heartbeat_interval_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    handler = _echo_reply_handler if args.mode == "echo-reply" else _print_handler

    if args.once:
        delivery = worker.process_once(handler)
        if delivery is None:
            print("no delivery available")
        return

    worker.run_forever(handler)


if __name__ == "__main__":
    main()
