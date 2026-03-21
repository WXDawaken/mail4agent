from __future__ import annotations

"""Higher-level adapter for Codex-style mailbox handlers.

This module sits above `codex_mailbox_client.py` and `codex_mailbox_worker.py`.
It lets a handler focus on the message payload and return one of a few simple outcomes:

    - `None`: ack only
    - `dict`: send that payload as a reply, then ack
    - `ReplyAction(...)`: send a customized reply, then ack
    - `NackAction(...)`: retry later

Example:
    from codex_mailbox_adapter import CodexMailboxAdapter

    adapter = CodexMailboxAdapter.from_env()

    def handler(ctx):
        task = ctx.payload.get("task")
        return {"ok": True, "task": task, "handled_by": ctx.consumer_id}

    adapter.run_forever(handler)
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from codex_mailbox_client import MailboxHTTPClient
from codex_mailbox_worker import CodexMailboxWorker, WorkerDecision, ack_decision, nack_decision


@dataclass(frozen=True)
class ReplyAction:
    payload: dict[str, Any]
    subject: str | None = None
    message_type: str = "codex.reply"
    headers: dict[str, Any] | None = None
    idempotency_key: str | None = None
    from_address: str | None = None


@dataclass(frozen=True)
class NackAction:
    retry_after_seconds: int = 30
    last_error: str | None = None


class RetryableMailboxError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 30):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass
class CodexMailboxContext:
    client: MailboxHTTPClient
    delivery: dict[str, Any]
    consumer_id: str

    @property
    def payload(self) -> dict[str, Any]:
        value = self.delivery.get("payload")
        if isinstance(value, dict):
            return value
        return {}

    @property
    def headers(self) -> dict[str, Any] | None:
        value = self.delivery.get("headers")
        if isinstance(value, dict):
            return value
        return None

    @property
    def message_id(self) -> str:
        return str(self.delivery["message_id"])

    @property
    def thread_id(self) -> str | None:
        value = self.delivery.get("thread_id")
        return str(value) if value is not None else None

    @property
    def from_address(self) -> str:
        return str(self.delivery["from"])

    @property
    def to_address(self) -> str:
        return str(self.delivery["to"])

    @property
    def subject(self) -> str | None:
        value = self.delivery.get("subject")
        return str(value) if value is not None else None

    @property
    def correlation_id(self) -> str | None:
        value = self.delivery.get("correlation_id")
        return str(value) if value is not None else None

    @property
    def workflow_id(self) -> str | None:
        value = self.delivery.get("workflow_id")
        return str(value) if value is not None else None

    def load_thread(self) -> dict[str, Any] | None:
        return self.client.get_thread(message_id=self.message_id, allow_missing=True)

    def reply(
        self,
        payload: dict[str, Any],
        *,
        subject: str | None = None,
        message_type: str = "codex.reply",
        headers: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        from_address: str | None = None,
    ) -> dict[str, Any]:
        return self.client.send_reply(
            self.delivery,
            payload=payload,
            subject=subject,
            message_type=message_type,
            headers=headers,
            idempotency_key=idempotency_key,
            from_address=from_address,
        )

    def ack(self) -> WorkerDecision:
        return ack_decision()

    def nack(self, retry_after_seconds: int = 30, last_error: str | None = None) -> WorkerDecision:
        return nack_decision(retry_after_seconds=retry_after_seconds, last_error=last_error)


HandlerResult = None | dict[str, Any] | ReplyAction | NackAction | WorkerDecision
CodexHandler = Callable[[CodexMailboxContext], HandlerResult]


class CodexMailboxAdapter:
    def __init__(
        self,
        client: MailboxHTTPClient,
        *,
        worker: CodexMailboxWorker | None = None,
        inbox_address: str | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: int | None = None,
        poll_interval_seconds: float = 1.0,
        logger: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.logger = logger or print
        self.worker = worker or CodexMailboxWorker(
            client,
            inbox_address=inbox_address,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            poll_interval_seconds=poll_interval_seconds,
            logger=self.logger,
        )

    @classmethod
    def from_env(
        cls,
        *,
        prefix: str = "MAILBOX",
        inbox_address: str | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: int | None = None,
        poll_interval_seconds: float = 1.0,
        logger: Callable[[str], None] | None = None,
    ) -> "CodexMailboxAdapter":
        client = MailboxHTTPClient.from_env(prefix=prefix)
        return cls(
            client,
            inbox_address=inbox_address,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            poll_interval_seconds=poll_interval_seconds,
            logger=logger,
        )

    def process_once(self, handler: CodexHandler) -> dict[str, Any] | None:
        return self.worker.process_once(self._wrap_handler(handler))

    def run_forever(self, handler: CodexHandler) -> None:
        self.worker.run_forever(self._wrap_handler(handler))

    def _wrap_handler(self, handler: CodexHandler) -> Callable[[dict[str, Any], MailboxHTTPClient], WorkerDecision]:
        def wrapped(delivery: dict[str, Any], client: MailboxHTTPClient) -> WorkerDecision:
            context = CodexMailboxContext(
                client=client,
                delivery=delivery,
                consumer_id=self.worker.consumer_id,
            )
            try:
                result = handler(context)
            except RetryableMailboxError as exc:
                return nack_decision(
                    retry_after_seconds=exc.retry_after_seconds,
                    last_error=str(exc),
                )
            return self._normalize_result(context, result)

        return wrapped

    def _normalize_result(self, context: CodexMailboxContext, result: HandlerResult) -> WorkerDecision:
        if result is None:
            return ack_decision()

        if isinstance(result, WorkerDecision):
            return result

        if isinstance(result, NackAction):
            return nack_decision(
                retry_after_seconds=result.retry_after_seconds,
                last_error=result.last_error,
            )

        if isinstance(result, ReplyAction):
            context.reply(
                result.payload,
                subject=result.subject,
                message_type=result.message_type,
                headers=result.headers,
                idempotency_key=result.idempotency_key,
                from_address=result.from_address,
            )
            return ack_decision()

        if isinstance(result, dict):
            context.reply(result)
            return ack_decision()

        raise TypeError(f"unsupported handler result type: {type(result).__name__}")
