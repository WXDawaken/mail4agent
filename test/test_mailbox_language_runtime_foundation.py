from __future__ import annotations

import unittest

from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    PLANNER_ADDRESS,
    REVIEWER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    login_role_session,
    request_json,
)


def protocol_map(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["protocol"]): item for item in items}


class MailboxLanguageRuntimeFoundationTests(MailboxHTTPFeatureTestCase):
    def test_admin_protocol_registry_and_mailbox_bindings_round_trip(self) -> None:
        plaintext = request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={
                "protocol": "PlainText/v1",
                "schema": {"entry_message": "Text"},
            },
        )
        orders = request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={
                "protocol": "Orders/v2",
                "schema": {"states": ["Init", "AwaitDecision", "Done"]},
            },
        )
        self.assertEqual(plaintext["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(orders["protocol"]["protocol"], "Orders/v2")

        listed = request_json(
            self.base_url,
            "GET",
            "/admin/list_protocols",
            token=self.admin_token,
        )
        protocols = protocol_map(listed["protocols"])
        self.assertIn("PlainText/v1", protocols)
        self.assertIn("Orders/v2", protocols)

        configured = request_json(
            self.base_url,
            "POST",
            "/admin/set_mailbox_protocols",
            token=self.admin_token,
            body={
                "address": REVIEWER_ADDRESS,
                "accepts": ["PlainText/v1", "Orders/v2"],
                "default_protocol": "PlainText/v1",
            },
        )
        bindings = configured["bindings"]
        self.assertEqual(bindings["address"], REVIEWER_ADDRESS)
        self.assertEqual(bindings["default_protocol"], "PlainText/v1")
        self.assertEqual(
            [item["protocol"] for item in bindings["accepted_protocols"]],
            ["PlainText/v1", "Orders/v2"],
        )

        resolved = request_json(
            self.base_url,
            "GET",
            "/admin/get_mailbox_protocols",
            token=self.admin_token,
            query={"address": REVIEWER_ADDRESS},
        )
        self.assertEqual(resolved["bindings"]["default_protocol"], "PlainText/v1")

        listed_mailboxes = request_json(
            self.base_url,
            "GET",
            "/admin/list_mailboxes",
            token=self.admin_token,
        )
        reviewer_mailboxes = [
            item
            for item in listed_mailboxes["mailboxes"]
            if isinstance(item, dict) and item.get("address") == REVIEWER_ADDRESS
        ]
        self.assertEqual(len(reviewer_mailboxes), 1)
        reviewer_mailbox = reviewer_mailboxes[0]
        self.assertEqual(reviewer_mailbox["default_protocol"], "PlainText/v1")
        self.assertEqual(
            [item["protocol"] for item in reviewer_mailbox["accepted_protocols"]],
            ["PlainText/v1", "Orders/v2"],
        )

    def test_thread_runtime_metadata_and_handoff_are_visible(self) -> None:
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "PlainText/v1"},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "Orders/v2"},
        )

        planner_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="planner",
            consumer_id="python-runtime-planner",
            session_name="runtime-foundation",
        )

        text_thread = self.operator_harness_client.send(
            to_address=PLANNER_ADDRESS,
            payload={"kind": "plain"},
            subject="text thread",
            message_type="runtime.text",
        )
        order_thread = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"kind": "typed"},
            subject="order thread",
            message_type="runtime.order",
        )

        text_runtime = request_json(
            self.base_url,
            "POST",
            "/admin/set_thread_runtime",
            token=self.admin_token,
            body={
                "thread_id": str(text_thread["thread_id"]),
                "protocol": "PlainText/v1",
                "state": "Open",
            },
        )
        self.assertEqual(text_runtime["thread"]["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(text_runtime["thread"]["state"], "Open")

        order_runtime = request_json(
            self.base_url,
            "POST",
            "/admin/set_thread_runtime",
            token=self.admin_token,
            body={
                "thread_id": str(order_thread["thread_id"]),
                "protocol": "Orders/v2",
                "state": "AwaitDecision",
                "parent_thread_id": str(text_thread["thread_id"]),
            },
        )
        self.assertEqual(order_runtime["thread"]["protocol"]["protocol"], "Orders/v2")
        self.assertEqual(order_runtime["thread"]["state"], "AwaitDecision")
        self.assertEqual(order_runtime["thread"]["parent_thread_id"], str(text_thread["thread_id"]))

        handoff = request_json(
            self.base_url,
            "POST",
            "/admin/record_thread_handoff",
            token=self.admin_token,
            body={
                "from_thread_id": str(text_thread["thread_id"]),
                "to_thread_id": str(order_thread["thread_id"]),
                "actor": "python-runtime-admin",
                "metadata": {"reason": "spawned typed follow-up"},
            },
        )
        self.assertEqual(handoff["handoff"]["actor"], "python-runtime-admin")

        admin_text_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(text_thread["thread_id"])},
        )["thread"]
        self.assertEqual(admin_text_thread["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(admin_text_thread["state"], "Open")
        self.assertEqual(len(admin_text_thread["outgoing_handoffs"]), 1)
        self.assertEqual(
            admin_text_thread["outgoing_handoffs"][0]["related_thread_id"],
            str(order_thread["thread_id"]),
        )

        admin_order_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(order_thread["thread_id"])},
        )["thread"]
        self.assertEqual(admin_order_thread["protocol"]["protocol"], "Orders/v2")
        self.assertEqual(admin_order_thread["parent_thread_id"], str(text_thread["thread_id"]))
        self.assertEqual(len(admin_order_thread["incoming_handoffs"]), 1)
        self.assertEqual(
            admin_order_thread["incoming_handoffs"][0]["related_thread_id"],
            str(text_thread["thread_id"]),
        )

        planner_view = planner_session.get_thread(thread_id=str(text_thread["thread_id"]))
        self.assertIsNotNone(planner_view)
        assert planner_view is not None
        self.assertEqual(planner_view["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(planner_view["state"], "Open")
        self.assertNotIn("outgoing_handoffs", planner_view)


if __name__ == "__main__":
    unittest.main()
