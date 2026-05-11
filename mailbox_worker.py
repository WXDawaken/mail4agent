from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codex_mailbox_client import MailboxHTTPClient


@dataclass(frozen=True)
class ConsumeConfig:
    to_address: str | None
    to_addresses: tuple[str, ...]
    consumer_id: str
    serialization_scope: str
    lease_seconds: int
    heartbeat_interval_seconds: float
    poll_interval_seconds: float
    retry_after_seconds: int
    ack_exit_codes: frozenset[int]
    once: bool = False
    max_deliveries: int | None = None
    max_empty_polls: int | None = None


ConsumeHandler = Callable[[dict[str, Any]], int]
DeliveryCompletionCallback = Callable[[dict[str, Any], int, str], None]
IdlePollCallback = Callable[[], None]


def run_consume_loop(
    client: MailboxHTTPClient,
    config: ConsumeConfig,
    handler: ConsumeHandler,
    on_delivery_complete: DeliveryCompletionCallback | None = None,
    on_idle: IdlePollCallback | None = None,
) -> dict[str, Any]:
    processed = 0
    acked = 0
    nacked = 0
    empty_polls = 0
    last_delivery_id: int | None = None

    while True:
        delivery = client.claim(
            to_address=config.to_address,
            to_addresses=list(config.to_addresses) if config.to_addresses else None,
            consumer_id=config.consumer_id,
            lease_seconds=config.lease_seconds,
            serialization_scope=config.serialization_scope,
        )
        if delivery is None:
            empty_polls += 1
            if on_idle is not None:
                on_idle()
            if config.once:
                break
            if config.max_empty_polls is not None and empty_polls >= config.max_empty_polls:
                break
            if config.max_deliveries is not None and processed >= config.max_deliveries:
                break
            time.sleep(max(0.0, config.poll_interval_seconds))
            continue

        processed += 1
        last_delivery_id = int(delivery["delivery_id"])
        return_code = _run_handler_with_heartbeat(
            client,
            delivery,
            handler,
            heartbeat_interval_seconds=config.heartbeat_interval_seconds,
            lease_seconds=config.lease_seconds,
        )
        if return_code in config.ack_exit_codes:
            ok = client.ack(
                delivery_id=int(delivery["delivery_id"]),
                claim_token=str(delivery["claim_token"]),
                actor=config.consumer_id,
            )
            if not ok:
                raise RuntimeError(f"ack rejected for delivery {delivery['delivery_id']}")
            acked += 1
            completion_status = "acked"
        else:
            ok = client.nack(
                delivery_id=int(delivery["delivery_id"]),
                claim_token=str(delivery["claim_token"]),
                retry_after_seconds=config.retry_after_seconds,
                last_error=f"handler exited with status {return_code}",
                actor=config.consumer_id,
            )
            if not ok:
                raise RuntimeError(f"nack rejected for delivery {delivery['delivery_id']}")
            nacked += 1
            completion_status = "retry_pending"

        if on_delivery_complete is not None:
            on_delivery_complete(delivery, return_code, completion_status)

        if config.once:
            break
        if config.max_deliveries is not None and processed >= config.max_deliveries:
            break

    return {
        "ok": True,
        "processed": processed,
        "acked": acked,
        "nacked": nacked,
        "empty_polls": empty_polls,
        "last_delivery_id": last_delivery_id,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
    }


def run_subprocess_handler(
    delivery: dict[str, Any],
    command: list[str],
    *,
    cwd: str | None = None,
    base_env: dict[str, str] | None = None,
) -> int:
    completed = subprocess.run(
        command,
        input=json.dumps(delivery, ensure_ascii=False),
        text=True,
        env=build_handler_env(delivery, base_env=base_env),
        cwd=cwd or str(Path.cwd()),
        check=False,
    )
    return int(completed.returncode)


def build_handler_env(
    delivery: dict[str, Any],
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_env or os.environ.copy())
    env["MAILBOX_DELIVERY_ID"] = str(delivery.get("delivery_id") or "")
    env["MAILBOX_MESSAGE_ID"] = str(delivery.get("message_id") or "")
    env["MAILBOX_THREAD_ID"] = str(delivery.get("thread_id") or "")
    env["MAILBOX_CLAIM_TOKEN"] = str(delivery.get("claim_token") or "")
    env["MAILBOX_FROM_ADDRESS"] = str(delivery.get("from") or "")
    env["MAILBOX_TO_ADDRESS"] = str(delivery.get("to") or "")
    return env


def _run_handler_with_heartbeat(
    client: MailboxHTTPClient,
    delivery: dict[str, Any],
    handler: ConsumeHandler,
    *,
    heartbeat_interval_seconds: float,
    lease_seconds: int,
) -> int:
    stop_event = threading.Event()
    heartbeat_errors: list[BaseException] = []
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(
            client,
            delivery,
            stop_event,
            heartbeat_errors,
            heartbeat_interval_seconds,
            lease_seconds,
        ),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        return int(handler(delivery))
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(1.0, heartbeat_interval_seconds + 1.0))
        if heartbeat_errors:
            raise RuntimeError(f"heartbeat failed during consume: {heartbeat_errors[0]}")


def _heartbeat_loop(
    client: MailboxHTTPClient,
    delivery: dict[str, Any],
    stop_event: threading.Event,
    heartbeat_errors: list[BaseException],
    heartbeat_interval_seconds: float,
    lease_seconds: int,
) -> None:
    while not stop_event.wait(heartbeat_interval_seconds):
        try:
            ok = client.heartbeat(
                delivery_id=int(delivery["delivery_id"]),
                claim_token=str(delivery["claim_token"]),
                lease_seconds=lease_seconds,
            )
            if not ok:
                heartbeat_errors.append(RuntimeError("heartbeat rejected by mailbox server"))
                return
        except Exception as exc:
            heartbeat_errors.append(exc)
            return
