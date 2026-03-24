from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_RUNTIME_DIR = ROOT / ".tmp_dogfood_update_drill"
DEFAULT_ADMIN_TOKEN = "dogfood-admin-token"
DEFAULT_PROJECT_ID = "mail4agent"
DEFAULT_HARNESS_ID = "dogfood"
OPERATOR_ADDRESS = "operator@mail4agent.dogfood"
PLANNER_ADDRESS = "planner@mail4agent.dogfood"
REVIEWER_ADDRESS = "reviewer@mail4agent.dogfood"


def _request_json(
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
    url = f"{base_url.rstrip('/')}{path}{query_string}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=20) as response:
            status = int(response.status)
            payload_text = response.read().decode("utf-8")
    except error.HTTPError as exc:
        status = int(exc.code)
        payload_text = exc.read().decode("utf-8", errors="replace")
    payload = json.loads(payload_text)
    if status != expected_status:
        raise RuntimeError(f"{method} {url} returned {status}, expected {expected_status}: {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON: {payload!r}")
    return payload


def _run_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 900,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed


def _run_client_json(
    *args: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    completed = _run_command(
        [sys.executable, str(ROOT / "client.py"), *args],
        env=env,
        timeout_seconds=timeout_seconds,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"client.py returned non-object JSON: {payload!r}")
    return payload


def _wait_for_health(base_url: str, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        try:
            payload = _request_json(base_url, "GET", "/healthz")
            if payload.get("ok") is True:
                return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = str(exc)
            time.sleep(0.2)
    raise RuntimeError(f"mailbox server did not become healthy: {last_error}")


def _start_server(base_url: str, runtime_dir: Path, admin_token: str) -> subprocess.Popen[str]:
    stdout_path = runtime_dir / "drill_server_stdout.log"
    stderr_path = runtime_dir / "drill_server_stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["MAILBOX_ADMIN_TOKEN"] = admin_token
    env["MAILBOX_HTTP_DEBUG"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(ROOT / "sqlite_mailbox_http.py"),
            "--db",
            str(runtime_dir / "mailbox.sqlite"),
            "--host",
            "127.0.0.1",
            "--port",
            base_url.rsplit(":", 1)[1],
        ],
        cwd=str(ROOT),
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
    )
    _wait_for_health(base_url)
    return process


def _stop_server(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _bootstrap(runtime_dir: Path, base_url: str, admin_token: str) -> dict[str, Any]:
    completed = _run_command(
        [
            sys.executable,
            str(ROOT / "dogfood_smoke_bootstrap.py"),
            "--base-url",
            base_url,
            "--admin-token",
            admin_token,
            "--runtime-dir",
            str(runtime_dir),
        ],
        timeout_seconds=120,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("bootstrap returned non-object JSON")
    return payload


def _launcher(role: str, runtime_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_command(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "launch_dogfood_medium_agent.ps1"),
            role,
            "-RuntimeDir",
            str(runtime_dir),
        ],
        timeout_seconds=1800,
    )


def _load_harness_token(runtime_dir: Path) -> str:
    return (runtime_dir / "harness.token").read_text(encoding="utf-8").strip()


def _role_env(role: str, runtime_dir: Path, base_url: str, harness_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["MAILBOX_BASE_URL"] = base_url
    env["MAILBOX_CONFIG"] = str(runtime_dir / f"{role}.mailbox_client.json")
    env["MAILBOX_TOKEN"] = harness_token
    env["MAILBOX_HARNESS_ID"] = DEFAULT_HARNESS_ID
    env["MAILBOX_PROJECT_ID"] = DEFAULT_PROJECT_ID
    return env


def _session_only_env(base_url: str, session_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["MAILBOX_BASE_URL"] = base_url
    env["MAILBOX_SESSION_TOKEN"] = session_token
    env.pop("MAILBOX_TOKEN", None)
    env.pop("MAILBOX_CONFIG", None)
    env.pop("MAILBOX_FROM_ADDRESS", None)
    env.pop("MAILBOX_INBOX_ADDRESS", None)
    env.pop("MAILBOX_TIMEOUT_SECONDS", None)
    return env


def _login_role(role: str, runtime_dir: Path, base_url: str, harness_token: str) -> str:
    env = _role_env(role, runtime_dir, base_url, harness_token)
    completed = _run_command(
        [
            sys.executable,
            str(ROOT / "client.py"),
            "login",
            "--output",
            "token",
            "--project-id",
            DEFAULT_PROJECT_ID,
            "--role",
            role,
            "--session",
            "dogfood",
            "--agent-name",
            f"dogfood-{role}",
        ],
        env=env,
        timeout_seconds=120,
    )
    return completed.stdout.strip()


def _thread_payload(base_url: str, admin_token: str, thread_id: str) -> dict[str, Any]:
    return _run_client_json(
        "--base-url",
        base_url,
        "thread",
        "--admin-token",
        admin_token,
        "--thread-id",
        thread_id,
    )


def _find_last_message_id(thread_payload: dict[str, Any], from_address: str) -> str:
    thread = thread_payload.get("thread")
    if not isinstance(thread, dict):
        raise RuntimeError("thread payload missing thread")
    messages = thread.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("thread payload missing messages")
    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("from")) == from_address:
            message_id = message.get("message_id")
            if isinstance(message_id, str) and message_id:
                return message_id
    raise RuntimeError(f"could not find message from {from_address}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mailbox-native dogfood feedback/update drill")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir)
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir, ignore_errors=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    admin_token = args.admin_token
    server_process: subprocess.Popen[str] | None = None

    summary: dict[str, Any] = {
        "ok": False,
        "base_url": base_url,
        "runtime_dir": str(runtime_dir.resolve()),
        "admin_token": admin_token,
        "note": (
            "This rehearsal validates the promoted session-scoped default inbox behavior together with "
            "mailbox-native restart and re-login continuity."
        ),
    }

    try:
        server_process = _start_server(base_url, runtime_dir, admin_token)
        summary["server_started"] = True

        bootstrap = _bootstrap(runtime_dir, base_url, admin_token)
        summary["bootstrap"] = bootstrap

        kickoff = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--admin-token",
            admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            PLANNER_ADDRESS,
            "--payload-json",
            json.dumps(
                {
                    "task": "triage",
                    "request": (
                        "Summarize this inbox item, ask reviewer for a short verdict, and have reviewer report any "
                        "bounded mailbox-runtime or CLI ergonomics issue back to operator via mailbox on the same thread."
                    ),
                },
                ensure_ascii=False,
            ),
        )
        thread_id = str(kickoff["thread_id"])
        summary["kickoff"] = kickoff

        planner_run = _launcher("planner", runtime_dir)
        reviewer_run = _launcher("reviewer", runtime_dir)
        summary["planner_run"] = {"stdout": planner_run.stdout, "stderr": planner_run.stderr}
        summary["reviewer_run"] = {"stdout": reviewer_run.stdout, "stderr": reviewer_run.stderr}

        thread_after_reviewer = _thread_payload(base_url, admin_token, thread_id)
        reviewer_message_id = _find_last_message_id(thread_after_reviewer, REVIEWER_ADDRESS)
        summary["thread_after_reviewer"] = thread_after_reviewer

        harness_token = _load_harness_token(runtime_dir)
        reviewer_old_token = _login_role("reviewer", runtime_dir, base_url, harness_token)
        planner_old_token = _login_role("planner", runtime_dir, base_url, harness_token)
        summary["pre_restart_session_tokens_captured"] = True
        summary["pre_restart_default_inbox_check"] = {
            "planner_whoami": _run_client_json(
                "whoami",
                env=_session_only_env(base_url, planner_old_token),
            ),
            "planner_thread_summaries": _run_client_json(
                "thread-summaries",
                "--limit",
                "10",
                env=_session_only_env(base_url, planner_old_token),
            ),
        }

        reviewer_feedback = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--token",
            reviewer_old_token,
            "--from-address",
            REVIEWER_ADDRESS,
            "--to-address",
            OPERATOR_ADDRESS,
            "--thread-id",
            thread_id,
            "--in-reply-to-message-id",
            reviewer_message_id,
            "--payload-json",
            json.dumps(
                {
                    "kind": "dogfood_feedback",
                    "source_role": "reviewer",
                    "observed_behavior": (
                        "maintenance and restart handling are still easier to reason about when session lifecycle "
                        "state is explicit and mailbox-native"
                    ),
                    "expected_behavior": (
                        "operator should be able to notify clients, restart the server, and confirm that old session "
                        "tokens fail while harness-token re-login succeeds cleanly"
                    ),
                    "surface": "mixed",
                    "suggested_scope": "bounded runtime/server/CLI fix",
                    "severity": "medium",
                },
                ensure_ascii=False,
            ),
        )
        summary["reviewer_feedback"] = reviewer_feedback

        maintenance_ack = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--admin-token",
            admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            REVIEWER_ADDRESS,
            "--thread-id",
            thread_id,
            "--payload-json",
            json.dumps(
                {
                    "kind": "maintenance_ack",
                    "ok": True,
                    "accepted_feedback": True,
                    "planned_action": "bounded server update rehearsal",
                    "note": "Server will restart; clients must re-login after the update.",
                },
                ensure_ascii=False,
            ),
        )
        planner_notice = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--admin-token",
            admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            PLANNER_ADDRESS,
            "--thread-id",
            thread_id,
            "--payload-json",
            json.dumps(
                {
                    "kind": "maintenance_notice",
                    "message": "Server will restart for a bounded update. Current session tokens will expire. Re-run client.py login after restart.",
                },
                ensure_ascii=False,
            ),
        )
        reviewer_notice = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--admin-token",
            admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            REVIEWER_ADDRESS,
            "--thread-id",
            thread_id,
            "--payload-json",
            json.dumps(
                {
                    "kind": "maintenance_notice",
                    "message": "Server will restart for a bounded update. Current session tokens will expire. Re-run client.py login after restart.",
                },
                ensure_ascii=False,
            ),
        )
        summary["maintenance_ack"] = maintenance_ack
        summary["planner_notice"] = planner_notice
        summary["reviewer_notice"] = reviewer_notice

        backup_path = runtime_dir / "mailbox.pre_update.sqlite"
        shutil.copy2(runtime_dir / "mailbox.sqlite", backup_path)
        summary["backup_path"] = str(backup_path.resolve())

        _stop_server(server_process)
        server_process = None
        server_process = _start_server(base_url, runtime_dir, admin_token)
        summary["server_restarted"] = True

        old_reviewer_whoami = _request_json(
            base_url,
            "GET",
            "/whoami",
            token=reviewer_old_token,
            expected_status=401,
        )
        old_planner_whoami = _request_json(
            base_url,
            "GET",
            "/whoami",
            token=planner_old_token,
            expected_status=401,
        )
        summary["old_session_denials"] = {
            "reviewer": old_reviewer_whoami,
            "planner": old_planner_whoami,
        }

        reviewer_new_token = _login_role("reviewer", runtime_dir, base_url, harness_token)
        planner_new_token = _login_role("planner", runtime_dir, base_url, harness_token)
        summary["relogin"] = {
            "reviewer_token_changed": reviewer_new_token != reviewer_old_token,
            "planner_token_changed": planner_new_token != planner_old_token,
        }

        post_update_check = _run_client_json(
            "--base-url",
            base_url,
            "send",
            "--admin-token",
            admin_token,
            "--from-address",
            OPERATOR_ADDRESS,
            "--to-address",
            PLANNER_ADDRESS,
            "--thread-id",
            thread_id,
            "--payload-json",
            json.dumps(
                {
                    "kind": "post_update_check",
                    "request": "Confirm that restart recovery worked and that you can still continue this thread normally.",
                },
                ensure_ascii=False,
            ),
        )
        post_update_planner = _launcher("planner", runtime_dir)
        summary["post_update_check"] = post_update_check
        summary["post_update_planner_run"] = {
            "stdout": post_update_planner.stdout,
            "stderr": post_update_planner.stderr,
        }
        planner_post_update_env = _session_only_env(base_url, planner_new_token)
        summary["post_restart_default_inbox_check"] = {
            "planner_whoami": _run_client_json(
                "whoami",
                env=planner_post_update_env,
            ),
            "planner_thread_summaries_before_mark_read": _run_client_json(
                "thread-summaries",
                "--limit",
                "10",
                env=planner_post_update_env,
            ),
            "planner_mark_thread_read": _run_client_json(
                "mark-thread-read",
                "--thread-id",
                thread_id,
                env=planner_post_update_env,
            ),
            "planner_thread_summaries_after_mark_read": _run_client_json(
                "thread-summaries",
                "--limit",
                "10",
                env=planner_post_update_env,
            ),
        }

        final_thread = _thread_payload(base_url, admin_token, thread_id)
        planner_summaries = _run_client_json(
            "--base-url",
            base_url,
            "thread-summaries",
            "--admin-token",
            admin_token,
            "--to-address",
            PLANNER_ADDRESS,
            "--limit",
            "10",
        )
        reviewer_summaries = _run_client_json(
            "--base-url",
            base_url,
            "thread-summaries",
            "--admin-token",
            admin_token,
            "--to-address",
            REVIEWER_ADDRESS,
            "--limit",
            "10",
        )
        retry_queue = _run_client_json(
            "--base-url",
            base_url,
            "retry-queue",
            "--admin-token",
            admin_token,
            "--project-id",
            DEFAULT_PROJECT_ID,
            "--limit",
            "10",
        )
        admin_whoami = _run_client_json(
            "--base-url",
            base_url,
            "whoami",
            "--admin-token",
            admin_token,
        )
        summary["final_checks"] = {
            "thread": final_thread,
            "planner_summaries": planner_summaries,
            "reviewer_summaries": reviewer_summaries,
            "retry_queue": retry_queue,
            "admin_whoami": admin_whoami,
        }
        summary["ok"] = True
    finally:
        _stop_server(server_process)

    summary_path = runtime_dir / "feedback_update_drill_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
