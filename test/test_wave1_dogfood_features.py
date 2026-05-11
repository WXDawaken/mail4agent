from __future__ import annotations

from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    PROJECT_ID,
    REVIEWER_ADDRESS,
    SHADOW_ADDRESS,
    MailboxHTTPFeatureTestCase,
    auth_token_for_client,
    login_role_session,
    make_harness_client,
    message_ids_from_items,
    request_json,
    run_client_json,
)


def thread_messages(payload: dict[str, object]) -> list[dict[str, object]]:
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        raise AssertionError(f"expected thread dict, got: {payload!r}")
    messages = thread.get("messages")
    if not isinstance(messages, list):
        raise AssertionError(f"expected thread messages list, got: {thread!r}")
    return [item for item in messages if isinstance(item, dict)]


def retry_items(payload: dict[str, object]) -> list[dict[str, object]]:
    items = payload.get("deliveries")
    if not isinstance(items, list):
        raise AssertionError(f"expected deliveries list, got: {payload!r}")
    return [item for item in items if isinstance(item, dict)]


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


class Wave1DogfoodFeatureTests(MailboxHTTPFeatureTestCase):
    def test_http_admin_token_can_run_core_mailbox_flow_across_harnesses(self) -> None:
        denied = request_json(
            self.base_url,
            "POST",
            "/resolve",
            token=self.tokens["codex"],
            body={"address": SHADOW_ADDRESS},
            expected_status=403,
        )
        self.assertEqual(denied.get("ok"), False)

        whoami = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=self.admin_token,
        )
        self.assertEqual(whoami.get("ok"), True)
        self.assertEqual(whoami.get("auth_kind"), "admin")
        self.assertEqual(whoami.get("admin"), True)

        mailbox = request_json(
            self.base_url,
            "POST",
            "/resolve",
            token=self.admin_token,
            body={"address": SHADOW_ADDRESS},
        )
        resolved = mailbox.get("mailbox")
        self.assertIsInstance(resolved, dict)
        self.assertEqual(resolved.get("address"), SHADOW_ADDRESS)
        self.assertEqual(resolved.get("harness_id"), "ops")

        sent = request_json(
            self.base_url,
            "POST",
            "/send",
            token=self.admin_token,
            body={
                "from_address": OPERATOR_ADDRESS,
                "to_address": SHADOW_ADDRESS,
                "subject": "admin http",
                "message_type": "codex.admin.http",
                "payload": {"task": "admin-http"},
            },
        )
        self.assertEqual(sent.get("ok"), True)
        self.assertIsInstance(sent.get("thread_id"), str)

        thread_payload = request_json(
            self.base_url,
            "GET",
            "/thread",
            token=self.admin_token,
            query={"message_id": str(sent["message_id"])},
        )
        message_ids = [str(item.get("message_id")) for item in thread_messages(thread_payload)]
        self.assertIn(str(sent["message_id"]), message_ids)

        claimed = request_json(
            self.base_url,
            "POST",
            "/claim",
            token=self.admin_token,
            body={
                "to_address": SHADOW_ADDRESS,
                "consumer_id": "python-admin-http-claim",
                "lease_seconds": 30,
            },
        )
        delivery = claimed.get("delivery")
        self.assertIsInstance(delivery, dict)
        self.assertEqual(str(delivery.get("message_id")), str(sent["message_id"]))
        self.assertEqual(str(delivery.get("to")), SHADOW_ADDRESS)

        heartbeat = request_json(
            self.base_url,
            "POST",
            "/heartbeat",
            token=self.admin_token,
            body={
                "delivery_id": int(delivery["delivery_id"]),
                "claim_token": str(delivery["claim_token"]),
                "lease_seconds": 45,
            },
        )
        self.assertEqual(heartbeat.get("ok"), True)

        nacked = request_json(
            self.base_url,
            "POST",
            "/nack",
            token=self.admin_token,
            body={
                "delivery_id": int(delivery["delivery_id"]),
                "claim_token": str(delivery["claim_token"]),
                "retry_after_seconds": 0,
                "last_error": "retry for ack path",
                "actor": "python-admin-http",
            },
        )
        self.assertEqual(nacked.get("ok"), True)

        reclaimed = request_json(
            self.base_url,
            "POST",
            "/claim",
            token=self.admin_token,
            body={
                "to_address": SHADOW_ADDRESS,
                "consumer_id": "python-admin-http-claim-2",
                "lease_seconds": 30,
            },
        )
        delivery = reclaimed.get("delivery")
        self.assertIsInstance(delivery, dict)
        self.assertEqual(str(delivery.get("message_id")), str(sent["message_id"]))

        acked = request_json(
            self.base_url,
            "POST",
            "/ack",
            token=self.admin_token,
            body={
                "delivery_id": int(delivery["delivery_id"]),
                "claim_token": str(delivery["claim_token"]),
                "actor": "python-admin-http",
            },
        )
        self.assertEqual(acked.get("ok"), True)

    def test_cli_admin_token_runs_existing_mailbox_commands_without_session(self) -> None:
        whoami = run_client_json(
            self.admin_env(),
            "whoami",
        )
        self.assertEqual(whoami.get("ok"), True)
        self.assertEqual(whoami.get("auth_kind"), "admin")
        self.assertEqual(whoami.get("admin"), True)

        resolved = run_client_json(
            self.admin_env(),
            "resolve",
            "--address",
            SHADOW_ADDRESS,
        )
        mailbox = resolved.get("mailbox")
        self.assertIsInstance(mailbox, dict)
        self.assertEqual(mailbox.get("address"), SHADOW_ADDRESS)

        sent = run_client_json(
            self.base_env(),
            "send",
            "--admin-token",
            self.admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            SHADOW_ADDRESS,
            "--subject",
            "admin cli",
            "--message-type",
            "codex.admin.cli",
            "--payload-json",
            "{\"task\": \"admin-cli\"}",
        )
        self.assertEqual(sent.get("ok"), True)
        self.assertIsInstance(sent.get("thread_id"), str)

        claimed = run_client_json(
            self.base_env(),
            "claim",
            "--admin-token",
            self.admin_token,
            "--to-address",
            SHADOW_ADDRESS,
            "--consumer-id",
            "python-admin-cli-claim",
            "--lease-seconds",
            "30",
        )
        delivery = claimed.get("delivery")
        self.assertIsInstance(delivery, dict)
        self.assertEqual(str(delivery.get("message_id")), str(sent["message_id"]))

        acked = run_client_json(
            self.base_env(),
            "ack",
            "--admin-token",
            self.admin_token,
            "--delivery-id",
            str(delivery["delivery_id"]),
            "--claim-token",
            str(delivery["claim_token"]),
            "--actor",
            "python-admin-cli",
        )
        self.assertEqual(acked.get("ok"), True)

    def test_http_retry_queue_lists_retry_pending_deliveries_and_hides_other_harnesses(self) -> None:
        sent = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "retry-queue-http"},
            subject="retry queue http",
            message_type="codex.retry.http",
        )
        claimed = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-retry-http",
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        long_error = "retry bridge failed: " + ("x" * 160)
        ok = self.reviewer_harness_client.nack(
            delivery_id=int(claimed["delivery_id"]),
            claim_token=str(claimed["claim_token"]),
            retry_after_seconds=45,
            last_error=long_error,
            actor="python-retry-http",
        )
        self.assertEqual(ok, True)
        shadow_client = make_harness_client(
            self.base_url,
            self.tokens["ops"],
            from_address=SHADOW_ADDRESS,
            inbox_address=SHADOW_ADDRESS,
            consumer_id="python-shadow-harness",
        )
        shadow_sent = shadow_client.send(
            to_address=SHADOW_ADDRESS,
            payload={"task": "retry-queue-shadow"},
            subject="retry queue shadow",
            message_type="codex.retry.shadow",
        )
        shadow_claimed = shadow_client.claim(
            to_address=SHADOW_ADDRESS,
            consumer_id="python-retry-shadow",
            lease_seconds=30,
        )
        self.assertIsNotNone(shadow_claimed)
        self.assertTrue(
            shadow_client.nack(
                delivery_id=int(shadow_claimed["delivery_id"]),
                claim_token=str(shadow_claimed["claim_token"]),
                retry_after_seconds=60,
                last_error="shadow harness retry failure",
                actor="python-retry-shadow",
            )
        )

        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-retry-session",
        )
        visible = request_json(
            self.base_url,
            "GET",
            "/retry-queue",
            token=auth_token_for_client(reviewer_session),
            query={"to_address": REVIEWER_ADDRESS, "limit": 10},
        )
        matching = [
            item
            for item in retry_items(visible)
            if str(item.get("message_id")) == str(sent["message_id"])
        ]
        self.assertEqual(len(matching), 1)
        delivery = matching[0]
        self.assertEqual(int(delivery.get("delivery_id", 0)), int(claimed["delivery_id"]))
        self.assertEqual(str(delivery.get("to")), REVIEWER_ADDRESS)
        self.assertEqual(int(delivery.get("attempt_count", 0)), 1)
        self.assertEqual(int(delivery.get("max_attempts", 0)), 8)
        self.assertEqual(str(delivery.get("status")), "queued")
        self.assertIsInstance(delivery.get("next_retry_at"), str)
        summary = str(delivery.get("last_error_summary", ""))
        self.assertIn("retry bridge failed", summary)
        self.assertLessEqual(len(summary), 120)

        project_visible = request_json(
            self.base_url,
            "GET",
            "/retry-queue",
            token=auth_token_for_client(reviewer_session),
            query={"project_id": PROJECT_ID, "limit": 10},
        )
        project_items = retry_items(project_visible)
        project_message_ids = message_ids_from_items(project_items)
        self.assertIn(str(sent["message_id"]), project_message_ids)
        self.assertNotIn(str(shadow_sent["message_id"]), project_message_ids)
        self.assertTrue(all(str(item.get("to")) == REVIEWER_ADDRESS for item in project_items))

    def test_cli_retry_queue_returns_machine_readable_entries(self) -> None:
        sent = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "retry-queue-cli"},
            subject="retry queue cli",
            message_type="codex.retry.cli",
        )
        claimed = self.reviewer_harness_client.claim(
            to_address=REVIEWER_ADDRESS,
            consumer_id="python-retry-cli",
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        self.assertTrue(
            self.reviewer_harness_client.nack(
                delivery_id=int(claimed["delivery_id"]),
                claim_token=str(claimed["claim_token"]),
                retry_after_seconds=30,
                last_error="cli retry visibility failure",
                actor="python-retry-cli",
            )
        )

        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-retry-cli-session",
        )
        payload = run_client_json(
            self.session_env(
                reviewer_session,
                from_address=REVIEWER_ADDRESS,
                inbox_address=REVIEWER_ADDRESS,
            ),
            "retry-queue",
            "--project-id",
            PROJECT_ID,
            "--to-address",
            REVIEWER_ADDRESS,
            "--limit",
            "5",
        )
        self.assertEqual(payload.get("ok"), True)
        matching = [
            item
            for item in retry_items(payload)
            if str(item.get("message_id")) == str(sent["message_id"])
        ]
        self.assertEqual(len(matching), 1)
        delivery = matching[0]
        self.assertEqual(str(delivery.get("to")), REVIEWER_ADDRESS)
        self.assertEqual(str(delivery.get("status")), "queued")
        self.assertEqual(str(delivery.get("last_error_summary")), "cli retry visibility failure")

    def test_http_thread_summaries_track_unread_and_mark_read(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-thread-summary-http",
        )
        session_token = auth_token_for_client(reviewer_session)

        first = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "thread-summary-http", "step": 1},
            subject="thread summary http",
            message_type="codex.thread.summary",
        )

        initial = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=session_token,
            query={"to_address": REVIEWER_ADDRESS, "limit": 10},
        )
        first_summary = summary_for_thread(initial, str(first["thread_id"]))
        self.assertEqual(first_summary.get("latest_message_id"), first["message_id"])
        self.assertEqual(first_summary.get("latest_from_address"), OPERATOR_ADDRESS)
        self.assertEqual(int(first_summary.get("message_count", 0)), 1)
        self.assertEqual(int(first_summary.get("reply_count", 0)), 0)
        self.assertEqual(bool(first_summary.get("unread")), True)

        marked = request_json(
            self.base_url,
            "POST",
            "/mark-thread-read",
            token=session_token,
            body={"thread_id": first["thread_id"], "to_address": REVIEWER_ADDRESS},
        )
        self.assertEqual(marked.get("ok"), True)

        after_mark = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=session_token,
            query={"to_address": REVIEWER_ADDRESS, "limit": 10},
        )
        self.assertEqual(bool(summary_for_thread(after_mark, str(first["thread_id"])).get("unread")), False)

        self.pause_for_ordering()
        second = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "thread-summary-http", "step": 2},
            subject="thread summary http reply",
            message_type="codex.thread.reply",
            thread_id=str(first["thread_id"]),
            in_reply_to_message_id=str(first["message_id"]),
        )

        reopened = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=session_token,
            query={"to_address": REVIEWER_ADDRESS, "limit": 10},
        )
        reopened_summary = summary_for_thread(reopened, str(first["thread_id"]))
        self.assertEqual(reopened_summary.get("latest_message_id"), second["message_id"])
        self.assertEqual(reopened_summary.get("latest_from_address"), OPERATOR_ADDRESS)
        self.assertEqual(int(reopened_summary.get("message_count", 0)), 2)
        self.assertEqual(int(reopened_summary.get("reply_count", 0)), 1)
        self.assertEqual(bool(reopened_summary.get("unread")), True)

        hidden = request_json(
            self.base_url,
            "GET",
            "/thread-summaries",
            token=self.tokens["ops"],
            query={"to_address": REVIEWER_ADDRESS, "limit": 10},
        )
        self.assertEqual(thread_items(hidden), [])

    def test_cli_thread_summaries_and_mark_read_are_scriptable(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-thread-summary-cli",
        )
        sent = self.operator_harness_client.send(
            to_address=REVIEWER_ADDRESS,
            payload={"task": "thread-summary-cli"},
            subject="thread summary cli",
            message_type="codex.thread.cli",
        )

        payload = run_client_json(
            self.session_env(
                reviewer_session,
                from_address=REVIEWER_ADDRESS,
                inbox_address=REVIEWER_ADDRESS,
            ),
            "thread-summaries",
            "--to-address",
            REVIEWER_ADDRESS,
            "--limit",
            "5",
        )
        self.assertEqual(bool(summary_for_thread(payload, str(sent["thread_id"])).get("unread")), True)

        marked = run_client_json(
            self.session_env(
                reviewer_session,
                from_address=REVIEWER_ADDRESS,
                inbox_address=REVIEWER_ADDRESS,
            ),
            "mark-thread-read",
            "--thread-id",
            str(sent["thread_id"]),
            "--to-address",
            REVIEWER_ADDRESS,
        )
        self.assertEqual(marked.get("ok"), True)

        refreshed = run_client_json(
            self.session_env(
                reviewer_session,
                from_address=REVIEWER_ADDRESS,
                inbox_address=REVIEWER_ADDRESS,
            ),
            "thread-summaries",
            "--to-address",
            REVIEWER_ADDRESS,
            "--limit",
            "5",
        )
        self.assertEqual(bool(summary_for_thread(refreshed, str(sent["thread_id"])).get("unread")), False)


if __name__ == "__main__":
    import unittest

    unittest.main()
