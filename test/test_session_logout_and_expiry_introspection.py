from __future__ import annotations

import unittest

from test.mail4agent_test_support import (
    REVIEWER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    auth_token_for_client,
    login_role_session,
    request_json,
    run_client_json,
)


class SessionLogoutAndExpiryIntrospectionTests(MailboxHTTPFeatureTestCase):
    def test_http_logout_revokes_session_and_whoami_exposes_expiry_metadata(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-reviewer-session-expiry",
            expires_in_seconds=120,
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
        self.assertEqual(session.get("session_name"), "main")
        self.assertIsInstance(session.get("created_at"), str)
        self.assertIsInstance(session.get("expires_at"), str)
        self.assertGreater(int(session.get("expires_in_seconds", 0)), 0)
        self.assertLessEqual(int(session.get("expires_in_seconds", 0)), 120)

        logout = request_json(
            self.base_url,
            "POST",
            "/logout",
            token=session_token,
        )
        self.assertEqual(logout.get("ok"), True)
        self.assertEqual(logout.get("logged_out"), True)

        denied = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=session_token,
            expected_status=401,
        )
        self.assertEqual(denied.get("ok"), False)
        self.assertTrue(str(denied.get("error", "")).strip())

        harness_whoami = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=self.tokens["codex"],
        )
        self.assertEqual(harness_whoami.get("ok"), True)
        self.assertIn("mailboxes", harness_whoami)

    def test_cli_logout_supports_clean_relogin(self) -> None:
        reviewer_session = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-reviewer-session-cli",
            session_name="cli-logout",
            expires_in_seconds=300,
        )
        original_token = auth_token_for_client(reviewer_session)

        logout = run_client_json(
            self.session_env(
                reviewer_session,
                from_address=REVIEWER_ADDRESS,
                inbox_address=REVIEWER_ADDRESS,
            ),
            "logout",
        )
        self.assertEqual(logout.get("ok"), True)
        self.assertEqual(logout.get("logged_out"), True)

        denied = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=original_token,
            expected_status=401,
        )
        self.assertEqual(denied.get("ok"), False)

        relogin = login_role_session(
            self.base_url,
            self.tokens["codex"],
            role="reviewer",
            consumer_id="python-reviewer-session-cli-relogin",
            session_name="cli-logout",
        )
        refreshed_token = auth_token_for_client(relogin)
        self.assertNotEqual(refreshed_token, original_token)
        whoami = request_json(
            self.base_url,
            "GET",
            "/whoami",
            token=refreshed_token,
        )
        self.assertEqual(whoami.get("ok"), True)


if __name__ == "__main__":
    unittest.main()
