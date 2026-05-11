from __future__ import annotations

import unittest

from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    PLANNER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    auth_token_for_client,
    login_role_session,
    run_client_json,
)


INTEGRATOR_ADDRESS = "integrator@mail4agent.codex"


class SessionHandoffTests(MailboxHTTPFeatureTestCase):
    def test_cli_handoff_wraps_visible_source_message_for_integrator(self) -> None:
        planner_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="planner",
            consumer_id="python-handoff-planner",
            session_name="dogfood-handoff-planner",
        )
        integrator_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="integrator",
            consumer_id="python-handoff-integrator",
            session_name="dogfood-handoff-integrator",
        )

        request_message = planner_session.send(
            to_address=OPERATOR_ADDRESS,
            payload={"request_kind": "bounded_patch_request", "target": "routing_explain"},
            subject="Need supplier review",
            message_type="integration_request",
            workflow_id="wf-handoff",
        )
        self.pause_for_ordering()
        supplier_reply = self.operator_harness_client.send(
            to_address=PLANNER_ADDRESS,
            payload={
                "artifact_summary": "routing explain is ready",
                "validation": ["python -m unittest test.test_session_inbox_listing -v"],
            },
            subject="Re: Need supplier review",
            message_type="integration_offer",
            thread_id=str(request_message["thread_id"]),
            in_reply_to_message_id=str(request_message["message_id"]),
            workflow_id="wf-handoff",
        )

        env = self.base_env()
        env["MAILBOX_SESSION_TOKEN"] = auth_token_for_client(planner_session)
        env.pop("MAILBOX_TOKEN", None)
        env.pop("MAILBOX_INBOX_ADDRESS", None)
        env.pop("MAILBOX_FROM_ADDRESS", None)
        handoff_result = run_client_json(
            env,
            "handoff",
            "--message-id",
            str(supplier_reply["message_id"]),
            "--to-address",
            INTEGRATOR_ADDRESS,
            "--message-type",
            "integration_handoff",
            "--summary",
            "Please verify the supplier response before we accept it.",
            "--instructions",
            "Reply with acceptance if the validation looks sufficient.",
        )

        self.assertEqual(handoff_result.get("ok"), True)
        self.assertEqual(handoff_result.get("target_address"), INTEGRATOR_ADDRESS)
        self.assertEqual(str(handoff_result["thread_id"]), str(request_message["thread_id"]))
        source_message = handoff_result.get("source_message")
        self.assertIsInstance(source_message, dict)
        assert isinstance(source_message, dict)
        self.assertEqual(str(source_message["message_id"]), str(supplier_reply["message_id"]))

        integrator_view = integrator_session.get_message(str(handoff_result["message_id"]))
        self.assertIsNotNone(integrator_view)
        assert integrator_view is not None
        self.assertEqual(integrator_view["message_type"], "integration_handoff")
        payload = integrator_view["payload"]
        self.assertEqual(payload["kind"], "mailbox_handoff")
        self.assertEqual(
            payload["summary"],
            "Please verify the supplier response before we accept it.",
        )
        self.assertEqual(
            payload["instructions"],
            "Reply with acceptance if the validation looks sufficient.",
        )
        self.assertEqual(payload["source"]["message_id"], str(supplier_reply["message_id"]))
        self.assertEqual(payload["source"]["thread_id"], str(request_message["thread_id"]))
        self.assertEqual(payload["source"]["from"], OPERATOR_ADDRESS)
        self.assertEqual(payload["source"]["to"], [PLANNER_ADDRESS])
        self.assertEqual(payload["source"]["workflow_id"], "wf-handoff")
        self.assertEqual(
            payload["source"]["payload"]["artifact_summary"],
            "routing explain is ready",
        )

        integrator_thread = integrator_session.get_thread(message_id=str(handoff_result["message_id"]))
        self.assertIsNotNone(integrator_thread)
        assert integrator_thread is not None
        self.assertEqual(integrator_thread["message_count"], 1)

        planner_thread = planner_session.get_thread(thread_id=str(request_message["thread_id"]))
        self.assertIsNotNone(planner_thread)
        assert planner_thread is not None
        self.assertEqual(planner_thread["message_count"], 3)


if __name__ == "__main__":
    unittest.main()
