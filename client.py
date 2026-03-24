from __future__ import annotations

"""Mailbox stdio CLI.

Recommended usage:

    $env:MAILBOX_BASE_URL = "http://127.0.0.1:8787"
    $env:MAILBOX_SESSION_TOKEN = (python client.py login --output token --project-id mail4agent --roles planner,reviewer --session main)
    Get-Content .\\payload.json | python client.py send --to-address reviewer@mail4agent.codex
    python client.py claim
    python client.py --format text thread --message-id <MESSAGE_ID>
    python client.py claim | python client.py --token-file .\\session.token reply --payload-json "{\"ok\":true}" --ack-after
    python client.py consume -- python -c "import json,sys; d=json.load(sys.stdin); print(d['message_id'])"

Compatibility mode:

    python client.py login --output token --project-id mail4agent --roles planner --session main > session.token
    Get-Content .\\session.token | python client.py --base-url http://127.0.0.1:8787 thread --message-id <MESSAGE_ID>

Auth input precedence for non-login commands:
    1. `--admin-token`
    2. `--token`
    3. `--token-file`
    4. `MAILBOX_SESSION_TOKEN`
    5. `MAILBOX_TOKEN`
    6. `MAILBOX_ADMIN_TOKEN`
    7. piped stdin (JSON bundle or plain token)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

from codex_mailbox_client import DEFAULT_MAILBOX_BASE_URL, MailboxClientConfig, MailboxHTTPClient, MailboxHTTPError
from mailbox_worker import ConsumeConfig, run_consume_loop, run_subprocess_handler


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "login":
            payload = _run_login(args)
            if args.output == "token":
                print(payload["token"])
            else:
                _emit_payload(payload, args)
            return

        client = _build_authenticated_client(args)
        payload = _run_client_command(client, args)
        if args.command == "consume":
            _emit_consume_summary(payload, args)
        else:
            _emit_payload(payload, args)
    except MailboxHTTPError as exc:
        _emit_error(
            {
                "ok": False,
                "error": str(exc),
                "status": exc.status,
                "payload": exc.payload or None,
            },
            args=args,
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        _emit_error({"ok": False, "error": str(exc)}, args=args)
        raise SystemExit(1) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mailbox stdio CLI")
    parser.add_argument("--base-url", dest="global_base_url", help="mailbox server base URL")
    parser.add_argument("--admin-token", dest="global_admin_token", help="admin bearer token for direct mailbox access")
    parser.add_argument("--token", dest="global_token", help="auth token (session token for normal commands)")
    parser.add_argument("--token-file", dest="global_token_file", help="read auth token or login bundle from file")
    parser.add_argument("--timeout-seconds", dest="global_timeout_seconds", type=float, default=None, help="HTTP timeout")
    parser.add_argument(
        "--format",
        dest="global_format",
        choices=("json", "text"),
        default=None,
        help="output format; default is json",
    )
    parser.add_argument("--pretty", dest="global_pretty", action="store_true", help="pretty-print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", help="mailbox server base URL")
    common.add_argument("--admin-token", help="admin bearer token for direct mailbox access")
    common.add_argument("--token", help="auth token (session token for normal commands)")
    common.add_argument("--token-file", help="read auth token or login bundle from file")
    common.add_argument("--timeout-seconds", type=float, default=None, help="HTTP timeout")
    common.add_argument("--format", choices=("json", "text"), default=None, help="output format; default is json")
    common.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    login_parser = subparsers.add_parser("login", help="exchange a harness token for an agent session token")
    login_parser.add_argument("--base-url", help="mailbox server base URL")
    login_parser.add_argument("--harness-token", help="harness token; falls back to MAILBOX_TOKEN")
    login_parser.add_argument("--harness-token-file", help="read harness token from a file")
    login_parser.add_argument("--timeout-seconds", type=float, default=None, help="HTTP timeout")
    login_parser.add_argument("--project-id", required=True, help="project id")
    login_parser.add_argument("--role", action="append", dest="roles_list", default=[], help="single role; repeatable")
    login_parser.add_argument("--roles", help="comma-separated roles")
    login_parser.add_argument("--session", help="session name")
    login_parser.add_argument("--agent-name", help="agent name")
    login_parser.add_argument("--local-part", help="local_part override")
    login_parser.add_argument("--mailbox-type", help="session mailbox type override")
    login_parser.add_argument("--expires-in-seconds", type=int, default=86400, help="session lifetime")
    login_parser.add_argument("--accept-messages", dest="accept_messages", action="store_true", default=True)
    login_parser.add_argument("--no-accept-messages", dest="accept_messages", action="store_false")
    login_parser.add_argument("--metadata-json", help="extra metadata JSON object")
    login_parser.add_argument(
        "--output",
        choices=("bundle", "token", "server"),
        default="bundle",
        help="bundle: JSON with base_url+token+session; token: plain token only; server: raw /login payload",
    )
    login_parser.add_argument("--format", choices=("json", "text"), default=None, help="output format; default is json")
    login_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    subparsers.add_parser("healthz", parents=[common], help="check server health")
    subparsers.add_parser("whoami", parents=[common], help="show caller identity")
    subparsers.add_parser("logout", parents=[common], help="invalidate the current agent session token")

    resolve_parser = subparsers.add_parser("resolve", parents=[common], help="resolve one address")
    resolve_parser.add_argument("--address", required=True)

    message_parser = subparsers.add_parser("message", parents=[common], help="load one message")
    message_parser.add_argument("--message-id", required=True)
    message_parser.add_argument("--allow-missing", action="store_true")

    thread_parser = subparsers.add_parser("thread", parents=[common], help="load one thread by thread_id or message_id")
    thread_parser.add_argument("--thread-id")
    thread_parser.add_argument("--message-id")
    thread_parser.add_argument("--allow-missing", action="store_true")

    retry_queue_parser = subparsers.add_parser(
        "retry-queue",
        parents=[common],
        help="list retry-pending deliveries visible to the caller",
    )
    retry_queue_parser.add_argument("--to-address")
    retry_queue_parser.add_argument("--project-id")
    retry_queue_parser.add_argument("--limit", type=int, default=50)

    inbox_parser = subparsers.add_parser(
        "inbox",
        parents=[common],
        help="list recent visible messages for one mailbox",
    )
    inbox_parser.add_argument("--to-address")
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--message-type")
    inbox_parser.add_argument("--thread-id")
    inbox_parser.add_argument("--since")
    inbox_parser.add_argument("--unread-only", action="store_true")

    thread_summaries_parser = subparsers.add_parser(
        "thread-summaries",
        parents=[common],
        help="list compact thread summaries for one mailbox",
    )
    thread_summaries_parser.add_argument("--to-address")
    thread_summaries_parser.add_argument("--limit", type=int, default=20)

    send_parser = subparsers.add_parser("send", parents=[common], help="send one message")
    send_parser.add_argument("--to-address", required=True)
    send_parser.add_argument("--from-address")
    send_parser.add_argument("--subject")
    send_parser.add_argument("--message-type", default="generic")
    send_parser.add_argument("--priority", type=int, default=0)
    send_parser.add_argument("--thread-id")
    send_parser.add_argument("--in-reply-to-message-id")
    send_parser.add_argument("--reply-to-address")
    send_parser.add_argument("--correlation-id")
    send_parser.add_argument("--workflow-id")
    send_parser.add_argument("--idempotency-key")
    send_parser.add_argument("--deliver-after-seconds", type=int, default=0)
    send_parser.add_argument("--expires-in-seconds", type=int)
    send_parser.add_argument("--max-attempts", type=int, default=8)
    send_parser.add_argument("--payload-json", help="message payload JSON object")
    send_parser.add_argument("--payload-file", help="read message payload JSON object from file")
    send_parser.add_argument("--headers-json", help="headers JSON object")
    send_parser.add_argument(
        "--stdin-payload-mode",
        choices=("auto", "json", "text"),
        default="auto",
        help="when payload is read from stdin: auto=json object else text wrapper; json=must be JSON object; text=wrap raw stdin as {\"text\": ...}",
    )

    reply_parser = subparsers.add_parser("reply", parents=[common], help="send a reply for one delivery")
    reply_parser.add_argument("--delivery-json", help="delivery JSON object or claim result JSON")
    reply_parser.add_argument("--delivery-file", help="read delivery JSON object or claim result JSON from file")
    reply_parser.add_argument("--from-address")
    reply_parser.add_argument("--subject")
    reply_parser.add_argument("--message-type", default="generic")
    reply_parser.add_argument("--idempotency-key")
    reply_parser.add_argument("--headers-json", help="headers JSON object")
    reply_parser.add_argument("--payload-json", help="reply payload JSON object")
    reply_parser.add_argument("--payload-file", help="read reply payload JSON object from file")
    reply_parser.add_argument(
        "--stdin-payload-mode",
        choices=("auto", "json", "text"),
        default="auto",
        help="when reply payload is read from stdin: auto=json object else text wrapper; json=must be JSON object; text=wrap raw stdin as {\"text\": ...}",
    )
    reply_parser.add_argument("--ack-after", action="store_true", help="ack the delivery after reply is sent")
    reply_parser.add_argument("--actor", help="actor used for ack-after")

    claim_parser = subparsers.add_parser("claim", parents=[common], help="claim one available delivery")
    claim_parser.add_argument("--to-address")
    claim_parser.add_argument("--to-addresses", help="comma-separated claim addresses")
    claim_parser.add_argument("--consumer-id", help="consumer id; defaults to hostname-pid")
    claim_parser.add_argument("--lease-seconds", type=int, default=60)

    ack_parser = subparsers.add_parser("ack", parents=[common], help="ack one delivery")
    ack_parser.add_argument("--delivery-id", required=True, type=int)
    ack_parser.add_argument("--claim-token", required=True)
    ack_parser.add_argument("--actor")

    nack_parser = subparsers.add_parser("nack", parents=[common], help="nack one delivery")
    nack_parser.add_argument("--delivery-id", required=True, type=int)
    nack_parser.add_argument("--claim-token", required=True)
    nack_parser.add_argument("--retry-after-seconds", type=int, default=30)
    nack_parser.add_argument("--last-error")
    nack_parser.add_argument("--actor")

    heartbeat_parser = subparsers.add_parser("heartbeat", parents=[common], help="extend one delivery lease")
    heartbeat_parser.add_argument("--delivery-id", required=True, type=int)
    heartbeat_parser.add_argument("--claim-token", required=True)
    heartbeat_parser.add_argument("--lease-seconds", type=int, default=60)

    mark_thread_read_parser = subparsers.add_parser(
        "mark-thread-read",
        parents=[common],
        help="mark one mailbox thread as read",
    )
    mark_thread_read_parser.add_argument("--thread-id", required=True)
    mark_thread_read_parser.add_argument("--to-address")
    mark_thread_read_parser.add_argument("--actor")

    consume_parser = subparsers.add_parser("consume", parents=[common], help="continuous claim -> child stdin -> ack/nack worker")
    consume_parser.add_argument("--to-address")
    consume_parser.add_argument("--to-addresses", help="comma-separated claim addresses")
    consume_parser.add_argument("--consumer-id", help="consumer id; defaults to hostname-pid")
    consume_parser.add_argument("--lease-seconds", type=int, default=60)
    consume_parser.add_argument("--heartbeat-interval-seconds", type=float, default=None)
    consume_parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    consume_parser.add_argument("--retry-after-seconds", type=int, default=30)
    consume_parser.add_argument("--ack-exit-codes", default="0", help="comma-separated child exit codes that count as success")
    consume_parser.add_argument("--once", action="store_true", help="process at most one delivery")
    consume_parser.add_argument("--max-deliveries", type=int, help="stop after this many claimed deliveries")
    consume_parser.add_argument(
        "--summary-output",
        choices=("stderr", "stdout", "none"),
        default="stderr",
        help="where to emit consume summary JSON",
    )
    consume_parser.add_argument("handler_command", nargs=argparse.REMAINDER, help="child command to run after --")

    return parser


def _run_login(args: argparse.Namespace) -> dict[str, Any]:
    base_url = _resolve_base_url(_option_value(args, "base_url"))
    harness_token = _resolve_harness_token(args)
    timeout_option = _option_value(args, "timeout_seconds")
    timeout_seconds = timeout_option if timeout_option is not None else _resolve_timeout_seconds()
    roles = _merge_roles(args.roles_list, args.roles)
    metadata = _load_optional_json_object(json_text=args.metadata_json, file_path=None, label="metadata")

    client = MailboxHTTPClient(
        MailboxClientConfig(
            base_url=base_url,
            token=harness_token,
            timeout_seconds=timeout_seconds,
        )
    )
    login_payload = client.login(
        project_id=args.project_id,
        roles=roles,
        session=args.session,
        agent_name=args.agent_name,
        local_part=args.local_part,
        mailbox_type=args.mailbox_type,
        accept_messages=bool(args.accept_messages),
        metadata=metadata,
        expires_in_seconds=args.expires_in_seconds,
    )
    if args.output == "server":
        return login_payload
    session_token = login_payload.get("session_token")
    session = login_payload.get("session")
    if not isinstance(session_token, str) or not session_token:
        raise RuntimeError("server login response did not contain session_token")
    bundle = {
        "ok": True,
        "kind": "agent_session_bundle",
        "base_url": base_url,
        "token": session_token,
        "session_token": session_token,
        "session": session if isinstance(session, dict) else None,
    }
    return bundle


def _build_authenticated_client(args: argparse.Namespace) -> MailboxHTTPClient:
    auth_input = _load_auth_input(args)
    base_url = _resolve_base_url(_option_value(args, "base_url"), auth_input=auth_input)
    timeout_option = _option_value(args, "timeout_seconds")
    timeout_seconds = timeout_option if timeout_option is not None else _resolve_timeout_seconds()
    token = auth_input["token"]

    client = MailboxHTTPClient(
        MailboxClientConfig(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
        )
    )
    session = auth_input.get("session")
    if isinstance(session, dict):
        client._session_token = token
        client._session_profile = session
    return client


def _run_client_command(client: MailboxHTTPClient, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "healthz":
        return client.healthz()
    if args.command == "whoami":
        return client.whoami()
    if args.command == "logout":
        return client.logout()
    if args.command == "resolve":
        return {"ok": True, "mailbox": client.resolve(args.address)}
    if args.command == "message":
        message = client.get_message(args.message_id, allow_missing=bool(args.allow_missing))
        return {"ok": message is not None, "message": message}
    if args.command == "thread":
        thread = client.get_thread(
            thread_id=args.thread_id,
            message_id=args.message_id,
            allow_missing=bool(args.allow_missing),
        )
        return {"ok": thread is not None, "thread": thread}
    if args.command == "inbox":
        return client.get_inbox(
            to_address=args.to_address,
            limit=args.limit,
            message_type=args.message_type,
            thread_id=args.thread_id,
            since=args.since,
            unread_only=bool(args.unread_only),
        )
    if args.command == "thread-summaries":
        return client.get_thread_summaries(
            to_address=args.to_address,
            limit=args.limit,
        )
    if args.command == "retry-queue":
        return client.retry_queue(
            to_address=args.to_address,
            project_id=args.project_id,
            limit=args.limit,
        )
    if args.command == "send":
        auth_source = getattr(args, "_auth_source", None)
        payload = _load_send_payload(args, allow_stdin=auth_source != "stdin")
        headers = _load_optional_json_object(json_text=args.headers_json, file_path=None, label="headers")
        return client.send(
            to_address=args.to_address,
            payload=payload,
            from_address=args.from_address,
            subject=args.subject,
            message_type=args.message_type,
            priority=args.priority,
            thread_id=args.thread_id,
            in_reply_to_message_id=args.in_reply_to_message_id,
            reply_to_address=args.reply_to_address,
            correlation_id=args.correlation_id,
            workflow_id=args.workflow_id,
            idempotency_key=args.idempotency_key,
            headers=headers,
            deliver_after_seconds=args.deliver_after_seconds,
            expires_in_seconds=args.expires_in_seconds,
            max_attempts=args.max_attempts,
        )
    if args.command == "reply":
        auth_source = getattr(args, "_auth_source", None)
        delivery = _load_reply_delivery(args, allow_stdin=auth_source != "stdin")
        payload = _load_reply_payload(args, allow_stdin=auth_source != "stdin")
        headers = _load_optional_json_object(json_text=args.headers_json, file_path=None, label="headers")
        reply_result = client.send_reply(
            delivery,
            payload=payload,
            from_address=args.from_address,
            subject=args.subject,
            message_type=args.message_type,
            headers=headers,
            idempotency_key=args.idempotency_key,
        )
        acked: bool | None = None
        if args.ack_after:
            delivery_id = delivery.get("delivery_id")
            claim_token = delivery.get("claim_token")
            if not isinstance(delivery_id, int):
                raise ValueError("reply --ack-after requires delivery.delivery_id")
            if not isinstance(claim_token, str) or not claim_token:
                raise ValueError("reply --ack-after requires delivery.claim_token")
            acked = client.ack(
                delivery_id=delivery_id,
                claim_token=claim_token,
                actor=args.actor or _default_consumer_id(),
            )
        return {
            "ok": True if acked is not False else False,
            "reply": reply_result,
            "acked": acked,
        }
    if args.command == "claim":
        to_addresses = _split_csv(args.to_addresses)
        delivery = client.claim(
            to_address=args.to_address,
            to_addresses=to_addresses if to_addresses else None,
            consumer_id=args.consumer_id or _default_consumer_id(),
            lease_seconds=args.lease_seconds,
        )
        return {"ok": True, "delivery": delivery}
    if args.command == "ack":
        ok = client.ack(
            delivery_id=args.delivery_id,
            claim_token=args.claim_token,
            actor=args.actor or _default_consumer_id(),
        )
        return {"ok": ok}
    if args.command == "nack":
        ok = client.nack(
            delivery_id=args.delivery_id,
            claim_token=args.claim_token,
            retry_after_seconds=args.retry_after_seconds,
            last_error=args.last_error,
            actor=args.actor or _default_consumer_id(),
        )
        return {"ok": ok}
    if args.command == "heartbeat":
        ok = client.heartbeat(
            delivery_id=args.delivery_id,
            claim_token=args.claim_token,
            lease_seconds=args.lease_seconds,
        )
        return {"ok": ok}
    if args.command == "mark-thread-read":
        return client.mark_thread_read(
            thread_id=args.thread_id,
            to_address=args.to_address,
            actor=args.actor or _default_consumer_id(),
        )
    if args.command == "consume":
        return _run_consume(client, args)
    raise ValueError(f"unsupported command: {args.command}")


def _run_consume(client: MailboxHTTPClient, args: argparse.Namespace) -> dict[str, Any]:
    handler_command = list(args.handler_command or [])
    if handler_command and handler_command[0] == "--":
        handler_command = handler_command[1:]
    if not handler_command:
        raise ValueError("consume requires a child command after --")

    ack_exit_codes = _parse_int_list(args.ack_exit_codes, label="ack-exit-codes")
    heartbeat_interval_seconds = (
        float(args.heartbeat_interval_seconds)
        if args.heartbeat_interval_seconds is not None
        else max(5.0, float(args.lease_seconds) / 3.0)
    )
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat-interval-seconds must be greater than zero")
    if heartbeat_interval_seconds >= args.lease_seconds:
        raise ValueError("heartbeat-interval-seconds must be less than lease-seconds")
    if args.max_deliveries is not None and args.max_deliveries <= 0:
        raise ValueError("max-deliveries must be greater than zero")

    consumer_id = args.consumer_id or _default_consumer_id()
    config = ConsumeConfig(
        to_address=args.to_address,
        to_addresses=tuple(_split_csv(args.to_addresses)),
        consumer_id=consumer_id,
        lease_seconds=int(args.lease_seconds),
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        poll_interval_seconds=float(args.poll_interval_seconds),
        retry_after_seconds=int(args.retry_after_seconds),
        ack_exit_codes=frozenset(ack_exit_codes),
        once=bool(args.once),
        max_deliveries=args.max_deliveries,
    )

    payload = run_consume_loop(
        client,
        config,
        lambda delivery: run_subprocess_handler(
            delivery,
            handler_command,
            cwd=str(Path.cwd()),
        ),
    )
    payload["handler_command"] = handler_command
    return payload


def _resolve_base_url(explicit_base_url: str | None, *, auth_input: dict[str, Any] | None = None) -> str:
    if explicit_base_url:
        return explicit_base_url.rstrip("/")
    if auth_input and isinstance(auth_input.get("base_url"), str) and auth_input["base_url"].strip():
        return str(auth_input["base_url"]).rstrip("/")
    env_base_url = (os.environ.get("MAILBOX_BASE_URL") or "").strip()
    if env_base_url:
        return env_base_url.rstrip("/")
    config_path = _resolve_config_path()
    if config_path and config_path.exists():
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in config file: {config_path}") from exc
        if isinstance(config_data, dict):
            base_url = config_data.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                return base_url.rstrip("/")
    return DEFAULT_MAILBOX_BASE_URL


def _resolve_timeout_seconds() -> float:
    env_timeout = (os.environ.get("MAILBOX_TIMEOUT_SECONDS") or "").strip()
    if env_timeout:
        return float(env_timeout)
    config_path = _resolve_config_path()
    if config_path and config_path.exists():
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in config file: {config_path}") from exc
        if isinstance(config_data, dict) and config_data.get("timeout_seconds") is not None:
            return float(config_data["timeout_seconds"])
    return 10.0


def _resolve_harness_token(args: argparse.Namespace) -> str:
    if args.harness_token:
        return args.harness_token.strip()
    if args.harness_token_file:
        return _read_text(Path(args.harness_token_file)).strip()
    env_token = (os.environ.get("MAILBOX_TOKEN") or "").strip()
    if env_token:
        return env_token
    stdin_auth = _read_stdin_auth()
    if stdin_auth and isinstance(stdin_auth.get("token"), str) and stdin_auth["token"].strip():
        return str(stdin_auth["token"]).strip()
    raise ValueError("missing harness token; use --harness-token, --harness-token-file, stdin, or MAILBOX_TOKEN")


def _load_auth_input(args: argparse.Namespace) -> dict[str, Any]:
    admin_token_option = _option_value(args, "admin_token")
    token_option = _option_value(args, "token")
    token_file_option = _option_value(args, "token_file")
    if admin_token_option and (token_option or token_file_option):
        raise ValueError("provide either --admin-token or --token/--token-file, not both")
    if admin_token_option:
        result = {"token": str(admin_token_option).strip(), "source": "admin_arg"}
        setattr(args, "_auth_source", "admin_arg")
        return result
    if token_option:
        result = {"token": str(token_option).strip(), "source": "arg"}
        setattr(args, "_auth_source", "arg")
        return result
    if token_file_option:
        result = _parse_auth_text(_read_text(Path(str(token_file_option))))
        result["source"] = "file"
        setattr(args, "_auth_source", "file")
        return result
    env_session_token = (os.environ.get("MAILBOX_SESSION_TOKEN") or "").strip()
    if env_session_token:
        result = {"token": env_session_token, "source": "env_session"}
        setattr(args, "_auth_source", "env_session")
        return result
    env_token = (os.environ.get("MAILBOX_TOKEN") or "").strip()
    if env_token:
        result = {"token": env_token, "source": "env"}
        setattr(args, "_auth_source", "env")
        return result
    env_admin_token = (os.environ.get("MAILBOX_ADMIN_TOKEN") or "").strip()
    if env_admin_token:
        result = {"token": env_admin_token, "source": "env_admin"}
        setattr(args, "_auth_source", "env_admin")
        return result
    stdin_auth = _read_stdin_auth()
    if stdin_auth is not None:
        stdin_auth["source"] = "stdin"
        setattr(args, "_auth_source", "stdin")
        return stdin_auth
    raise ValueError(
        "missing auth token; use --admin-token, --token, --token-file, MAILBOX_SESSION_TOKEN, MAILBOX_TOKEN, MAILBOX_ADMIN_TOKEN, or stdin"
    )


def _option_value(args: argparse.Namespace, name: str) -> Any:
    local_value = getattr(args, name, None)
    if local_value is not None and local_value != "" and local_value is not False:
        return local_value
    return getattr(args, f"global_{name}", None)


def _pretty_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "pretty", False) or getattr(args, "global_pretty", False))


def _output_format(args: argparse.Namespace) -> str:
    value = _option_value(args, "format")
    if value in {"json", "text"}:
        return str(value)
    return "json"


_STDIN_UNSET = object()
_STDIN_TEXT_CACHE: str | None | object = _STDIN_UNSET


def _read_stdin_text() -> str | None:
    global _STDIN_TEXT_CACHE
    if _STDIN_TEXT_CACHE is _STDIN_UNSET:
        if sys.stdin.isatty():
            _STDIN_TEXT_CACHE = None
        else:
            _STDIN_TEXT_CACHE = sys.stdin.read()
    if _STDIN_TEXT_CACHE is None:
        return None
    return cast(str, _STDIN_TEXT_CACHE)


def _read_stdin_auth() -> dict[str, Any] | None:
    raw = _read_stdin_text()
    if raw is None or not raw.strip():
        return None
    return _parse_auth_text(raw)


def _parse_auth_text(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if not stripped:
        raise ValueError("auth input is empty")
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return {"token": stripped}
    if isinstance(decoded, str):
        token = decoded.strip()
        if not token:
            raise ValueError("auth token must not be empty")
        return {"token": token}
    if not isinstance(decoded, dict):
        raise ValueError("auth input must be a token string or a JSON object")

    token = decoded.get("token") or decoded.get("session_token") or decoded.get("auth_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("auth JSON must contain token/session_token/auth_token")
    result: dict[str, Any] = {"token": token.strip()}
    if isinstance(decoded.get("base_url"), str) and decoded["base_url"].strip():
        result["base_url"] = decoded["base_url"].strip()
    session = decoded.get("session")
    if isinstance(session, dict):
        result["session"] = session
    elif isinstance(decoded.get("login"), dict):
        result["session"] = decoded["login"]
    return result


def _load_optional_json_object(
    *,
    json_text: str | None,
    file_path: str | None,
    label: str,
    required: bool = False,
) -> dict[str, Any] | None:
    raw_text: str | None = None
    if json_text and file_path:
        raise ValueError(f"provide either --{label}-json or --{label}-file, not both")
    if json_text is not None:
        raw_text = json_text
    elif file_path is not None:
        raw_text = _read_text(Path(file_path))
    if raw_text is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    payload = _load_json_any_from_text(raw_text, label=label)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return payload


def _load_optional_json_any(
    *,
    json_text: str | None,
    file_path: str | None,
    label: str,
) -> Any | None:
    raw_text: str | None = None
    if json_text and file_path:
        raise ValueError(f"provide either --{label}-json or --{label}-file, not both")
    if json_text is not None:
        raw_text = json_text
    elif file_path is not None:
        raw_text = _read_text(Path(file_path))
    if raw_text is None:
        return None
    return _load_json_any_from_text(raw_text, label=label)


def _load_json_any_from_text(raw_text: str, *, label: str) -> Any:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        source = f"{label} JSON"
        raise ValueError(f"invalid {source}: {exc}") from exc
    return payload


def _load_send_payload(args: argparse.Namespace, *, allow_stdin: bool) -> dict[str, Any]:
    payload = _load_optional_json_object(
        json_text=args.payload_json,
        file_path=args.payload_file,
        label="payload",
        required=False,
    )
    if payload is not None:
        return payload
    if not allow_stdin:
        raise ValueError("payload is required; stdin is already being used for auth input")
    stdin_text = _read_stdin_text()
    if stdin_text is None or not stdin_text.strip():
        raise ValueError("payload is required; provide --payload-json, --payload-file, or pipe stdin")
    return _payload_from_stdin(stdin_text, mode=args.stdin_payload_mode)


def _load_reply_payload(args: argparse.Namespace, *, allow_stdin: bool) -> dict[str, Any]:
    payload = _load_optional_json_object(
        json_text=args.payload_json,
        file_path=args.payload_file,
        label="payload",
        required=False,
    )
    if payload is not None:
        return payload
    if not allow_stdin:
        raise ValueError("reply payload is required; stdin is already being used for auth input")
    if args.delivery_json is None and args.delivery_file is None:
        raise ValueError(
            "reply payload is required via --payload-json/--payload-file when delivery comes from stdin"
        )
    stdin_text = _read_stdin_text()
    if stdin_text is None or not stdin_text.strip():
        raise ValueError("reply payload is required; provide --payload-json, --payload-file, or pipe stdin")
    return _payload_from_stdin(stdin_text, mode=args.stdin_payload_mode)


def _load_reply_delivery(args: argparse.Namespace, *, allow_stdin: bool) -> dict[str, Any]:
    payload = _load_optional_json_any(
        json_text=args.delivery_json,
        file_path=args.delivery_file,
        label="delivery",
    )
    if payload is None:
        if not allow_stdin:
            raise ValueError("reply delivery is required; stdin is already being used for auth input")
        stdin_text = _read_stdin_text()
        if stdin_text is None or not stdin_text.strip():
            raise ValueError("reply delivery is required; provide --delivery-json, --delivery-file, or pipe stdin")
        payload = _load_json_any_from_text(stdin_text, label="delivery")
    if not isinstance(payload, dict):
        raise ValueError("reply delivery must be a JSON object")
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else payload
    if not isinstance(delivery, dict):
        raise ValueError("reply delivery input must be a delivery object or {\"delivery\": {...}}")
    required_fields = ("message_id", "from", "to")
    missing = [field for field in required_fields if field not in delivery]
    if missing:
        raise ValueError(f"reply delivery is missing required fields: {', '.join(missing)}")
    return delivery


def _payload_from_stdin(raw_text: str, *, mode: str) -> dict[str, Any]:
    stripped = raw_text.strip()
    if mode == "text":
        return {"text": raw_text}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if mode == "json":
            raise ValueError("stdin payload must be a JSON object")
        return {"text": raw_text}
    if not isinstance(payload, dict):
        if mode == "json":
            raise ValueError("stdin payload must decode to a JSON object")
        return {"value": payload}
    return payload


def _merge_roles(repeated_roles: list[str], csv_roles: str | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for raw in [*repeated_roles, *_split_csv(csv_roles)]:
        stripped = raw.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        merged.append(stripped)
    return merged


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int_list(value: str, *, label: str) -> set[int]:
    parts = _split_csv(value)
    if not parts:
        raise ValueError(f"{label} must contain at least one integer")
    try:
        return {int(part) for part in parts}
    except ValueError as exc:
        raise ValueError(f"{label} must be a comma-separated list of integers") from exc


def _resolve_config_path() -> Path | None:
    config_path = (os.environ.get("MAILBOX_CONFIG") or "").strip()
    if config_path:
        return Path(config_path)
    default_path = Path.cwd() / "mailbox_client.json"
    return default_path if default_path.exists() else None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _emit_payload(payload: dict[str, Any], args: argparse.Namespace) -> None:
    if _output_format(args) == "text":
        print(_format_text_payload(payload, args))
        return
    _emit_json(payload, pretty=_pretty_enabled(args))


def _emit_json(payload: dict[str, Any], *, pretty: bool, file: Any = None) -> None:
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=file)
        return
    print(json.dumps(payload, ensure_ascii=False), file=file)


def _emit_error(payload: dict[str, Any], *, args: argparse.Namespace | None = None) -> None:
    if args is not None and _output_format(args) == "text":
        print(_format_error_text(payload), file=sys.stderr)
        return
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _emit_consume_summary(payload: dict[str, Any], args: argparse.Namespace) -> None:
    target = args.summary_output
    if target == "none":
        return
    if _output_format(args) == "text":
        text = _format_consume_summary_text(payload)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2 if _pretty_enabled(args) else None)
    if target == "stdout":
        print(text)
        return
    print(text, file=sys.stderr)


def _format_text_payload(payload: dict[str, Any], args: argparse.Namespace) -> str:
    command = getattr(args, "command", "")
    if command == "login":
        return _format_login_text(payload)
    if command == "healthz":
        return _format_healthz_text(payload)
    if command == "whoami":
        return _format_whoami_text(payload)
    if command == "logout":
        return _format_logout_text(payload)
    if command == "resolve":
        return _format_resolve_text(payload)
    if command == "message":
        return _format_message_wrapper_text(payload)
    if command == "thread":
        return _format_thread_text(payload)
    if command == "inbox":
        return _format_inbox_text(payload)
    if command == "thread-summaries":
        return _format_thread_summaries_text(payload)
    if command == "retry-queue":
        return _format_retry_queue_text(payload)
    if command == "send":
        return _format_send_text(payload)
    if command == "reply":
        return _format_reply_text(payload)
    if command == "claim":
        return _format_claim_text(payload)
    if command in {"ack", "nack", "heartbeat", "mark-thread-read"}:
        return _format_boolean_result_text(command, payload)
    return json.dumps(payload, ensure_ascii=False, indent=2 if _pretty_enabled(args) else None)


def _format_healthz_text(payload: dict[str, Any]) -> str:
    lines = [f"ok: {_stringify_value(payload.get('ok'))}"]
    _append_field(lines, "service", payload.get("service"))
    return "\n".join(lines)


def _format_login_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_field(lines, "base_url", payload.get("base_url"))
    token = payload.get("token") or payload.get("session_token")
    _append_field(lines, "session_token", token)
    _append_field(lines, "reused", payload.get("reused"))
    _append_common_context(lines, payload)
    session = payload.get("session") if isinstance(payload.get("session"), dict) else payload.get("login")
    if isinstance(session, dict):
        if lines:
            lines.append("")
        lines.append("session:")
        lines.extend(_indent_lines(_format_session_lines(session), prefix="  "))
    return "\n".join(lines)


def _format_whoami_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    kind = payload.get("auth_kind") or ("agent_session" if isinstance(payload.get("session"), dict) else "harness")
    _append_field(lines, "kind", kind)
    _append_common_context(lines, payload)
    session = payload.get("session")
    if isinstance(session, dict):
        lines.append("")
        lines.append("session:")
        lines.extend(_indent_lines(_format_session_lines(session), prefix="  "))
        return "\n".join(lines)

    defaults = payload.get("defaults")
    if isinstance(defaults, dict):
        lines.append("")
        lines.append("defaults:")
        _append_field(lines, "  default_from_address", defaults.get("default_from_address"))
        _append_field(lines, "  default_inbox_address", defaults.get("default_inbox_address"))
        _append_field(lines, "  updated_at", defaults.get("updated_at"))

    mailboxes = payload.get("mailboxes")
    if isinstance(mailboxes, list):
        lines.append("")
        lines.append("mailboxes:")
        if not mailboxes:
            lines.append("  - none")
        for mailbox in mailboxes:
            if not isinstance(mailbox, dict):
                continue
            enabled = _stringify_value(mailbox.get("enabled"))
            accepts = _stringify_value(mailbox.get("accept_messages"))
            lines.append(
                f"  - {mailbox.get('address')} ({mailbox.get('mailbox_type')}, enabled={enabled}, accept_messages={accepts})"
            )
    return "\n".join(lines)


def _format_resolve_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    mailbox = payload.get("mailbox")
    if isinstance(mailbox, dict):
        _append_field(lines, "address", mailbox.get("address"))
        _append_field(lines, "mailbox_id", mailbox.get("mailbox_id"))
        _append_field(lines, "local_part", mailbox.get("local_part"))
        _append_field(lines, "project_id", mailbox.get("project_id"))
        _append_field(lines, "harness_id", mailbox.get("harness_id"))
        _append_field(lines, "mailbox_type", mailbox.get("mailbox_type"))
    _append_common_context(lines, payload)
    return "\n".join(lines)


def _format_message_wrapper_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    message = payload.get("message")
    if not isinstance(message, dict):
        lines.append("")
        lines.append("message: not found")
        return "\n".join(lines)
    lines.append("")
    lines.extend(_format_message_lines(message))
    return "\n".join(lines)


def _format_thread_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        lines.append("")
        lines.append("thread: not found")
        return "\n".join(lines)
    lines.append("")
    _append_field(lines, "thread_id", thread.get("thread_id"))
    _append_field(lines, "message_count", thread.get("message_count"))
    messages = thread.get("messages")
    if isinstance(messages, list):
        for index, message in enumerate(messages, start=1):
            if not isinstance(message, dict):
                continue
            lines.append("")
            lines.append(f"message[{index}]:")
            lines.extend(_indent_lines(_format_message_lines(message), prefix="  "))
    return "\n".join(lines)


def _format_logout_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_field(lines, "logged_out", payload.get("logged_out"))
    _append_common_context(lines, payload)
    session = payload.get("session")
    if isinstance(session, dict):
        lines.append("")
        lines.append("session:")
        lines.extend(_indent_lines(_format_session_lines(session), prefix="  "))
    return "\n".join(lines)


def _format_thread_summaries_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    threads = payload.get("threads")
    if not isinstance(threads, list):
        lines.append("")
        lines.append("threads: none")
        return "\n".join(lines)
    if not threads:
        lines.append("")
        lines.append("threads: none")
        return "\n".join(lines)
    for index, thread in enumerate(threads, start=1):
        if not isinstance(thread, dict):
            continue
        lines.append("")
        lines.append(f"thread[{index}]:")
        lines.extend(_indent_lines(_format_thread_summary_lines(thread), prefix="  "))
    return "\n".join(lines)


def _format_inbox_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        lines.append("")
        lines.append("messages: none")
        return "\n".join(lines)
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        lines.append("")
        lines.append(f"message[{index}]:")
        lines.extend(_indent_lines(_format_message_lines(message), prefix="  "))
    return "\n".join(lines)


def _format_retry_queue_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    deliveries = payload.get("deliveries")
    if not isinstance(deliveries, list) or not deliveries:
        lines.append("")
        lines.append("deliveries: none")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"deliveries: {len(deliveries)}")
    for index, delivery in enumerate(deliveries, start=1):
        if not isinstance(delivery, dict):
            continue
        lines.append("")
        lines.append(f"delivery[{index}]:")
        lines.extend(_indent_lines(_format_retry_delivery_lines(delivery), prefix="  "))
    return "\n".join(lines)


def _format_send_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_field(lines, "message_id", payload.get("message_id"))
    _append_field(lines, "delivery_id", payload.get("delivery_id"))
    _append_field(lines, "thread_id", payload.get("thread_id"))
    _append_field(lines, "deduplicated", payload.get("deduplicated"))
    _append_common_context(lines, payload)
    return "\n".join(lines)


def _format_reply_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_field(lines, "acked", payload.get("acked"))
    reply = payload.get("reply")
    if isinstance(reply, dict):
        lines.append("")
        lines.append("reply:")
        lines.extend(_indent_lines(_format_send_text(reply).splitlines(), prefix="  "))
    return "\n".join(lines)


def _format_claim_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_common_context(lines, payload)
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        lines.append("")
        lines.append("delivery: none")
        return "\n".join(lines)
    lines.append("")
    lines.extend(_format_delivery_lines(delivery))
    return "\n".join(lines)


def _format_boolean_result_text(command: str, payload: dict[str, Any]) -> str:
    lines = [f"{command}: {_stringify_value(payload.get('ok'))}"]
    _append_common_context(lines, payload)
    return "\n".join(lines)


def _format_consume_summary_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_field(lines, "ok", payload.get("ok"))
    _append_field(lines, "processed", payload.get("processed"))
    _append_field(lines, "acked", payload.get("acked"))
    _append_field(lines, "nacked", payload.get("nacked"))
    _append_field(lines, "empty_polls", payload.get("empty_polls"))
    _append_field(lines, "last_delivery_id", payload.get("last_delivery_id"))
    handler_command = payload.get("handler_command")
    if isinstance(handler_command, list) and handler_command:
        lines.append(f"handler_command: {' '.join(str(part) for part in handler_command)}")
    _append_field(lines, "heartbeat_interval_seconds", payload.get("heartbeat_interval_seconds"))
    return "\n".join(lines)


def _format_error_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"error: {payload.get('error') or 'unknown error'}")
    _append_field(lines, "status", payload.get("status"))
    if payload.get("payload") is not None:
        _append_json_block(lines, "payload", payload.get("payload"))
    return "\n".join(lines)


def _format_session_lines(session: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    _append_field(lines, "agent_session_id", session.get("agent_session_id"))
    _append_field(lines, "project_id", session.get("project_id"))
    _append_field(lines, "agent_name", session.get("agent_name"))
    _append_field(lines, "session_name", session.get("session_name"))
    _append_field(lines, "default_from_address", session.get("default_from_address"))
    _append_field(lines, "default_inbox_address", session.get("default_inbox_address"))
    _append_list_block(lines, "default_claim_addresses", session.get("default_claim_addresses"))
    _append_list_block(lines, "send_as_addresses", session.get("send_as_addresses"))
    _append_list_block(lines, "claim_addresses", session.get("claim_addresses"))
    _append_list_block(lines, "allowed_addresses", session.get("allowed_addresses"))
    _append_field(lines, "created_at", session.get("created_at"))
    _append_field(lines, "expires_at", session.get("expires_at"))
    _append_field(lines, "last_used_at", session.get("last_used_at"))
    if session.get("metadata") is not None:
        _append_json_block(lines, "metadata", session.get("metadata"))
    if session.get("created_mailboxes") is not None:
        _append_list_block(lines, "created_mailboxes", session.get("created_mailboxes"))
    return lines


def _format_message_lines(message: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    _append_field(lines, "message_id", message.get("message_id"))
    _append_field(lines, "thread_id", message.get("thread_id"))
    _append_field(lines, "in_reply_to_message_id", message.get("in_reply_to_message_id"))
    _append_field(lines, "correlation_id", message.get("correlation_id"))
    _append_field(lines, "workflow_id", message.get("workflow_id"))
    _append_field(lines, "from", message.get("from"))
    _append_list_block(lines, "to", message.get("to"))
    _append_field(lines, "subject", message.get("subject"))
    _append_field(lines, "message_type", message.get("message_type"))
    _append_field(lines, "priority", message.get("priority"))
    _append_field(lines, "created_at", message.get("created_at"))
    _append_json_block(lines, "payload", message.get("payload"))
    if message.get("headers") is not None:
        _append_json_block(lines, "headers", message.get("headers"))
    return lines


def _format_delivery_lines(delivery: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    _append_field(lines, "delivery_id", delivery.get("delivery_id"))
    _append_field(lines, "message_id", delivery.get("message_id"))
    _append_field(lines, "claim_token", delivery.get("claim_token"))
    _append_field(lines, "from", delivery.get("from"))
    _append_field(lines, "to", delivery.get("to"))
    _append_field(lines, "subject", delivery.get("subject"))
    _append_field(lines, "message_type", delivery.get("message_type"))
    _append_field(lines, "priority", delivery.get("priority"))
    _append_field(lines, "thread_id", delivery.get("thread_id"))
    _append_field(lines, "in_reply_to_message_id", delivery.get("in_reply_to_message_id"))
    _append_field(lines, "correlation_id", delivery.get("correlation_id"))
    _append_field(lines, "workflow_id", delivery.get("workflow_id"))
    _append_field(lines, "attempt_count", delivery.get("attempt_count"))
    _append_field(lines, "claimed_at", delivery.get("claimed_at"))
    _append_field(lines, "lease_until", delivery.get("lease_until"))
    _append_json_block(lines, "payload", delivery.get("payload"))
    if delivery.get("headers") is not None:
        _append_json_block(lines, "headers", delivery.get("headers"))
    return lines


def _format_thread_summary_lines(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    _append_field(lines, "thread_id", summary.get("thread_id"))
    _append_field(lines, "latest_message_id", summary.get("latest_message_id"))
    _append_field(lines, "latest_message_at", summary.get("latest_message_at"))
    _append_field(lines, "latest_from_address", summary.get("latest_from_address"))
    _append_field(lines, "message_count", summary.get("message_count"))
    _append_field(lines, "reply_count", summary.get("reply_count"))
    _append_field(lines, "unread", summary.get("unread"))
    return lines


def _format_retry_delivery_lines(delivery: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    _append_field(lines, "delivery_id", delivery.get("delivery_id"))
    _append_field(lines, "message_id", delivery.get("message_id"))
    _append_field(lines, "to", delivery.get("to"))
    _append_field(lines, "status", delivery.get("status"))
    _append_field(lines, "attempt_count", delivery.get("attempt_count"))
    _append_field(lines, "max_attempts", delivery.get("max_attempts"))
    _append_field(lines, "next_retry_at", delivery.get("next_retry_at"))
    _append_field(lines, "last_error_summary", delivery.get("last_error_summary"))
    return lines


def _append_common_context(lines: list[str], payload: dict[str, Any]) -> None:
    _append_field(lines, "caller_harness_id", payload.get("caller_harness_id"))
    _append_field(lines, "auth_kind", payload.get("auth_kind"))
    _append_field(lines, "agent_session_id", payload.get("agent_session_id"))
    _append_field(lines, "admin", payload.get("admin"))


def _append_field(lines: list[str], label: str, value: Any) -> None:
    if value is None:
        return
    lines.append(f"{label}: {_stringify_value(value)}")


def _append_list_block(lines: list[str], label: str, value: Any) -> None:
    if not isinstance(value, list):
        return
    lines.append(f"{label}:")
    if not value:
        lines.append("  - none")
        return
    for item in value:
        lines.append(f"  - {_stringify_value(item)}")


def _append_json_block(lines: list[str], label: str, value: Any) -> None:
    if value is None:
        return
    lines.append(f"{label}:")
    json_text = json.dumps(value, ensure_ascii=False, indent=2)
    lines.extend(_indent_lines(json_text.splitlines(), prefix="  "))


def _indent_lines(lines: list[str], *, prefix: str) -> list[str]:
    return [f"{prefix}{line}" if line else prefix.rstrip() for line in lines]


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _default_consumer_id() -> str:
    return f"{socket.gethostname().lower()}-{os.getpid()}"


if __name__ == "__main__":
    main()
