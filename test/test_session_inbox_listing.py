from __future__ import annotations

import unittest

from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    REVIEWER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    auth_token_for_client,
    login_role_session,
    message_ids_from_items,
    request_json,
    run_client_json,
)


def inbox_items(payload: dict[str, object]) -> list[dict[str, object]]:
    items = payload.get("messages")
    if not isinstance(items, list):
        raise AssertionError(f"expected messages list, got: {payload!r}")
    return [item for item in items if isinstance(item, dict)]


class SessionInboxListingTests(MailboxHTTPFeatureTestCase):
    def test_http_inbox_route_uses_session_default_inbox_and_message_type_filter(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-inbox-http",
            session_name="dogfood-inbox-http",
        )
        session_token = auth_token_for_client(reviewer_session)

        first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http", "step": 1},
            subject="inbox http first",
            message_type="codex.inbox.other",
        )
        self.pause_for_ordering()
        second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http", "step": 2},
            subject="inbox http second",
            message_type="codex.inbox.keep",
        )
        self.pause_for_ordering()
        third = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http", "step": 3},
            subject="inbox http third",
            message_type="codex.inbox.keep",
        )

        payload = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 2},
        )
        visible = inbox_items(payload)
        self.assertEqual(message_ids_from_items(visible), [str(third["message_id"]), str(second["message_id"])])

        filtered = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "message_type": "codex.inbox.keep"},
        )
        filtered_items = inbox_items(filtered)
        self.assertEqual(message_ids_from_items(filtered_items), [str(third["message_id"]), str(second["message_id"])])
        self.assertEqual(filtered_items[0].get("from"), OPERATOR_ADDRESS)
        self.assertEqual(filtered_items[0].get("to"), [REVIEWER_ADDRESS])
        self.assertEqual(filtered_items[0].get("subject"), "inbox http third")
        self.assertEqual(filtered_items[0].get("message_type"), "codex.inbox.keep")
        self.assertEqual(filtered_items[0].get("payload"), {"task": "inbox-http", "step": 3})
        self.assertNotIn(str(first["message_id"]), message_ids_from_items(filtered_items))

    def test_http_inbox_route_filters_by_since_and_rejects_invalid_timestamp(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-inbox-http-since",
            session_name="dogfood-inbox-http-since",
        )
        session_token = auth_token_for_client(reviewer_session)

        older = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-since", "step": 1},
            subject="inbox http since older",
            message_type="codex.inbox.since",
        )
        self.pause_for_ordering()
        newer = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-since", "step": 2},
            subject="inbox http since newer",
            message_type="codex.inbox.since",
        )

        baseline = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "message_type": "codex.inbox.since"},
        )
        baseline_items = inbox_items(baseline)
        since = str(
            next(
                item["created_at"]
                for item in baseline_items
                if item.get("message_id") == str(newer["message_id"])
            )
        )
        filtered = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "since": since},
        )
        filtered_items = inbox_items(filtered)
        self.assertEqual(message_ids_from_items(filtered_items), [str(newer["message_id"])])
        self.assertNotIn(str(older["message_id"]), message_ids_from_items(filtered_items))

        invalid = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"since": "not-a-timestamp"},
            expected_status=400,
        )
        self.assertEqual(invalid.get("ok"), False)
        self.assertIn("Invalid isoformat string", str(invalid.get("error")))

    def test_http_inbox_route_can_filter_unread_only_before_and_after_mark_read(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-inbox-http-unread",
            session_name="dogfood-inbox-http-unread",
        )
        session_token = auth_token_for_client(reviewer_session)

        first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-unread", "step": 1},
            subject="inbox http unread first",
            message_type="codex.inbox.unread",
        )
        self.pause_for_ordering()
        second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-unread", "step": 2},
            subject="inbox http unread second",
            message_type="codex.inbox.unread",
            thread_id=str(first["thread_id"]),
            in_reply_to_message_id=str(first["message_id"]),
        )

        unread = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "unread_only": "true"},
        )
        unread_items = inbox_items(unread)
        self.assertEqual(message_ids_from_items(unread_items), [str(second["message_id"]), str(first["message_id"])])

        marked = request_json(
            self.base_url,
            "POST",
            "/mark-thread-read",
            token=session_token,
            body={"thread_id": str(first["thread_id"])},
        )
        self.assertEqual(marked.get("ok"), True)

        after_mark = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "unread_only": "1"},
        )
        self.assertEqual(inbox_items(after_mark), [])

        reopened = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-unread", "step": 3},
            subject="inbox http unread third",
            message_type="codex.inbox.unread",
            thread_id=str(first["thread_id"]),
            in_reply_to_message_id=str(second["message_id"]),
        )

        after_reopen = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "unread_only": "yes"},
        )
        reopened_items = inbox_items(after_reopen)
        self.assertEqual(message_ids_from_items(reopened_items), [str(reopened["message_id"])])

    def test_http_inbox_route_can_filter_by_thread_id(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-inbox-http-thread-id",
            session_name="dogfood-inbox-http-thread-id",
        )
        session_token = auth_token_for_client(reviewer_session)

        thread_a_first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-thread-id", "step": 1},
            subject="inbox http thread a first",
            message_type="codex.inbox.thread-id",
        )
        self.pause_for_ordering()
        thread_b = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-thread-id", "step": 2},
            subject="inbox http thread b only",
            message_type="codex.inbox.thread-id",
        )
        self.pause_for_ordering()
        thread_a_second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-http-thread-id", "step": 3},
            subject="inbox http thread a second",
            message_type="codex.inbox.thread-id",
            thread_id=str(thread_a_first["thread_id"]),
            in_reply_to_message_id=str(thread_a_first["message_id"]),
        )

        filtered = request_json(
            self.base_url,
            "GET",
            "/inbox",
            token=session_token,
            query={"limit": 10, "thread_id": str(thread_a_first["thread_id"])},
        )
        filtered_items = inbox_items(filtered)
        self.assertEqual(
            message_ids_from_items(filtered_items),
            [str(thread_a_second["message_id"]), str(thread_a_first["message_id"])],
        )
        self.assertNotIn(str(thread_b["message_id"]), message_ids_from_items(filtered_items))

    def test_cli_inbox_command_infers_session_default_inbox(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-inbox-cli",
            session_name="dogfood-inbox-cli",
        )
        session_token = auth_token_for_client(reviewer_session)

        older = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-cli", "step": 1},
            subject="inbox cli older",
            message_type="codex.inbox.cli.keep",
        )
        self.pause_for_ordering()
        ignored = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-cli", "step": 2},
            subject="inbox cli ignored",
            message_type="codex.inbox.cli.skip",
        )
        self.pause_for_ordering()
        newer = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "inbox-cli", "step": 3},
            subject="inbox cli newer",
            message_type="codex.inbox.cli.keep",
            thread_id=str(older["thread_id"]),
            in_reply_to_message_id=str(older["message_id"]),
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

        baseline = run_client_json(
            env,
            "inbox",
            "--limit",
            "10",
            "--message-type",
            "codex.inbox.cli.keep",
        )
        baseline_items = inbox_items(baseline)
        self.assertEqual(message_ids_from_items(baseline_items), [str(newer["message_id"]), str(older["message_id"])])
        since = str(
            next(
                item["created_at"]
                for item in baseline_items
                if item.get("message_id") == str(newer["message_id"])
            )
        )

        payload = run_client_json(
            env,
            "inbox",
            "--limit",
            "10",
            "--message-type",
            "codex.inbox.cli.keep",
            "--since",
            since,
        )
        visible = inbox_items(payload)
        self.assertEqual(message_ids_from_items(visible), [str(newer["message_id"])])
        self.assertNotIn(str(older["message_id"]), message_ids_from_items(visible))
        self.assertNotIn(str(ignored["message_id"]), message_ids_from_items(visible))

        thread_filtered = run_client_json(
            env,
            "inbox",
            "--limit",
            "10",
            "--message-type",
            "codex.inbox.cli.keep",
            "--thread-id",
            str(older["thread_id"]),
        )
        thread_filtered_items = inbox_items(thread_filtered)
        self.assertEqual(
            message_ids_from_items(thread_filtered_items),
            [str(newer["message_id"]), str(older["message_id"])],
        )
        self.assertNotIn(str(ignored["message_id"]), message_ids_from_items(thread_filtered_items))

        unread = run_client_json(
            env,
            "inbox",
            "--limit",
            "10",
            "--message-type",
            "codex.inbox.cli.keep",
            "--unread-only",
        )
        unread_items = inbox_items(unread)
        self.assertEqual(message_ids_from_items(unread_items), [str(newer["message_id"]), str(older["message_id"])])

        marked = run_client_json(
            env,
            "mark-thread-read",
            "--thread-id",
            str(newer["thread_id"]),
        )
        self.assertEqual(marked.get("ok"), True)

        after_mark = run_client_json(
            env,
            "inbox",
            "--limit",
            "10",
            "--message-type",
            "codex.inbox.cli.keep",
            "--thread-id",
            str(newer["thread_id"]),
            "--unread-only",
        )
        self.assertEqual(inbox_items(after_mark), [])


if __name__ == "__main__":
    unittest.main()
