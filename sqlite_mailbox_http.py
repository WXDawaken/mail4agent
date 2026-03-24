from __future__ import annotations

"""SQLite mailbox HTTP server with harness and agent-session authentication.

Run locally:
    $env:MAILBOX_ADMIN_TOKEN = "dev-admin-token"
    python sqlite_mailbox_http.py --db ./mailbox.sqlite --host 127.0.0.1 --port 8787

Auth model:
    - `/healthz` is public.
    - Mailbox routes accept `Authorization: Bearer <token>` or `X-Mailbox-Token: <token>`.
      The token may be either:
        * a harness token for harness-wide access
        * an agent session token issued by `POST /login`
        * the configured admin token for direct operator mailbox access
    - `POST /login` always requires a harness token and returns a short-lived agent session token.
      Clients pass semantic identity such as `project_id`, `role`/`roles`, and `session`;
      the server derives the concrete mailbox addresses and the session's allowed address set.
    - `/admin/*` routes accept either:
        * the env token via `Authorization: Bearer <token>` or `X-Mailbox-Admin-Token: <token>`
        * an initialized admin account via `Authorization: Basic <base64(username:password)>`
    - If no admin account exists and no env token is configured, startup prints a one-time
      loopback setup URL like `/setup-admin?token=...` for initial admin creation.
    - `/admin-ui` and `/setup-admin` are loopback-only browser pages.
"""

import argparse
import base64
import hmac
import ipaddress
import json
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlite_mailbox import (
    SQLiteMailbox,
    canonicalize_address,
    normalize_address_component,
    parse_utc_timestamp,
    split_address,
)


_ASSET_DIR = Path(__file__).resolve().parent


def _load_html_asset(filename: str) -> str:
    return (_ASSET_DIR / filename).read_text(encoding="utf-8")


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


_UNSET_HTTP = object()


@dataclass(frozen=True)
class AuthContext:
    kind: str
    harness_id: str | None = None
    agent_session_id: int | None = None
    project_id: str | None = None
    session_name: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    allowed_addresses: tuple[str, ...] = ()
    send_as_addresses: tuple[str, ...] = ()
    claim_addresses: tuple[str, ...] = ()
    default_from_address: str | None = None
    default_claim_addresses: tuple[str, ...] = ()
    default_inbox_address: str | None = None
    bootstrap_admin: bool = False


class AuthenticationError(Exception):
    pass


class MailboxHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        db_path: str,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.mailbox = SQLiteMailbox(db_path)
        self.mailbox.init_db()
        self.admin_token = (os.environ.get("MAILBOX_ADMIN_TOKEN") or "").strip() or None
        self.debug_mode = _env_flag("MAILBOX_HTTP_DEBUG")
        self.admin_ui_html = _load_html_asset("sqlite_mailbox_admin_ui.html")
        self.setup_admin_html = _load_html_asset("sqlite_mailbox_setup_admin.html")


