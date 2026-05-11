from __future__ import annotations

import unittest

from test.mail4agent_test_support import (
    MailboxHTTPFeatureTestCase,
    OPERATOR_ADDRESS,
    REVIEWER_ADDRESS,
    auth_token_for_client,
    login_role_session,
    request_json,
    run_client_json,
)


def thread_items(payload: dict[str, object]) -> list[dict[str, object]]:
    items = payload.get("threads")
    if not isinstance(items, list):
        raise AssertionError(f"expected threads list, got: {payload!r}")
    return [item for item in items if isinstance(item, dict)]


def summary_for_thread(payload: dict[str, object], thread_id: str) -> dict[str, object]:
    matches = [item for item in thread_items(payload) if str(item.get("thread_id")) == thread_id]
    if len(matches) != 1:
        raise AssertionError(f"expected one summary for thread {thread_id}, got: {matches!r}")
    return matches[0]


class SessionThreadDefaultInboxTests(MailboxHTTPFeatureTestCase):
    def test_http_thread_state_routes_use_session_default_inbox_address(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-thread-default-http",
            session_name="fixture-defaults-http",
        )
        session_token = auth_token_for_client(reviewer_session)

        whoami = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=session_token,
        )
        session = whoami.get("session")
        self.assertIsInstance(session, dict)
        self.assertEqual(session.get("default_inbox_address"), REVIEWER_ADDRESS)
        self.assertIn(REVIEWER_ADDRESS, list(session.get("default_claim_addresses", [])))

        sent = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "thread-default-http"},
            subject="thread default http",
            message_type="codex.thread.default.http",
        )

        initial = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=session_token,
            query={"limit": 10},
        )
        self.assertEqual(bool(summary_for_thread(initial, str(sent["thread_id"])).get("unread")), True)

        marked = request_json(
            self.base_url,
            "POST",
            "/mark-thread-read",
            token=session_token,
            body={"thread_id": sent["thread_id"]},
        )
        self.assertEqual(marked.get("ok"), True)

        refreshed = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=session_token,
            query={"limit": 10},
        )
        self.assertEqual(bool(summary_for_thread(refreshed, str(sent["thread_id"])).get("unread")), False)

    def test_cli_thread_state_routes_infer_session_default_inbox_address(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-thread-default-cli",
            session_name="fixture-defaults-cli",
        )
        session_token = auth_token_for_client(reviewer_session)

        sent = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "thread-default-cli"},
            subject="thread default cli",
            message_type="codex.thread.default.cli",
        )

        env = self.base_env()
        env["MAILBOX_SESSION_TOKEN"] = session_token
        env.pop("MAILBOX_TOKEN", None)
        env.pop("MAILBOX_INBOX_ADDRESS", None)
        env.pop("MAILBOX_FROM_ADDRESS", None)

        whoami = run_client_json(env, "whoami")
        session = whoami.get("session")
        self.assertIsInstance(session, dict)
        self.assertEqual(session.get("default_inbox_address"), REVIEWER_ADDRESS)

        payload = run_client_json(
            env,
            "thread-summaries",
            "--limit",
            "5",
        )
        self.assertEqual(bool(summary_for_thread(payload, str(sent["thread_id"])).get("unread")), True)

        marked = run_client_json(
            env,
            "mark-thread-read",
            "--thread-id",
            str(sent["thread_id"]),
        )
        self.assertEqual(marked.get("ok"), True)

        refreshed = run_client_json(
            env,
            "thread-summaries",
            "--limit",
            "5",
        )
        self.assertEqual(bool(summary_for_thread(refreshed, str(sent["thread_id"])).get("unread")), False)


if __name__ == "__main__":
    unittest.main()
