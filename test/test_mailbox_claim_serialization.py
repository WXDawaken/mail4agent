from __future__ import annotations

from test.mail4agent_test_support import MailboxHTTPFeatureTestCase, REVIEWER_ADDRESS


class MailboxClaimSerializationTests(MailboxHTTPFeatureTestCase):
    def test_default_claim_serializes_same_thread_within_one_mailbox(self) -> None:
        first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "claim-serialization", "step": 1},
            subject="claim serialization",
            message_type="codex.claim.serialization",
        )
        second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "claim-serialization", "step": 2},
            subject="claim serialization followup",
            message_type="codex.claim.serialization",
            thread_id=str(first["thread_id"]),
            in_reply_to_message_id=str(first["message_id"]),
        )

        claimed_first = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-serialization-first",
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed_first)
        self.assertEqual(str(claimed_first["message_id"]), str(first["message_id"]))
        self.assertEqual(str(claimed_first["serialization_scope"]), "mailbox_thread")

        blocked = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-serialization-second",
            lease_seconds=30,
        )
        self.assertIsNone(blocked)

        self.assertTrue(
            self.reviewer_harness_client.ack(
                delivery_id=int(claimed_first["delivery_id"]),
                claim_token=str(claimed_first["claim_token"]),
                actor="python-serialization-first",
            )
        )

        claimed_second = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-serialization-third",
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed_second)
        self.assertEqual(str(claimed_second["message_id"]), str(second["message_id"]))

    def test_delivery_scope_allows_followup_claim_from_same_thread(self) -> None:
        first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "claim-delivery-scope", "step": 1},
            subject="claim delivery scope",
            message_type="codex.claim.scope",
        )
        second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "claim-delivery-scope", "step": 2},
            subject="claim delivery scope followup",
            message_type="codex.claim.scope",
            thread_id=str(first["thread_id"]),
            in_reply_to_message_id=str(first["message_id"]),
        )

        claimed_first = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-delivery-scope-first",
            lease_seconds=30,
            serialization_scope="delivery",
        )
        self.assertIsNotNone(claimed_first)
        self.assertEqual(str(claimed_first["message_id"]), str(first["message_id"]))
        self.assertEqual(str(claimed_first["serialization_scope"]), "delivery")

        claimed_second = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-delivery-scope-second",
            lease_seconds=30,
            serialization_scope="delivery",
        )
        self.assertIsNotNone(claimed_second)
        self.assertEqual(str(claimed_second["message_id"]), str(second["message_id"]))