class MailboxRequestHandler(BaseHTTPRequestHandler):
    server_version = "SQLiteMailboxHTTP/0.3"
    protocol_version = "HTTP/1.1"

    @property
    def mailbox(self) -> SQLiteMailbox:
        return self.server.mailbox  # type: ignore[attr-defined]

    @property
    def admin_token(self) -> str | None:
        return self.server.admin_token  # type: ignore[attr-defined]

    @property
    def debug_mode(self) -> bool:
        return bool(self.server.debug_mode)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _html_response(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _extract_bearer_token(self, custom_header: str) -> str | None:
        authorization = self.headers.get("Authorization")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                token = token.strip()
                if not token:
                    raise AuthenticationError("invalid Authorization header; expected Bearer token")
                return token

        token = self.headers.get(custom_header)
        if token is None:
            return None
        token = token.strip()
        if not token:
            raise AuthenticationError(f"empty {custom_header} header")
        return token

    def _is_loopback_request(self) -> bool:
        host = self.client_address[0]
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host.lower() == "localhost"

    def _extract_basic_credentials(self) -> tuple[str, str] | None:
        authorization = self.headers.get("Authorization")
        if not authorization:
            return None
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic":
            return None
        encoded = encoded.strip()
        if not encoded:
            raise AuthenticationError("invalid Authorization header; expected Basic credentials")
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
        except Exception as exc:
            raise AuthenticationError("invalid Basic authorization encoding") from exc
        username, separator, password = decoded.partition(":")
        if not separator:
            raise AuthenticationError("invalid Basic authorization payload")
        return username, password

    def _serve_admin_ui(self) -> None:
        if not self._is_loopback_request():
            raise PermissionError("admin UI is only available from loopback")
        admin_mode = "env token and/or admin username/password"
        self._html_response(
            200,
            self.server.admin_ui_html.replace("__ADMIN_MODE__", admin_mode),  # type: ignore[attr-defined]
        )

    def _serve_setup_admin_page(self, setup_token: str | None) -> None:
        if not self._is_loopback_request():
            raise PermissionError("setup page is only available from loopback")
        if self.mailbox.has_admin_account():
            raise PermissionError("admin account is already initialized")
        if not setup_token:
            raise AuthenticationError("missing setup token")
        if not self.mailbox.is_valid_admin_bootstrap_token(setup_token):
            raise AuthenticationError("invalid or expired setup token")
        self._html_response(200, self.server.setup_admin_html)  # type: ignore[attr-defined]

    def _require_harness_auth(self) -> AuthContext:
        token = self._extract_bearer_token("X-Mailbox-Token")
        if not token:
            raise AuthenticationError("missing harness token")
        harness_id = self.mailbox.authenticate_harness_token(token)
        if not harness_id:
            raise AuthenticationError("invalid harness token")
        return AuthContext(kind="harness", harness_id=harness_id)

    def _auth_context_from_principal(self, principal: dict[str, Any]) -> AuthContext:
        if principal["kind"] == "harness":
            return AuthContext(kind="harness", harness_id=principal["harness_id"])
        return AuthContext(
            kind="agent_session",
            harness_id=principal["harness_id"],
            agent_session_id=int(principal["agent_session_id"]),
            project_id=principal.get("project_id"),
            session_name=principal.get("session_name"),
            created_at=principal.get("created_at"),
            expires_at=principal.get("expires_at"),
            last_used_at=principal.get("last_used_at"),
            allowed_addresses=tuple(str(item) for item in principal.get("allowed_addresses", [])),
            send_as_addresses=tuple(str(item) for item in principal.get("send_as_addresses", [])),
            claim_addresses=tuple(str(item) for item in principal.get("claim_addresses", [])),
            default_from_address=principal.get("default_from_address"),
            default_claim_addresses=tuple(str(item) for item in principal.get("default_claim_addresses", [])),
            default_inbox_address=principal.get("default_inbox_address"),
        )

    def _session_expiry_seconds(self, expires_at: str | None) -> int | None:
        if not expires_at:
            return None
        try:
            expires_at_dt = parse_utc_timestamp(expires_at)
        except ValueError:
            return None
        now = datetime.now(timezone.utc)
        remaining_seconds = int((expires_at_dt - now).total_seconds())
        return max(0, remaining_seconds)

    def _require_mailbox_auth(self) -> AuthContext:
        token = self._extract_bearer_token("X-Mailbox-Token")
        if not token:
            raise AuthenticationError("missing mailbox token")
        if self.admin_token and hmac.compare_digest(token, self.admin_token):
            return AuthContext(kind="admin")
        principal = self.mailbox.authenticate_mailbox_principal(token)
        if not principal:
            raise AuthenticationError("invalid mailbox token")
        return self._auth_context_from_principal(principal)

    def _require_admin_auth(self) -> AuthContext:
        token = self._extract_bearer_token("X-Mailbox-Admin-Token")
        if token and self.admin_token and hmac.compare_digest(token, self.admin_token):
            return AuthContext(kind="admin")
        if token:
            raise AuthenticationError("invalid admin token")

        basic_credentials = self._extract_basic_credentials()
        if basic_credentials:
            username, password = basic_credentials
            admin_username = self.mailbox.authenticate_admin_credentials(username, password)
            if not admin_username:
                raise AuthenticationError("invalid admin username or password")
            return AuthContext(kind="admin")

        if self.admin_token:
            raise AuthenticationError("missing admin token or Basic credentials")
        if self.mailbox.has_admin_account():
            raise AuthenticationError("missing admin credentials")
        raise AuthenticationError("admin is not initialized; use the one-time setup URL from server startup")

    def _mailbox_payload(self, mailbox: Any) -> dict[str, Any]:
        return {
            "mailbox_id": mailbox.mailbox_id,
            "address": mailbox.address,
            "local_part": mailbox.local_part,
            "project_id": mailbox.project_id,
            "harness_id": mailbox.harness_id,
            "mailbox_type": mailbox.mailbox_type,
        }

    def _with_caller(self, payload: dict[str, Any], auth: AuthContext | None) -> dict[str, Any]:
        if not auth:
            return payload
        enriched = {
            **payload,
            "auth_kind": auth.kind,
        }
        if auth.kind == "admin":
            enriched["admin"] = True
            return enriched
        if auth.harness_id:
            enriched["caller_harness_id"] = auth.harness_id
        if auth.kind == "agent_session" and auth.agent_session_id is not None:
            enriched["agent_session_id"] = auth.agent_session_id
        return enriched

    def _authorize_delivery(self, delivery_id: int, auth: AuthContext) -> dict[str, Any] | None:
        context = self.mailbox.get_delivery_context(delivery_id)
        if not context:
            return None
        if auth.kind == "admin":
            return context
        if auth.kind == "agent_session":
            if context["to_address"] not in auth.claim_addresses:
                raise PermissionError(
                    f"delivery {delivery_id} targets {context['to_address']}, which is not claimable by this agent session"
                )
            return context
        if context["to_harness_id"] != auth.harness_id:
            raise PermissionError(
                f"delivery {delivery_id} belongs to harness {context['to_harness_id']}, not caller harness {auth.harness_id}"
            )
        return context

    def _normalize_session_address(self, address: str, auth: AuthContext, *, purpose: str) -> str:
        canonical = canonicalize_address(address)
        allowed_addresses = set(auth.allowed_addresses)
        if canonical not in allowed_addresses:
            raise PermissionError(f"address {canonical} is not available in the current agent session for {purpose}")
        return canonical

    def _effective_from_address(self, body: dict[str, Any], auth: AuthContext) -> str:
        requested_from = _optional(body, "from_address", str)
        if auth.kind == "admin":
            if requested_from is None:
                raise ValueError("missing required field: from_address")
            return self.mailbox.resolve_address(requested_from).address
        if auth.kind == "agent_session":
            if requested_from is None:
                if not auth.default_from_address:
                    raise ValueError("from_address is required because this agent session has no default_from_address")
                return auth.default_from_address
            canonical = canonicalize_address(requested_from)
            if canonical not in set(auth.send_as_addresses):
                raise PermissionError(f"from_address {canonical} is not allowed for the current agent session")
            return canonical
        if requested_from is None:
            raise ValueError("missing required field: from_address")
        self.mailbox.resolve_address_for_harness(requested_from, auth.harness_id or "")
        return requested_from

    def _effective_claim_addresses(self, body: dict[str, Any], auth: AuthContext) -> list[str]:
        requested_single = _optional(body, "to_address", str)
        requested_many = body.get("to_addresses")
        if requested_single is not None and requested_many is not None:
            raise ValueError("provide either to_address or to_addresses, not both")

        if requested_many is not None:
            if not isinstance(requested_many, list) or not requested_many:
                raise ValueError("field to_addresses must be a non-empty list of strings")
            normalized_many: list[str] = []
            for item in requested_many:
                if not isinstance(item, str):
                    raise ValueError("field to_addresses must contain only strings")
                normalized_many.append(canonicalize_address(item))
        elif requested_single is not None:
            normalized_many = [canonicalize_address(requested_single)]
        else:
            normalized_many = []

        if auth.kind == "agent_session":
            if not normalized_many:
                defaults = list(auth.default_claim_addresses) or list(auth.claim_addresses)
                if not defaults:
                    raise ValueError("this agent session has no claimable addresses; specify to_address or adjust login roles/session")
                return defaults
            claimable = set(auth.claim_addresses)
            for address in normalized_many:
                if address not in claimable:
                    raise PermissionError(f"to_address {address} is not claimable by the current agent session")
            return normalized_many

        if auth.kind == "admin":
            if not normalized_many:
                raise ValueError("missing required field: to_address")
            for address in normalized_many:
                self.mailbox.resolve_address(address)
            return normalized_many

        if not normalized_many:
            raise ValueError("missing required field: to_address")
        for address in normalized_many:
            self.mailbox.resolve_address_for_harness(address, auth.harness_id or "")
        return normalized_many

    def _resolve_thread_state_mailbox(
        self,
        requested_address: str | None,
        auth: AuthContext,
        *,
        missing_error: str,
        purpose: str = "thread-state routes",
    ) -> Any | None:
        resolved_address = requested_address
        if not resolved_address:
            if auth.kind == "agent_session" and auth.default_inbox_address:
                resolved_address = auth.default_inbox_address
            elif auth.kind == "agent_session":
                raise ValueError("to_address is required because this agent session has no default_inbox_address")
            else:
                raise ValueError(missing_error)

        canonical = canonicalize_address(resolved_address)
        if auth.kind == "admin":
            return self.mailbox.resolve_address(canonical)
        if auth.kind == "agent_session":
            if canonical not in set(auth.allowed_addresses):
                raise PermissionError(
                    f"address {canonical} is not available in the current agent session for {purpose}"
                )
            return self.mailbox.resolve_address(canonical)

        caller_harness_id = auth.harness_id or ""
        _, _, address_harness_id = split_address(canonical)
        if address_harness_id != caller_harness_id:
            return None
        return self.mailbox.resolve_address_for_harness(canonical, caller_harness_id)

    def _retry_queue_query(self, query: dict[str, list[str]], auth: AuthContext) -> tuple[str | None, str | None, int]:
        query_to_address = (query.get("to_address") or [None])[0]
        query_project_id = (query.get("project_id") or [None])[0]
        query_limit = (query.get("limit") or [None])[0]

        if query_limit in {None, ""}:
            limit = 50
        else:
            try:
                limit = int(query_limit)
            except (TypeError, ValueError) as exc:
                raise ValueError("query parameter limit must be an integer") from exc
        if limit <= 0:
            raise ValueError("query parameter limit must be greater than zero")

        normalized_to_address: str | None = None
        if query_to_address:
            if auth.kind == "admin":
                normalized_to_address = self.mailbox.resolve_address(query_to_address).address
            elif auth.kind == "agent_session":
                normalized_to_address = canonicalize_address(query_to_address)
                if normalized_to_address not in set(auth.claim_addresses):
                    raise PermissionError(
                        f"to_address {normalized_to_address} is not visible to the current agent session"
                    )
            else:
                normalized_to_address = self.mailbox.resolve_address_for_harness(
                    query_to_address,
                    auth.harness_id or "",
                ).address

        normalized_project_id = (
            normalize_address_component(query_project_id, "project_id") if query_project_id else None
        )
        return normalized_to_address, normalized_project_id, limit

    def _dispatch_admin(
        self, method: str, path: str, body: dict[str, Any], query: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        self._require_admin_auth()

        if method == "POST" and path == "/admin/upsert_harness":
            self.mailbox.upsert_harness(
                harness_id=_require(body, "harness_id", str),
                display_name=_optional(body, "display_name", str),
                enabled=bool(body.get("enabled", True)),
            )
            return 200, {"ok": True}

        if method == "POST" and path == "/admin/upsert_project":
            self.mailbox.upsert_project(
                project_id=_require(body, "project_id", str),
                harness_id=_require(body, "harness_id", str),
                display_name=_optional(body, "display_name", str),
                enabled=bool(body.get("enabled", True)),
            )
            return 200, {"ok": True}

        if method == "POST" and path == "/admin/upsert_mailbox":
            mailbox = self.mailbox.upsert_mailbox(
                address=_require(body, "address", str),
                mailbox_type=_optional(body, "mailbox_type", str) or "session",
                enabled=bool(body.get("enabled", True)),
                accept_messages=bool(body.get("accept_messages", True)),
                metadata=_optional_dict(body, "metadata"),
            )
            return 200, {"ok": True, "mailbox": self._mailbox_payload(mailbox)}

        if method == "POST" and path == "/admin/bind_mailbox":
            binding_id = self.mailbox.bind_mailbox(
                address=_require(body, "address", str),
                session_id=_optional(body, "session_id", str),
                run_id=_optional(body, "run_id", str),
                consumer_id=_optional(body, "consumer_id", str),
                bind_mode=_optional(body, "bind_mode", str) or "exclusive",
                lease_seconds=int(body.get("lease_seconds", 60)),
            )
            return 200, {"ok": True, "binding_id": binding_id}

        if method == "POST" and path == "/admin/add_routing_policy":
            policy_id = self.mailbox.add_routing_policy(
                effect=_require(body, "effect", str),
                priority=int(body.get("priority", 0)),
                from_harness_id=_optional(body, "from_harness_id", str),
                from_project_id=_optional(body, "from_project_id", str),
                from_mailbox_type=_optional(body, "from_mailbox_type", str),
                to_harness_id=_optional(body, "to_harness_id", str),
                to_project_id=_optional(body, "to_project_id", str),
                to_mailbox_type=_optional(body, "to_mailbox_type", str),
                description=_optional(body, "description", str),
                enabled=bool(body.get("enabled", True)),
            )
            return 200, {"ok": True, "policy_id": policy_id}

        if method == "POST" and path == "/admin/allow_same_project":
            policy_id = self.mailbox.allow_same_project(
                project_id=_require(body, "project_id", str),
                harness_id=_require(body, "harness_id", str),
                priority=int(body.get("priority", 100)),
            )
            return 200, {"ok": True, "policy_id": policy_id}

        if method == "POST" and path == "/admin/allow_cross_harness_same_project":
            policy_id = self.mailbox.allow_cross_harness_same_project(
                project_id=_require(body, "project_id", str),
                from_harness_id=_require(body, "from_harness_id", str),
                to_harness_id=_require(body, "to_harness_id", str),
                priority=int(body.get("priority", 90)),
            )
            return 200, {"ok": True, "policy_id": policy_id}

        if method == "POST" and path == "/admin/deny_all":
            policy_id = self.mailbox.deny_all(priority=int(body.get("priority", -1000)))
            return 200, {"ok": True, "policy_id": policy_id}

        if method == "POST" and path == "/admin/create_harness_token":
            result = self.mailbox.create_harness_token(
                harness_id=_require(body, "harness_id", str),
                token_name=_optional(body, "token_name", str),
            )
            return 200, {"ok": True, **result}

        if method == "POST" and path == "/admin/preview_agent_session":
            preview = self.mailbox.preview_agent_session_for_harness(
                harness_id=_require(body, "harness_id", str),
                project_id=_require(body, "project_id", str),
                role=_optional(body, "role", str),
                roles=_optional_str_list(body, "roles"),
                session_name=_optional(body, "session", str),
                agent_name=_optional(body, "agent_name", str),
                local_part=_optional(body, "local_part", str),
                mailbox_type=_optional(body, "mailbox_type", str),
                accept_messages=bool(body.get("accept_messages", True)),
                metadata=_optional_dict(body, "metadata"),
                expires_in_seconds=int(body.get("expires_in_seconds", 86_400)),
            )
            return 200, {"ok": True, "preview": preview}

        if method == "POST" and path == "/admin/disable_harness_token":
            ok = self.mailbox.disable_harness_token(int(_require(body, "token_id", (int, str))))
            return 200, {"ok": ok}

        if method == "POST" and path == "/admin/set_harness_defaults":
            defaults = self.mailbox.set_harness_defaults(
                harness_id=_require(body, "harness_id", str),
                default_from_address=body["default_from_address"] if "default_from_address" in body else _UNSET_HTTP,
                default_inbox_address=body["default_inbox_address"] if "default_inbox_address" in body else _UNSET_HTTP,
            )
            return 200, {"ok": True, "defaults": defaults}

        if method == "GET" and path == "/admin/get_harness_defaults":
            harness_id = (query.get("harness_id") or [None])[0]
            if not harness_id:
                raise ValueError("missing query parameter: harness_id")
            return 200, {"ok": True, "defaults": self.mailbox.get_harness_defaults(harness_id)}

        if method == "GET" and path == "/admin/list_harnesses":
            return 200, {"ok": True, "harnesses": self.mailbox.list_harnesses()}

        if method == "GET" and path == "/admin/list_projects":
            harness_id = (query.get("harness_id") or [None])[0]
            return 200, {"ok": True, "projects": self.mailbox.list_projects(harness_id=harness_id)}

        if method == "GET" and path == "/admin/list_mailboxes":
            harness_id = (query.get("harness_id") or [None])[0]
            project_id = (query.get("project_id") or [None])[0]
            return 200, {
                "ok": True,
                "mailboxes": self.mailbox.list_mailboxes(harness_id=harness_id, project_id=project_id),
            }

        if method == "GET" and path == "/admin/list_harness_tokens":
            harness_id = (query.get("harness_id") or [None])[0]
            if not harness_id:
                raise ValueError("missing query parameter: harness_id")
            tokens = self.mailbox.list_harness_tokens(harness_id)
            return 200, {"ok": True, "harness_id": harness_id.lower(), "tokens": tokens}

        if method == "POST" and path == "/admin/resolve":
            mailbox = self.mailbox.resolve_address(_require(body, "address", str))
            return 200, {"ok": True, "mailbox": self._mailbox_payload(mailbox)}

        if method == "GET" and path == "/admin/thread":
            query_thread_id = (query.get("thread_id") or [None])[0]
            query_message_id = (query.get("message_id") or [None])[0]
            if not query_thread_id and not query_message_id:
                raise ValueError("missing query parameter: thread_id or message_id")
            thread = self.mailbox.get_thread(
                thread_id=query_thread_id,
                message_id=query_message_id,
            )
            if not thread:
                return 404, {"ok": False, "error": "thread not found"}
            return 200, {"ok": True, "thread": thread}

        return 404, {"ok": False, "error": f"unknown route: {method} {path}"}

    def _dispatch_harness(
        self, method: str, path: str, body: dict[str, Any], query: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/whoami":
            auth = self._require_mailbox_auth()
            caller_harness_id = auth.harness_id or ""
            if auth.kind == "admin":
                return 200, self._with_caller({"ok": True}, auth)
            if auth.kind == "agent_session":
                return 200, self._with_caller(
                    {
                        "ok": True,
                        "session": {
                            "agent_session_id": auth.agent_session_id,
                            "project_id": auth.project_id,
                            "session_name": auth.session_name,
                            "created_at": auth.created_at,
                            "expires_at": auth.expires_at,
                            "expires_in_seconds": self._session_expiry_seconds(auth.expires_at),
                            "last_used_at": auth.last_used_at,
                            "allowed_addresses": list(auth.allowed_addresses),
                            "send_as_addresses": list(auth.send_as_addresses),
                            "claim_addresses": list(auth.claim_addresses),
                            "default_from_address": auth.default_from_address,
                            "default_claim_addresses": list(auth.default_claim_addresses),
                            "default_inbox_address": auth.default_inbox_address,
                        },
                    },
                    auth,
                )

            defaults = self.mailbox.get_harness_defaults(caller_harness_id)
            mailboxes = self.mailbox.list_mailboxes(harness_id=caller_harness_id)
            return 200, self._with_caller(
                {
                    "ok": True,
                    "defaults": defaults,
                    "mailboxes": [
                        {
                            "address": mailbox["address"],
                            "mailbox_type": mailbox["mailbox_type"],
                            "enabled": mailbox["enabled"],
                            "accept_messages": mailbox["accept_messages"],
                        }
                        for mailbox in mailboxes
                    ],
                },
                auth,
            )

        if method == "POST" and path == "/logout":
            token = self._extract_bearer_token("X-Mailbox-Token")
            if not token:
                raise AuthenticationError("missing mailbox token")
            auth = self._require_mailbox_auth()
            if auth.kind != "agent_session":
                raise PermissionError("logout is only supported for agent session tokens")
            session = self.mailbox.invalidate_agent_session_token(token)
            if session is None:
                raise AuthenticationError("invalid or expired agent session token")
            return 200, self._with_caller(
                {
                    "ok": True,
                    "logged_out": True,
                    "session": session,
                },
                auth,
            )

        if method == "POST" and path == "/login":
            auth = self._require_harness_auth()
            caller_harness_id = auth.harness_id or ""
            login_result = self.mailbox.create_agent_session_for_harness(
                caller_harness_id,
                _require(body, "project_id", str),
                role=_optional(body, "role", str),
                roles=_optional_str_list(body, "roles"),
                session_name=_optional(body, "session", str),
                agent_name=_optional(body, "agent_name", str),
                local_part=_optional(body, "local_part", str),
                mailbox_type=_optional(body, "mailbox_type", str),
                accept_messages=bool(body.get("accept_messages", True)),
                metadata=_optional_dict(body, "metadata"),
                expires_in_seconds=int(body.get("expires_in_seconds", 86_400)),
            )
            return 200, self._with_caller(
                {
                    "ok": True,
                    **login_result,
                    "login": login_result.get("session"),
                },
                auth,
            )

        auth = self._require_mailbox_auth()
        caller_harness_id = auth.harness_id or ""

        if method == "GET" and path == "/retry-queue":
            to_address, project_id, limit = self._retry_queue_query(query, auth)
            if auth.kind == "admin":
                deliveries = self.mailbox.list_retry_queue(
                    to_address=to_address,
                    project_id=project_id,
                    limit=limit,
                )
            elif auth.kind == "agent_session":
                deliveries = self.mailbox.list_retry_queue_for_addresses(
                    addresses=list(auth.claim_addresses),
                    to_address=to_address,
                    project_id=project_id,
                    limit=limit,
                )
            else:
                deliveries = self.mailbox.list_retry_queue_for_harness(
                    harness_id=caller_harness_id,
                    to_address=to_address,
                    project_id=project_id,
                    limit=limit,
                )
            return 200, self._with_caller({"ok": True, "deliveries": deliveries}, auth)

        if method == "GET" and path == "/message":
            message_id = (query.get("message_id") or [None])[0]
            if not message_id:
                raise ValueError("missing query parameter: message_id")
            if auth.kind == "admin":
                message = self.mailbox.get_message(message_id)
            elif auth.kind == "agent_session":
                message = self.mailbox.get_message_for_addresses(message_id, list(auth.allowed_addresses))
            else:
                message = self.mailbox.get_message_for_harness(message_id, caller_harness_id)
            if not message:
                if auth.kind != "admin" and self.mailbox.get_message(message_id):
                    visibility = "current agent session" if auth.kind == "agent_session" else f"caller harness {caller_harness_id}"
                    raise PermissionError(f"message {message_id} is not visible to {visibility}")
                return 404, {"ok": False, "error": "message not found"}
            return 200, self._with_caller({"ok": True, "message": message}, auth)

        if method == "GET" and path == "/thread":
            query_thread_id = (query.get("thread_id") or [None])[0]
            query_message_id = (query.get("message_id") or [None])[0]
            if not query_thread_id and not query_message_id:
                raise ValueError("missing query parameter: thread_id or message_id")
            if auth.kind == "admin":
                thread = self.mailbox.get_thread(
                    thread_id=query_thread_id,
                    message_id=query_message_id,
                )
            elif auth.kind == "agent_session":
                thread = self.mailbox.get_thread_for_addresses(
                    thread_id=query_thread_id,
                    message_id=query_message_id,
                    addresses=list(auth.allowed_addresses),
                )
            else:
                thread = self.mailbox.get_thread_for_harness(
                    thread_id=query_thread_id,
                    message_id=query_message_id,
                    harness_id=caller_harness_id,
                )
            if not thread:
                if auth.kind == "admin":
                    return 404, {"ok": False, "error": "thread not found"}
                existing_thread_id = query_thread_id
                if existing_thread_id is None and query_message_id:
                    existing_message = self.mailbox.get_message(query_message_id)
                    existing_thread_id = existing_message["thread_id"] if existing_message else None
                if existing_thread_id and self.mailbox.thread_exists(existing_thread_id):
                    visibility = "current agent session" if auth.kind == "agent_session" else f"caller harness {caller_harness_id}"
                    raise PermissionError(f"thread {existing_thread_id} is not visible to {visibility}")
                if query_message_id and self.mailbox.get_message(query_message_id):
                    visibility = "current agent session" if auth.kind == "agent_session" else f"caller harness {caller_harness_id}"
                    raise PermissionError(f"thread for message {query_message_id} is not visible to {visibility}")
                return 404, {"ok": False, "error": "thread not found"}
            return 200, self._with_caller({"ok": True, "thread": thread}, auth)

        if method == "GET" and path == "/thread-summaries":
            requested_address = (query.get("to_address") or [None])[0]
            limit_text = (query.get("limit") or ["20"])[0]
            limit = int(limit_text)
            mailbox = self._resolve_thread_state_mailbox(
                requested_address,
                auth,
                missing_error="missing query parameter: to_address",
                purpose="thread summaries",
            )
            if mailbox is None:
                return 200, self._with_caller({"ok": True, "threads": []}, auth)
            threads = self.mailbox.get_thread_summaries_for_mailbox(
                to_address=mailbox.address,
                limit=limit,
            )
            return 200, self._with_caller({"ok": True, "threads": threads}, auth)

        if method == "GET" and path == "/inbox":
            requested_address = (query.get("to_address") or [None])[0]
            limit_text = (query.get("limit") or ["20"])[0]
            limit = int(limit_text)
            message_type = (query.get("message_type") or [None])[0]
            since = (query.get("since") or [None])[0]
            mailbox = self._resolve_thread_state_mailbox(
                requested_address,
                auth,
                missing_error="missing query parameter: to_address",
                purpose="inbox listing",
            )
            if mailbox is None:
                return 200, self._with_caller({"ok": True, "messages": []}, auth)
            messages = self.mailbox.get_inbox_messages_for_mailbox(
                to_address=mailbox.address,
                limit=limit,
                message_type=message_type,
                since=since,
            )
            return 200, self._with_caller({"ok": True, "messages": messages}, auth)

        if method == "POST" and path == "/resolve":
            requested_address = _require(body, "address", str)
            if auth.kind == "admin":
                mailbox = self.mailbox.resolve_address(requested_address)
            elif auth.kind == "agent_session":
                canonical = self._normalize_session_address(requested_address, auth, purpose="resolve")
                mailbox = self.mailbox.resolve_address(canonical)
            else:
                mailbox = self.mailbox.resolve_address_for_harness(
                    requested_address,
                    caller_harness_id,
                )
            return 200, self._with_caller({"ok": True, "mailbox": self._mailbox_payload(mailbox)}, auth)

        if method == "POST" and path == "/send":
            payload = body.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")

            from_address = self._effective_from_address(body, auth)
            reply_to_address = _optional(body, "reply_to_address", str)
            if reply_to_address is not None:
                if auth.kind == "admin":
                    reply_to_address = self.mailbox.resolve_address(reply_to_address).address
                elif auth.kind == "agent_session":
                    self._normalize_session_address(reply_to_address, auth, purpose="reply_to")
                else:
                    self.mailbox.resolve_address_for_harness(reply_to_address, caller_harness_id)

            result = self.mailbox.send(
                from_address=from_address,
                to_address=_require(body, "to_address", str),
                payload=payload,
                subject=_optional(body, "subject", str),
                message_type=_optional(body, "message_type", str) or "generic",
                priority=int(body.get("priority", 0)),
                thread_id=_optional(body, "thread_id", str),
                in_reply_to_message_id=_optional(body, "in_reply_to_message_id", str),
                reply_to_address=reply_to_address,
                correlation_id=_optional(body, "correlation_id", str),
                workflow_id=_optional(body, "workflow_id", str),
                idempotency_key=_optional(body, "idempotency_key", str),
                headers=_optional_dict(body, "headers"),
                deliver_after_seconds=int(body.get("deliver_after_seconds", 0)),
                expires_in_seconds=_optional_int(body, "expires_in_seconds"),
                max_attempts=int(body.get("max_attempts", 8)),
                bypass_routing=auth.kind == "admin",
            )
            return 200, self._with_caller({"ok": True, **result}, auth)

        if method == "POST" and path == "/claim":
            result = self.mailbox.claim_any(
                to_addresses=self._effective_claim_addresses(body, auth),
                consumer_id=_require(body, "consumer_id", str),
                lease_seconds=int(body.get("lease_seconds", 60)),
            )
            return 200, self._with_caller({"ok": True, "delivery": result}, auth)

        if method == "POST" and path == "/ack":
            delivery_id = int(_require(body, "delivery_id", (int, str)))
            self._authorize_delivery(delivery_id, auth)
            ok = self.mailbox.ack(
                delivery_id=delivery_id,
                claim_token=_require(body, "claim_token", str),
                actor=_optional(body, "actor", str) or caller_harness_id,
            )
            return 200, self._with_caller({"ok": ok}, auth)

        if method == "POST" and path == "/nack":
            delivery_id = int(_require(body, "delivery_id", (int, str)))
            self._authorize_delivery(delivery_id, auth)
            ok = self.mailbox.nack(
                delivery_id=delivery_id,
                claim_token=_require(body, "claim_token", str),
                retry_after_seconds=int(body.get("retry_after_seconds", 30)),
                last_error=_optional(body, "last_error", str),
                actor=_optional(body, "actor", str) or caller_harness_id,
            )
            return 200, self._with_caller({"ok": ok}, auth)

        if method == "POST" and path == "/heartbeat":
            delivery_id = int(_require(body, "delivery_id", (int, str)))
            self._authorize_delivery(delivery_id, auth)
            ok = self.mailbox.heartbeat(
                delivery_id=delivery_id,
                claim_token=_require(body, "claim_token", str),
                lease_seconds=int(body.get("lease_seconds", 60)),
            )
            return 200, self._with_caller({"ok": ok}, auth)

        if method == "POST" and path == "/mark-thread-read":
            mailbox = self._resolve_thread_state_mailbox(
                _optional(body, "to_address", str),
                auth,
                missing_error="missing required field: to_address",
                purpose="thread read updates",
            )
            if mailbox is None:
                return 200, self._with_caller({"ok": False}, auth)
            ok = self.mailbox.mark_thread_read(
                thread_id=_require(body, "thread_id", str),
                to_address=mailbox.address,
                actor=_optional(body, "actor", str) or caller_harness_id or mailbox.address,
            )
            return 200, self._with_caller({"ok": ok}, auth)

        return 404, {"ok": False, "error": f"unknown route: {method} {path}"}

    def _dispatch(self, method: str, path: str, body: dict[str, Any], query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True, "service": "sqlite-mailbox-http"}

        if path.startswith("/admin/"):
            return self._dispatch_admin(method, path, body, query)

        return self._dispatch_harness(method, path, body, query)

    def _complete_admin_setup(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self._is_loopback_request():
            raise PermissionError("setup endpoint is only available from loopback")
        result = self.mailbox.create_initial_admin_account(
            token=_require(body, "token", str),
            username=_require(body, "username", str),
            password=_require(body, "password", str),
        )
        return 200, {"ok": True, **result}

    def _handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body() if method == "POST" else {}
            status, payload = self._dispatch(method, parsed.path, body, parse_qs(parsed.query))
        except ValueError as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
            return
        except AuthenticationError as exc:
            self._json_response(401, {"ok": False, "error": str(exc)})
            return
        except PermissionError as exc:
            self._json_response(403, {"ok": False, "error": str(exc)})
            return
        except Exception:
            traceback.print_exc()
            payload: dict[str, Any] = {"ok": False, "error": "internal server error"}
            if self.debug_mode:
                payload["detail"] = traceback.format_exc(limit=1).strip()
                payload["traceback"] = traceback.format_exc(limit=8)
            self._json_response(500, payload)
            return
        self._json_response(status, payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/admin-ui", "/admin-ui/"}:
            try:
                self._serve_admin_ui()
            except AuthenticationError as exc:
                self._json_response(401, {"ok": False, "error": str(exc)})
            except PermissionError as exc:
                self._json_response(403, {"ok": False, "error": str(exc)})
            return
        if parsed.path in {"/setup-admin", "/setup-admin/"}:
            setup_token = (parse_qs(parsed.query).get("token") or [None])[0]
            try:
                self._serve_setup_admin_page(setup_token)
            except AuthenticationError as exc:
                self._json_response(401, {"ok": False, "error": str(exc)})
            except PermissionError as exc:
                self._json_response(403, {"ok": False, "error": str(exc)})
            return
        self._handle_request("GET")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/setup-admin/complete":
            try:
                body = self._read_json_body()
                status, payload = self._complete_admin_setup(body)
            except ValueError as exc:
                self._json_response(400, {"ok": False, "error": str(exc)})
                return
            except AuthenticationError as exc:
                self._json_response(401, {"ok": False, "error": str(exc)})
                return
            except PermissionError as exc:
                self._json_response(403, {"ok": False, "error": str(exc)})
                return
            except Exception:
                traceback.print_exc()
                payload: dict[str, Any] = {"ok": False, "error": "internal server error"}
                if self.debug_mode:
                    payload["detail"] = traceback.format_exc(limit=1).strip()
                    payload["traceback"] = traceback.format_exc(limit=8)
                self._json_response(500, payload)
                return
            self._json_response(status, payload)
            return
        self._handle_request("POST")


def _require(body: dict[str, Any], key: str, expected_type: type | tuple[type, ...]) -> Any:
    if key not in body:
        raise ValueError(f"missing required field: {key}")
    value = body[key]
    if not isinstance(value, expected_type):
        typename = (
            ", ".join(t.__name__ for t in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        raise ValueError(f"field {key} must be of type {typename}")
    return value


def _optional(body: dict[str, Any], key: str, expected_type: type) -> Any | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, expected_type):
        raise ValueError(f"field {key} must be of type {expected_type.__name__}")
    return value


def _optional_dict(body: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"field {key} must be a JSON object")
    return value


def _optional_str_list(body: dict[str, Any], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"field {key} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"field {key} must contain only strings")
        stripped = item.strip()
        if not stripped:
            raise ValueError(f"field {key} must not contain empty strings")
        result.append(stripped)
    return result


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"field {key} must be an integer")
    return value


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = MailboxHTTPServer((host, port), MailboxRequestHandler, db_path)
    admin_mode = "env token and/or admin account"
    print(
        f"serving sqlite mailbox HTTP on http://{host}:{port} using db={Path(db_path).resolve()} "
        f"(admin={admin_mode}, debug={server.debug_mode})"
    )
    if not server.mailbox.has_admin_account():
        bootstrap = server.mailbox.create_admin_bootstrap_token()
        setup_url = f"http://127.0.0.1:{server.server_address[1]}/setup-admin?token={bootstrap['token']}"
        print(
            "no admin account found. open this one-time setup URL from the same machine:\n"
            f"  {setup_url}\n"
            f"setup token expires at {bootstrap['expires_at']}"
        )
    elif server.admin_token:
        print("admin token is configured via MAILBOX_ADMIN_TOKEN; /admin-ui also accepts Basic auth for admin accounts.")
    else:
        print("admin account is initialized; open http://127.0.0.1:%d/admin-ui from the same machine." % server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQLite mailbox HTTP server")
    parser.add_argument("--db", default="./mailbox_http.sqlite", help="path to sqlite db file")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=8787, help="bind port")
    args = parser.parse_args()
    serve(db_path=args.db, host=args.host, port=args.port)
