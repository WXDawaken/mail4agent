from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from codex_mailbox_client import MailboxClientConfig, MailboxHTTPClient
from sqlite_mailbox import SQLiteMailbox
from sqlite_mailbox_http import MailboxHTTPServer, MailboxRequestHandler


ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TEST_RUNTIME_BASE = os.environ.get("TEMP") or os.environ.get("TMP")
TEST_RUNTIME_ROOT = Path(
    os.environ.get("MAIL4AGENT_TEST_RUNTIME_ROOT")
    or (
        (Path(_DEFAULT_TEST_RUNTIME_BASE) / "mail4agent_feature_tests")
        if _DEFAULT_TEST_RUNTIME_BASE
        else (ROOT / ".tmp_test_runtime")
    )
)
HARNESS_ID = "codex"
SHADOW_HARNESS_ID = "ops"
PROJECT_ID = "mail4agent"
ADMIN_TOKEN = "feature-bench-admin"
PLANNER_ADDRESS = "planner@mail4agent.codex"
REVIEWER_ADDRESS = "reviewer@mail4agent.codex"
OPERATOR_ADDRESS = "operator@mail4agent.codex"
SHADOW_ADDRESS = "shadow@mail4agent.ops"


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_healthz(base_url: str, timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with request.urlopen(f"{base_url}/healthz", timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") is True:
                return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"mailbox server did not become healthy: {last_error}")


def seed_mailbox(db_path: Path) -> dict[str, str]:
    mailbox = SQLiteMailbox(str(db_path))
    mailbox.init_db()
    mailbox.upsert_harness(HARNESS_ID, display_name="Codex Bench")
    mailbox.upsert_project(PROJECT_ID, HARNESS_ID, display_name="Mail4Agent Bench")
    mailbox.upsert_mailbox(PLANNER_ADDRESS, mailbox_type="role")
    mailbox.upsert_mailbox(REVIEWER_ADDRESS, mailbox_type="role")
    mailbox.upsert_mailbox(OPERATOR_ADDRESS, mailbox_type="role")
    mailbox.allow_same_project(PROJECT_ID, HARNESS_ID)

    mailbox.upsert_harness(SHADOW_HARNESS_ID, display_name="Ops Bench")
    mailbox.upsert_project(PROJECT_ID, SHADOW_HARNESS_ID, display_name="Ops Bench")
    mailbox.upsert_mailbox(SHADOW_ADDRESS, mailbox_type="role")
    mailbox.allow_same_project(PROJECT_ID, SHADOW_HARNESS_ID)

    codex_token = mailbox.create_harness_token(HARNESS_ID, token_name="feature-bench-codex")
    ops_token = mailbox.create_harness_token(SHADOW_HARNESS_ID, token_name="feature-bench-ops")
    return {"codex": str(codex_token["token"]), "ops": str(ops_token["token"])}


def start_mailbox_server(db_path: Path) -> tuple[MailboxHTTPServer, threading.Thread, str]:
    port = pick_free_port()
    server = MailboxHTTPServer(("127.0.0.1", port), MailboxRequestHandler, str(db_path))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"
    wait_for_healthz(base_url)
    return server, server_thread, base_url


def stop_mailbox_server(server: MailboxHTTPServer, server_thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5.0)


def make_harness_client(
    base_url: str,
    token: str,
    *,
    from_address: str,
    inbox_address: str,
    consumer_id: str,
) -> MailboxHTTPClient:
    return MailboxHTTPClient(
        MailboxClientConfig(
            base_url=base_url,
            token=token,
            from_address=from_address,
            inbox_address=inbox_address,
            consumer_id=consumer_id,
            timeout_seconds=5.0,
        )
    )


def login_role_session(
    base_url: str,
    harness_token: str,
    *,
    role: str,
    consumer_id: str,
    session_name: str = "main",
    expires_in_seconds: int | None = None,
) -> MailboxHTTPClient:
    address = f"{role}@{PROJECT_ID}.{HARNESS_ID}"
    client = make_harness_client(
        base_url,
        harness_token,
        from_address=address,
        inbox_address=address,
        consumer_id=consumer_id,
    )
    login_payload = client.login(
        project_id=PROJECT_ID,
        role=role,
        session=session_name,
        expires_in_seconds=expires_in_seconds,
    )
    session_payload = login_payload.get("session")
    if not isinstance(session_payload, dict):
        raise AssertionError("login did not produce a session payload")
    return client


def auth_token_for_client(client: MailboxHTTPClient) -> str:
    token = client._session_token or client.config.token  # noqa: SLF001 - local test helper
    if not token:
        raise AssertionError("client does not have an active auth token")
    return token


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    query_string = ""
    if query:
        query_pairs = [(key, "" if value is None else str(value)) for key, value in query.items()]
        query_string = "?" + parse.urlencode(query_pairs)
    url = f"{base_url}{path}{query_string}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=5.0) as response:
            status = int(response.status)
            payload_text = response.read().decode("utf-8")
    except error.HTTPError as exc:
        status = int(exc.code)
        payload_text = exc.read().decode("utf-8")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(f"{method} {url} did not return valid JSON:\n{payload_text}") from exc
    if status != expected_status:
        raise AssertionError(f"{method} {url} returned {status}, expected {expected_status}: {payload}")
    if not isinstance(payload, dict):
        raise AssertionError(f"{method} {url} returned non-dict JSON: {payload!r}")
    return payload


def run_client_json(
    env: dict[str, str],
    *args: str,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "client.py"), *args],
        cwd=ROOT,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"client.py exited with {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "client.py did not emit valid JSON\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"client.py returned non-dict JSON: {payload!r}")
    return payload


