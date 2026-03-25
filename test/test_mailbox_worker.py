from __future__ import annotations

import unittest

from mailbox_worker import ConsumeConfig, build_handler_env, run_consume_loop


class FakeMailboxClient:
    def __init__(self, deliveries: list[dict[str, object]]) -> None:
        self._deliveries = list(deliveries)
        self.claim_calls: list[dict[str, object]] = []
        self.ack_calls: list[dict[str, object]] = []
        self.nack_calls: list[dict[str, object]] = []

    def claim(
        self,
        *,
        to_address: str | None = None,
        to_addresses: list[str] | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        serialization_scope: str = "mailbox_thread",
    ) -> dict[str, object] | None:
        self.claim_calls.append(
            {
                "to_address": to_address,
                "to_addresses": list(to_addresses or []),
                "consumer_id": consumer_id,
                "lease_seconds": lease_seconds,
                "serialization_scope": serialization_scope,
            }
        )
        if self._deliveries:
            return dict(self._deliveries.pop(0))
        return None

    def ack(self, *, delivery_id: int, claim_token: str, actor: str | None = None) -> bool:
        self.ack_calls.append(
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "actor": actor,
            }
        )
        return True

    def nack(
        self,
        *,
        delivery_id: int,
        claim_token: str,
        retry_after_seconds: int = 30,
        last_error: str | None = None,
        actor: str | None = None,
    ) -> bool:
        self.nack_calls.append(
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "retry_after_seconds": retry_after_seconds,
                "last_error": last_error,
                "actor": actor,
            }
        )
        return True

    def heartbeat(self, *, delivery_id: int, claim_token: str, lease_seconds: int = 60) -> bool:
        return True


class MailboxWorkerTests(unittest.TestCase):
    def test_run_consume_loop_acks_successful_handler(self) -> None:
        client = FakeMailboxClient(
            [
                {
                    "delivery_id": 101,
                    "message_id": "msg-101",
                    "thread_id": "thread-101",
                    "claim_token": "claim-101",
                    "from": "planner@example.test",
                    "to": "operator@example.test",
                }
            ]
        )
        config = ConsumeConfig(
            to_address="operator@example.test",
            to_addresses=(),
            consumer_id="worker-ack",
            serialization_scope="mailbox_thread",
            lease_seconds=120,
            heartbeat_interval_seconds=10.0,
            poll_interval_seconds=0.01,
            retry_after_seconds=30,
            ack_exit_codes=frozenset({0}),
            once=True,
            max_deliveries=None,
        )

        summary = run_consume_loop(client, config, lambda _delivery: 0)

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["acked"], 1)
        self.assertEqual(summary["nacked"], 0)
        self.assertEqual(summary["last_delivery_id"], 101)
        self.assertEqual(len(client.ack_calls), 1)
        self.assertEqual(client.ack_calls[0]["delivery_id"], 101)
        self.assertEqual(client.ack_calls[0]["actor"], "worker-ack")

    def test_run_consume_loop_nacks_failed_handler(self) -> None:
        client = FakeMailboxClient(
            [
                {
                    "delivery_id": 202,
                    "message_id": "msg-202",
                    "thread_id": "thread-202",
                    "claim_token": "claim-202",
                    "from": "planner@example.test",
                    "to": "operator@example.test",
                }
            ]
        )
        config = ConsumeConfig(
            to_address="operator@example.test",
            to_addresses=(),
            consumer_id="worker-nack",
            serialization_scope="mailbox_thread",
            lease_seconds=120,
            heartbeat_interval_seconds=10.0,
            poll_interval_seconds=0.01,
            retry_after_seconds=45,
            ack_exit_codes=frozenset({0}),
            once=True,
            max_deliveries=None,
        )

        summary = run_consume_loop(client, config, lambda _delivery: 7)

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["acked"], 0)
        self.assertEqual(summary["nacked"], 1)
        self.assertEqual(len(client.nack_calls), 1)
        self.assertEqual(client.nack_calls[0]["delivery_id"], 202)
        self.assertEqual(client.nack_calls[0]["retry_after_seconds"], 45)
        self.assertIn("status 7", str(client.nack_calls[0]["last_error"]))

    def test_run_consume_loop_passes_serialization_scope_to_claim(self) -> None:
        client = FakeMailboxClient([])
        config = ConsumeConfig(
            to_address="operator@example.test",
            to_addresses=(),
            consumer_id="worker-scope",
            serialization_scope="delivery",
            lease_seconds=120,
            heartbeat_interval_seconds=10.0,
            poll_interval_seconds=0.01,
            retry_after_seconds=30,
            ack_exit_codes=frozenset({0}),
            once=True,
            max_deliveries=None,
        )

        summary = run_consume_loop(client, config, lambda _delivery: 0)

        self.assertEqual(summary["processed"], 0)
        self.assertEqual(len(client.claim_calls), 1)
        self.assertEqual(client.claim_calls[0]["serialization_scope"], "delivery")

    def test_build_handler_env_exports_delivery_fields(self) -> None:
        env = build_handler_env(
            {
                "delivery_id": 303,
                "message_id": "msg-303",
                "thread_id": "thread-303",
                "claim_token": "claim-303",
                "from": "planner@example.test",
                "to": "operator@example.test",
            },
            base_env={"EXISTING": "1"},
        )

        self.assertEqual(env["EXISTING"], "1")
        self.assertEqual(env["MAILBOX_DELIVERY_ID"], "303")
        self.assertEqual(env["MAILBOX_MESSAGE_ID"], "msg-303")
        self.assertEqual(env["MAILBOX_THREAD_ID"], "thread-303")
        self.assertEqual(env["MAILBOX_CLAIM_TOKEN"], "claim-303")
        self.assertEqual(env["MAILBOX_FROM_ADDRESS"], "planner@example.test")
        self.assertEqual(env["MAILBOX_TO_ADDRESS"], "operator@example.test")
