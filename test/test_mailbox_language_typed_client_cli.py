from __future__ import annotations

import json
import unittest

from codex_mailbox_client import MailboxClientConfig, MailboxHTTPClient
from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    PLANNER_ADDRESS,
    REVIEWER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    login_role_session,
    request_json,
    run_client_json,
)


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
        },
        "transitions": [
            {"message": "QuoteReq", "from": "Init", "to": "AwaitDecision"},
            {"message": "Approve", "from": "AwaitDecision", "to": "Done"},
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


class MailboxLanguageTypedClientCliTests(MailboxHTTPFeatureTestCase):
    def _admin_client(self) -> MailboxHTTPClient:
        return MailboxHTTPClient(
            MailboxClientConfig(
                base_url=self.base_url,
                token=self.admin_token,
                from_address=OPERATOR_ADDRESS,
                inbox_address=OPERATOR_ADDRESS,
                timeout_seconds=5.0,
            )
        )

    def test_python_client_typed_helpers_cover_runtime_flow(self) -> None:
        admin_client = self._admin_client()

        orders = admin_client.register_protocol(protocol="Orders/v2", schema=orders_protocol_schema())
        plaintext = admin_client.register_protocol(protocol="PlainText/v1", schema=plaintext_protocol_schema())
        self.assertEqual(orders["protocol"], "Orders/v2")
        self.assertEqual(plaintext["protocol"], "PlainText/v1")

        listed = {str(item["protocol"]): item for item in admin_client.list_protocols()}
        self.assertIn("Orders/v2", listed)
        self.assertIn("PlainText/v1", listed)

        reviewer_bindings = admin_client.set_mailbox_protocols(address=REVIEWER_ADDRESS, accepts=["Orders/v2"])
        planner_bindings = admin_client.set_mailbox_protocols(
            address=PLANNER_ADDRESS,
            accepts=["PlainText/v1"],
            default_protocol="PlainText/v1",
        )
        self.assertEqual(reviewer_bindings["address"], REVIEWER_ADDRESS)
        self.assertEqual(planner_bindings["default_protocol"], "PlainText/v1")

        resolved_bindings = admin_client.get_mailbox_protocols(address=REVIEWER_ADDRESS)
        self.assertEqual([item["protocol"] for item in resolved_bindings["accepted_protocols"]], ["Orders/v2"])

        created = admin_client.typed_send(
            to_address=REVIEWER_ADDRESS,
            protocol="Orders/v2",
            message="QuoteReq",
            payload={"order_id": "py-1", "items": ["sku-1"]},
            subject="Need typed review",
        )
        self.assertEqual(created["protocol"], "Orders/v2")
        self.assertEqual(created["state"], "AwaitDecision")

        advanced = admin_client.typed_send(
            to_address=REVIEWER_ADDRESS,
            thread_id=str(created["thread_id"]),
            protocol="Orders/v2",
            message="Approve",
            payload={"order_id": "py-1"},
        )
        self.assertEqual(advanced["state"], "Done")
        self.assertEqual(str(advanced["thread_id"]), str(created["thread_id"]))

        spawned = admin_client.typed_spawn(
            to_address=PLANNER_ADDRESS,
            from_thread_id=str(created["thread_id"]),
            protocol="PlainText/v1",
            message="Text",
            payload={"body": "Please follow up on the approved order."},
            subject="Typed follow-up",
        )
        self.assertEqual(spawned["protocol"], "PlainText/v1")
        self.assertEqual(spawned["parent_thread_id"], str(created["thread_id"]))

        handoff = admin_client.typed_handoff(
            from_thread_id=str(created["thread_id"]),
            to_thread_id=str(spawned["thread_id"]),
            actor="python-typed-client",
            metadata={"reason": "approved order follow-up"},
        )
        self.assertEqual(handoff["actor"], "python-typed-client")

        parent_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(created["thread_id"])},
        )["thread"]
        self.assertEqual(parent_thread["state"], "Done")
        self.assertEqual(len(parent_thread["outgoing_handoffs"]), 1)

        child_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(spawned["thread_id"])},
        )["thread"]
        self.assertEqual(child_thread["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(child_thread["parent_thread_id"], str(created["thread_id"]))
        self.assertEqual(len(child_thread["incoming_handoffs"]), 1)

    def test_cli_typed_commands_work_without_hand_written_envelope_json(self) -> None:
        planner_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="planner",
            consumer_id="python-typed-cli-planner",
            session_name="typed-cli",
        )

        env = self.session_env(
            planner_session,
            from_address=PLANNER_ADDRESS,
            inbox_address=PLANNER_ADDRESS,
        )
        env["MAILBOX_ADMIN_TOKEN"] = self.admin_token

        register_orders = run_client_json(
            env,
            "register-protocol",
            "--protocol",
            "Orders/v2",
            "--schema-json",
            json.dumps(orders_protocol_schema()),
        )
        self.assertEqual(register_orders["protocol"]["protocol"], "Orders/v2")

        register_plaintext = run_client_json(
            env,
            "register-protocol",
            "--protocol",
            "PlainText/v1",
            "--schema-json",
            json.dumps(plaintext_protocol_schema()),
        )
        self.assertEqual(register_plaintext["protocol"]["protocol"], "PlainText/v1")

        listed = run_client_json(env, "list-protocols")
        listed_protocols = {str(item["protocol"]): item for item in listed["protocols"]}
        self.assertIn("Orders/v2", listed_protocols)
        self.assertIn("PlainText/v1", listed_protocols)

        reviewer_bindings = run_client_json(
            env,
            "set-mailbox-protocols",
            "--address",
            REVIEWER_ADDRESS,
            "--accepts",
            "Orders/v2",
        )
        self.assertEqual(
            [item["protocol"] for item in reviewer_bindings["bindings"]["accepted_protocols"]],
            ["Orders/v2"],
        )

        planner_bindings = run_client_json(
            env,
            "set-mailbox-protocols",
            "--address",
            PLANNER_ADDRESS,
            "--accepts",
            "PlainText/v1",
            "--default-protocol",
            "PlainText/v1",
        )
        self.assertEqual(planner_bindings["bindings"]["default_protocol"], "PlainText/v1")

        resolved_bindings = run_client_json(
            env,
            "get-mailbox-protocols",
            "--address",
            REVIEWER_ADDRESS,
        )
        self.assertEqual(
            [item["protocol"] for item in resolved_bindings["bindings"]["accepted_protocols"]],
            ["Orders/v2"],
        )

        created = run_client_json(
            env,
            "typed-send",
            "--to-address",
            REVIEWER_ADDRESS,
            "--protocol",
            "Orders/v2",
            "--message",
            "QuoteReq",
            "--subject",
            "Need typed cli review",
            "--payload-json",
            json.dumps({"order_id": "cli-1", "items": ["sku-1"]}),
        )
        self.assertEqual(created["protocol"], "Orders/v2")
        self.assertEqual(created["state"], "AwaitDecision")

        advanced = run_client_json(
            env,
            "typed-send",
            "--to-address",
            REVIEWER_ADDRESS,
            "--thread-id",
            str(created["thread_id"]),
            "--protocol",
            "Orders/v2",
            "--message",
            "Approve",
            "--payload-json",
            json.dumps({"order_id": "cli-1"}),
        )
        self.assertEqual(advanced["state"], "Done")

        spawned = run_client_json(
            env,
            "typed-spawn",
            "--to-address",
            PLANNER_ADDRESS,
            "--from-thread-id",
            str(created["thread_id"]),
            "--protocol",
            "PlainText/v1",
            "--message",
            "Text",
            "--payload-json",
            json.dumps({"body": "Please follow up via the planner thread."}),
        )
        self.assertEqual(spawned["protocol"], "PlainText/v1")
        self.assertEqual(spawned["parent_thread_id"], str(created["thread_id"]))

        handoff = run_client_json(
            env,
            "typed-handoff",
            "--from-thread-id",
            str(created["thread_id"]),
            "--to-thread-id",
            str(spawned["thread_id"]),
            "--actor",
            "cli-typed-runtime",
            "--metadata-json",
            json.dumps({"reason": "bridge typed child thread"}),
        )
        self.assertEqual(handoff["handoff"]["actor"], "cli-typed-runtime")

        reviewer_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(created["thread_id"])},
        )["thread"]
        self.assertEqual(reviewer_thread["state"], "Done")

        planner_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": str(spawned["thread_id"])},
        )["thread"]
        self.assertEqual(planner_thread["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(planner_thread["parent_thread_id"], str(created["thread_id"]))
        self.assertEqual(len(planner_thread["incoming_handoffs"]), 1)


if __name__ == "__main__":
    unittest.main()