def message_ids_from_items(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["message_id"]) for item in items]


class MailboxHTTPFeatureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = self._create_runtime_dir()
        self.db_path = self.runtime_dir / "mailbox.sqlite"
        self.tokens = seed_mailbox(self.db_path)
        self.mailbox = SQLiteMailbox(str(self.db_path))
        self._previous_admin_token = os.environ.get("MAILBOX_ADMIN_TOKEN")
        os.environ["MAILBOX_ADMIN_TOKEN"] = ADMIN_TOKEN
        self.admin_token = ADMIN_TOKEN
        self.server, self.server_thread, self.base_url = start_mailbox_server(self.db_path)
        self.codex_client = make_harness_client(
            self.base_url,
            self.tokens["codex"],
            from_address=OPERATOR_ADDRESS,
            inbox_address=OPERATOR_ADDRESS,
            consumer_id="python-codex-harness",
        )
        self.reviewer_harness_client = make_harness_client(
            self.base_url,
            self.tokens["codex"],
            from_address=REVIEWER_ADDRESS,
            inbox_address=REVIEWER_ADDRESS,
            consumer_id="python-reviewer-harness",
        )
        self.operator_harness_client = make_harness_client(
            self.base_url,
            self.tokens["codex"],
            from_address=OPERATOR_ADDRESS,
            inbox_address=OPERATOR_ADDRESS,
            consumer_id="python-operator-harness",
        )

    def tearDown(self) -> None:
        stop_mailbox_server(self.server, self.server_thread)
        if self._previous_admin_token is None:
            os.environ.pop("MAILBOX_ADMIN_TOKEN", None)
        else:
            os.environ["MAILBOX_ADMIN_TOKEN"] = self._previous_admin_token
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        shutil.rmtree(TEST_RUNTIME_ROOT, ignore_errors=True)

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["MAILBOX_BASE_URL"] = self.base_url
        return env

    def session_env(self, client: MailboxHTTPClient, *, from_address: str, inbox_address: str) -> dict[str, str]:
        env = self.base_env()
        env.update(
            {
                "MAILBOX_SESSION_TOKEN": auth_token_for_client(client),
                "MAILBOX_FROM_ADDRESS": from_address,
                "MAILBOX_INBOX_ADDRESS": inbox_address,
            }
        )
        env.pop("MAILBOX_TOKEN", None)
        return env

    def admin_env(self) -> dict[str, str]:
        env = self.base_env()
        env["MAILBOX_ADMIN_TOKEN"] = self.admin_token
        env.pop("MAILBOX_TOKEN", None)
        env.pop("MAILBOX_SESSION_TOKEN", None)
        return env

    def pause_for_ordering(self) -> None:
        time.sleep(0.02)

    def _create_runtime_dir(self) -> Path:
        for _ in range(32):
            candidate = TEST_RUNTIME_ROOT / f"case-{os.getpid()}-{time.time_ns()}"
            try:
                candidate.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                continue
            return candidate
        raise AssertionError("failed to create unique test runtime directory")
