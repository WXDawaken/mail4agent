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


def orders_protocol_schema() -> dict[str, object]:
    return {
        "states": ["Init", "AwaitDecision", "Done"],
        "start": "Init",
        "messages": {
            "QuoteReq": {
                "required": ["order_id", "items"],
                "optional": [],
                "allow_additional_fields": False,
            },
            "Approve": {
                "required": ["order_id"],
                "optional": [],
                "allow_additional_fields": False,
            },
            "Cancel": {
                "required": ["order_id"],
                "optional": ["reason"],
                "allow_additional_fields": False,
            },
        },
        "transitions": [
            {"message": "QuoteReq", "from": "Init", "to": "AwaitDecision"},
            {"message": "Approve", "from": "AwaitDecision", "to": "Done"},
            {"message": "Cancel", "from": "AwaitDecision", "to": "Done"},
        ],
    }


def plaintext_protocol_schema() -> dict[str, object]:
    return {
        "states": ["Init", "Open"],
        "start": "Init",
        "messages": {
            "Text": {
                "required": ["body"],
                "optional": ["subject"],
                "allow_additional_fields": False,
            }
        },
        "transitions": [
            {"message": "Text", "from": "Init", "to": "Open"},
        ],
    }


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

    def test_execute_message_envelope_advances_runtime_and_supports_spawn(self) -> None:
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "Orders/v2", "schema": orders_protocol_schema()},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "PlainText/v1", "schema": plaintext_protocol_schema()},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/set_mailbox_protocols",
            token=self.admin_token,
            body={"address": REVIEWER_ADDRESS, "accepts": ["Orders/v2"]},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/set_mailbox_protocols",
            token=self.admin_token,
            body={
                "address": PLANNER_ADDRESS,
                "accepts": ["PlainText/v1"],
                "default_protocol": "PlainText/v1",
            },
        )

        created = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            body={
                "from_address": OPERATOR_ADDRESS,
                "subject": "Need order review",
                "envelope": {
                    "op": "send",
                    "target_kind": "mailbox",
                    "mailbox_address": REVIEWER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "QuoteReq",
                    "payload": {"order_id": "123", "items": ["sku-1"]},
                },
            },
        )["result"]
        self.assertEqual(created["protocol"], "Orders/v2")
        self.assertEqual(created["state"], "AwaitDecision")
        self.assertEqual(created["message_type"], "Orders/v2.QuoteReq")
        self.assertEqual(created["thread"]["protocol"]["protocol"], "Orders/v2")
        self.assertEqual(created["thread"]["state"], "AwaitDecision")
        self.assertEqual(created["thread"]["message_count"], 1)

        advanced = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            body={
                "from_address": OPERATOR_ADDRESS,
                "subject": "Approved",
                "envelope": {
                    "op": "send",
                    "target_kind": "thread",
                    "thread_id": str(created["thread_id"]),
                    "to_address": REVIEWER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "Approve",
                    "payload": {"order_id": "123"},
                },
            },
        )["result"]
        self.assertEqual(advanced["thread_id"], created["thread_id"])
        self.assertEqual(advanced["state"], "Done")
        self.assertEqual(advanced["thread"]["state"], "Done")
        self.assertEqual(advanced["thread"]["message_count"], 2)

        spawned = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            body={
                "from_address": OPERATOR_ADDRESS,
                "subject": "Plaintext summary",
                "envelope": {
                    "op": "spawn",
                    "target_kind": "mailbox",
                    "mailbox_address": PLANNER_ADDRESS,
                    "protocol": "PlainText",
                    "version": "v1",
                    "msg_type": "Text",
                    "parent_thread_id": str(created["thread_id"]),
                    "payload": {"body": "Order 123 was approved"},
                },
            },
        )["result"]
        self.assertNotEqual(spawned["thread_id"], created["thread_id"])
        self.assertEqual(spawned["protocol"], "PlainText/v1")
        self.assertEqual(spawned["state"], "Open")
        self.assertEqual(spawned["parent_thread_id"], str(created["thread_id"]))
        self.assertEqual(spawned["thread"]["parent_thread_id"], str(created["thread_id"]))

        handoff = request_json(
            self.base_url,
            "POST",
            "/admin/execute_handoff_event",
            token=self.admin_token,
            body={
                "event": {
                    "op": "handoff",
                    "from_thread_id": str(created["thread_id"]),
                    "to_thread_id": str(spawned["thread_id"]),
                    "metadata": {"reason": "control-plane transfer"},
                },
                "actor": "python-envelope-admin",
            },
        )
        self.assertEqual(handoff["handoff"]["actor"], "python-envelope-admin")

        source_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(created["thread_id"])},
        )["thread"]
        self.assertEqual(len(source_thread["outgoing_handoffs"]), 1)
        self.assertEqual(
            source_thread["outgoing_handoffs"][0]["related_thread_id"],
            str(spawned["thread_id"]),
        )

    def test_execute_message_envelope_returns_runtime_error_codes(self) -> None:
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "Orders/v2", "schema": orders_protocol_schema()},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/register_protocol",
            token=self.admin_token,
            body={"protocol": "Support/v1", "schema": plaintext_protocol_schema()},
        )
        request_json(
            self.base_url,
            "POST",
            "/admin/set_mailbox_protocols",
            token=self.admin_token,
            body={"address": REVIEWER_ADDRESS, "accepts": ["Orders/v2"]},
        )

        rejected_mailbox = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            expected_status=400,
            body={
                "from_address": OPERATOR_ADDRESS,
                "envelope": {
                    "op": "send",
                    "target_kind": "mailbox",
                    "mailbox_address": PLANNER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "QuoteReq",
                    "payload": {"order_id": "123", "items": ["sku-1"]},
                },
            },
        )
        self.assertEqual(rejected_mailbox["error_code"], "E_MAILBOX_PROTOCOL_NOT_ACCEPTED")

        created = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            body={
                "from_address": OPERATOR_ADDRESS,
                "envelope": {
                    "op": "send",
                    "target_kind": "mailbox",
                    "mailbox_address": REVIEWER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "QuoteReq",
                    "payload": {"order_id": "123", "items": ["sku-1"]},
                },
            },
        )["result"]

        mismatched_protocol = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            expected_status=400,
            body={
                "from_address": OPERATOR_ADDRESS,
                "envelope": {
                    "op": "send",
                    "target_kind": "thread",
                    "thread_id": str(created["thread_id"]),
                    "to_address": REVIEWER_ADDRESS,
                    "protocol": "Support",
                    "version": "v1",
                    "msg_type": "Text",
                    "payload": {"body": "wrong protocol"},
                },
            },
        )
        self.assertEqual(mismatched_protocol["error_code"], "E_THREAD_PROTOCOL_MISMATCH")

        invalid_transition = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            expected_status=400,
            body={
                "from_address": OPERATOR_ADDRESS,
                "envelope": {
                    "op": "send",
                    "target_kind": "mailbox",
                    "mailbox_address": REVIEWER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "Approve",
                    "payload": {"order_id": "123"},
                },
            },
        )
        self.assertEqual(invalid_transition["error_code"], "E_STATE_TRANSITION_INVALID")

        invalid_payload = request_json(
            self.base_url,
            "POST",
            "/admin/execute_message_envelope",
            token=self.admin_token,
            expected_status=400,
            body={
                "from_address": OPERATOR_ADDRESS,
                "envelope": {
                    "op": "send",
                    "target_kind": "mailbox",
                    "mailbox_address": REVIEWER_ADDRESS,
                    "protocol": "Orders",
                    "version": "v2",
                    "msg_type": "QuoteReq",
                    "payload": {"items": ["sku-1"]},
                },
            },
        )
        self.assertEqual(invalid_payload["error_code"], "E_PAYLOAD_SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
