from __future__ import annotations

"""Thin standard-library mailbox HTTP client for Codex-style harness workers.

Expected environment variables:
    MAILBOX_TOKEN=<harness token>
or:
    MAILBOX_SESSION_TOKEN=<agent session token>

Optional environment variables:
    MAILBOX_BASE_URL=http://127.0.0.1:8787
    MAILBOX_FROM_ADDRESS=runner@project.codex
    MAILBOX_INBOX_ADDRESS=planner@project.codex
    MAILBOX_PROJECT_ID=mail4agent
    MAILBOX_ROLE=planner
    MAILBOX_ROLES=planner,reviewer
    MAILBOX_SESSION=main
    MAILBOX_AGENT_NAME=codex-main
    MAILBOX_LOCAL_PART=planner
    MAILBOX_MAILBOX_TYPE=role
    MAILBOX_CONSUMER_ID=codex-runner-001
    MAILBOX_TIMEOUT_SECONDS=10
    MAILBOX_CONFIG=./mailbox_client.json

Optional config file (`mailbox_client.json` by default, or `MAILBOX_CONFIG`):
    {
      "base_url": "http://127.0.0.1:8787",
      "from_address": "runner@project.codex",
      "inbox_address": "planner@project.codex",
      "project_id": "mail4agent",
      "role": "planner",
      "roles": ["planner", "reviewer"],
      "session": "main",
      "agent_name": "codex-main",
      "local_part": "planner",
      "mailbox_type": "role",
      "consumer_id": "codex-runner-001",
      "timeout_seconds": 10
    }

If `project_id + (role/roles/session/local_part)` are configured, the client will first
exchange the harness token for an agent session token via `/login`. After that, regular
mailbox calls automatically use the session token.
"""

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib import error, parse, request


DEFAULT_MAILBOX_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_MAILBOX_CONFIG = "mailbox_client.json"


class MailboxHTTPError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any] | None = None, body: str | None = None):
        self.status = status
        self.payload = payload or {}
        self.body = body
        detail = self.payload.get("error") if isinstance(self.payload, dict) else None
        message = detail or body or "mailbox request failed"
        super().__init__(f"mailbox http {status}: {message}")


@dataclass(frozen=True)
class MailboxClientConfig:
    base_url: str
    token: str
    from_address: str | None = None
    inbox_address: str | None = None
    project_id: str | None = None
    role: str | None = None
    roles: tuple[str, ...] = ()
    session: str | None = None
    agent_name: str | None = None
    local_part: str | None = None
    mailbox_type: str | None = None
    consumer_id: str | None = None
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, prefix: str = "MAILBOX") -> "MailboxClientConfig":
        file_config = _load_optional_config(prefix=prefix)
        base_url = os.environ.get(f"{prefix}_BASE_URL") or file_config.get("base_url") or DEFAULT_MAILBOX_BASE_URL
        token = _optional_env(f"{prefix}_SESSION_TOKEN") or _optional_env(f"{prefix}_TOKEN")
        if not token:
            raise ValueError(
                f"missing required environment variable: {prefix}_SESSION_TOKEN or {prefix}_TOKEN"
            )
        from_address = _optional_env(f"{prefix}_FROM_ADDRESS") or _optional_config_str(file_config, "from_address")
        inbox_address = _optional_env(f"{prefix}_INBOX_ADDRESS") or _optional_config_str(file_config, "inbox_address")
        project_id = _optional_env(f"{prefix}_PROJECT_ID") or _optional_config_str(file_config, "project_id")
        role = _optional_env(f"{prefix}_ROLE") or _optional_config_str(file_config, "role")
        roles = tuple(
            _merge_unique_strings(
                _optional_env_str_list(f"{prefix}_ROLES"),
                [role] if role else [],
                _optional_config_str_list(file_config, "roles"),
                [_optional_config_str(file_config, "role")] if _optional_config_str(file_config, "role") else [],
            )
        )
        session = _optional_env(f"{prefix}_SESSION") or _optional_config_str(file_config, "session")
        agent_name = _optional_env(f"{prefix}_AGENT_NAME") or _optional_config_str(file_config, "agent_name")
        local_part = _optional_env(f"{prefix}_LOCAL_PART") or _optional_config_str(file_config, "local_part")
        mailbox_type = _optional_env(f"{prefix}_MAILBOX_TYPE") or _optional_config_str(file_config, "mailbox_type")
        consumer_id = _optional_env(f"{prefix}_CONSUMER_ID") or _optional_config_str(file_config, "consumer_id")
        timeout_raw = os.environ.get(f"{prefix}_TIMEOUT_SECONDS")
        if timeout_raw is None:
            timeout_raw = str(file_config.get("timeout_seconds", "10"))
        timeout_seconds = float(timeout_raw)
        return cls(
            base_url=base_url.rstrip("/"),
            token=token,
            from_address=from_address,
            inbox_address=inbox_address,
            project_id=project_id,
            role=role,
            roles=roles,
            session=session,
            agent_name=agent_name,
            local_part=local_part,
            mailbox_type=mailbox_type,
            consumer_id=consumer_id,
            timeout_seconds=timeout_seconds,
        )


class MailboxHTTPClient:
    def __init__(self, config: MailboxClientConfig):
        self.config = config
        self.default_consumer_id = config.consumer_id or _default_consumer_id()
        self._whoami_cache: dict[str, Any] | None = None
        self._login_cache: dict[str, Any] | None = None
        self._session_token: str | None = None
        self._session_profile: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, prefix: str = "MAILBOX") -> "MailboxHTTPClient":
        return cls(MailboxClientConfig.from_env(prefix=prefix))

    def healthz(self) -> dict[str, Any]:
        return self._request_json("GET", "/healthz")

    def whoami(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._whoami_cache is None:
            self._whoami_cache = self._request_json("GET", "/whoami")
        return self._whoami_cache

    def login(
        self,
        *,
        refresh: bool = False,
        project_id: str | None = None,
        role: str | None = None,
        roles: list[str] | tuple[str, ...] | None = None,
        session: str | None = None,
        agent_name: str | None = None,
        local_part: str | None = None,
        mailbox_type: str | None = None,
        accept_messages: bool = True,
        metadata: dict[str, Any] | None = None,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        effective_project_id = project_id or self.config.project_id
        configured_roles = list(self.config.roles)
        effective_roles = _merge_unique_strings(
            list(roles or []),
            [role] if role else [],
            configured_roles,
        )
        effective_session = session or self.config.session
        effective_agent_name = agent_name or self.config.agent_name
        effective_local_part = local_part or self.config.local_part
        effective_mailbox_type = mailbox_type or self.config.mailbox_type
        if not effective_project_id:
            raise ValueError("project_id is required unless MAILBOX_PROJECT_ID is configured")
        if not effective_roles and not effective_session and not effective_local_part:
            raise ValueError("login requires role/roles/session/local_part unless configured")

        cache_key = {
            "project_id": effective_project_id,
            "roles": tuple(effective_roles),
            "session": effective_session,
            "agent_name": effective_agent_name,
            "local_part": effective_local_part,
            "mailbox_type": effective_mailbox_type,
            "accept_messages": accept_messages,
            "expires_in_seconds": expires_in_seconds,
        }
        if refresh or self._login_cache is None or self._login_cache.get("_cache_key") != cache_key:
            body: dict[str, Any] = {
                "project_id": effective_project_id,
                "accept_messages": accept_messages,
            }
            if effective_roles:
                if len(effective_roles) == 1:
                    body["role"] = effective_roles[0]
                body["roles"] = effective_roles
            if effective_session is not None:
                body["session"] = effective_session
            if effective_agent_name is not None:
                body["agent_name"] = effective_agent_name
            if effective_local_part is not None:
                body["local_part"] = effective_local_part
            if effective_mailbox_type is not None:
                body["mailbox_type"] = effective_mailbox_type
            if metadata is not None:
                body["metadata"] = metadata
            if expires_in_seconds is not None:
                body["expires_in_seconds"] = expires_in_seconds
            payload = self._request_json("POST", "/login", body, use_bootstrap_token=True)
            session_token = payload.get("session_token")
            session_payload = payload.get("session")
            if isinstance(session_token, str) and session_token:
                self._session_token = session_token
            if isinstance(session_payload, dict):
                self._session_profile = session_payload
            self._whoami_cache = None
            self._login_cache = {
                **payload,
                "_cache_key": cache_key,
            }
        return {
            key: value
            for key, value in self._login_cache.items()
            if key != "_cache_key"
        }

    def logout(self) -> dict[str, Any]:
        payload = self._request_json("POST", "/logout")
        self._whoami_cache = None
        self._login_cache = None
        self._session_profile = None
        self._session_token = None
        return payload

    def resolve(self, address: str) -> dict[str, Any]:
        payload = self._request_json("POST", "/resolve", {"address": address})
        return payload["mailbox"]

    def get_message(self, message_id: str, *, allow_missing: bool = False) -> dict[str, Any] | None:
        try:
            payload = self._request_json("GET", "/message", query={"message_id": message_id})
        except MailboxHTTPError as exc:
            if allow_missing and exc.status == 404:
                return None
            raise
        return payload["message"]

    def get_thread(
        self,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        if thread_id is None and message_id is None:
            raise ValueError("thread_id or message_id is required")
        query: dict[str, str] = {}
        if thread_id is not None:
            query["thread_id"] = thread_id
        if message_id is not None:
            query["message_id"] = message_id
        try:
            payload = self._request_json("GET", "/thread", query=query)
        except MailboxHTTPError as exc:
            if allow_missing and exc.status == 404:
                return None
            raise
        return payload["thread"]

    def get_thread_summaries(
        self,
        *,
        to_address: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        effective_to_address = to_address or self._effective_inbox_address()
        if not effective_to_address and self._current_session_profile() is None:
            raise ValueError(
                "to_address is required unless configured locally or provided by an agent session login"
            )
        query = {"limit": str(limit)}
        if effective_to_address:
            query["to_address"] = effective_to_address
        return self._request_json(
            "GET",
            "/thread-summaries",
            query=query,
        )

    def get_inbox(
        self,
        *,
        to_address: str | None = None,
        limit: int = 20,
        message_type: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        effective_to_address = to_address or self._effective_inbox_address()
        if not effective_to_address and self._current_session_profile() is None:
            raise ValueError(
                "to_address is required unless configured locally or provided by an agent session login"
            )
        query = {"limit": str(limit)}
        if effective_to_address:
            query["to_address"] = effective_to_address
        if message_type is not None:
            query["message_type"] = message_type
        if thread_id is not None:
            query["thread_id"] = thread_id
        if since is not None:
            query["since"] = since
        if unread_only:
            query["unread_only"] = "true"
        return self._request_json(
            "GET",
            "/inbox",
            query=query,
        )

    def mark_thread_read(
        self,
        *,
        thread_id: str,
        to_address: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        effective_to_address = to_address or self._effective_inbox_address()
        if not effective_to_address and self._current_session_profile() is None:
            raise ValueError(
                "to_address is required unless configured locally or provided by an agent session login"
            )
        body = {
            "thread_id": thread_id,
            "actor": actor or self.default_consumer_id,
        }
        if effective_to_address:
            body["to_address"] = effective_to_address
        return self._request_json("POST", "/mark-thread-read", body)

    def retry_queue(
        self,
        *,
        to_address: str | None = None,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        query: dict[str, str] = {}
        if to_address is not None:
            query["to_address"] = to_address
        if project_id is not None:
            query["project_id"] = project_id
        if limit is not None:
            query["limit"] = str(limit)
        return self._request_json("GET", "/retry-queue", query=query or None)

    def send(
        self,
        *,
        to_address: str,
        payload: dict[str, Any],
        from_address: str | None = None,
        subject: str | None = None,
        message_type: str = "generic",
        priority: int = 0,
        thread_id: str | None = None,
        in_reply_to_message_id: str | None = None,
        reply_to_address: str | None = None,
        correlation_id: str | None = None,
        workflow_id: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, Any] | None = None,
        deliver_after_seconds: int = 0,
        expires_in_seconds: int | None = None,
        max_attempts: int = 8,
    ) -> dict[str, Any]:
        effective_from = from_address or self._effective_from_address()
        body: dict[str, Any] = {
            "to_address": to_address,
            "payload": payload,
            "message_type": message_type,
            "priority": priority,
            "deliver_after_seconds": deliver_after_seconds,
            "max_attempts": max_attempts,
        }
        if effective_from is not None:
            body["from_address"] = effective_from
        elif self._current_session_profile() is None:
            raise ValueError(
                "from_address is required unless configured locally or provided by an agent session login"
            )
        if subject is not None:
            body["subject"] = subject
        if thread_id is not None:
            body["thread_id"] = thread_id
        if in_reply_to_message_id is not None:
            body["in_reply_to_message_id"] = in_reply_to_message_id
        if reply_to_address is not None:
            body["reply_to_address"] = reply_to_address
        if correlation_id is not None:
            body["correlation_id"] = correlation_id
        if workflow_id is not None:
            body["workflow_id"] = workflow_id
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        if headers is not None:
            body["headers"] = headers
        if expires_in_seconds is not None:
            body["expires_in_seconds"] = expires_in_seconds
        return self._request_json("POST", "/send", body)

    def send_reply(
        self,
        delivery: dict[str, Any],
        *,
        payload: dict[str, Any],
        from_address: str | None = None,
        subject: str | None = None,
        message_type: str = "generic",
        headers: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        to_address = delivery.get("from")
        if not to_address:
            raise ValueError("delivery is missing sender address")
        original_subject = delivery.get("subject")
        reply_subject = subject if subject is not None else _reply_subject(original_subject)
        effective_from = from_address or delivery.get("to") or self.config.from_address
        return self.send(
            from_address=effective_from,
            to_address=to_address,
            payload=payload,
            subject=reply_subject,
            message_type=message_type,
            thread_id=delivery.get("thread_id"),
            in_reply_to_message_id=delivery.get("message_id"),
            correlation_id=delivery.get("correlation_id"),
            workflow_id=delivery.get("workflow_id"),
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def claim(
        self,
        *,
        to_address: str | None = None,
        to_addresses: list[str] | tuple[str, ...] | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        serialization_scope: str = "mailbox_thread",
    ) -> dict[str, Any] | None:
        effective_consumer = consumer_id or self.default_consumer_id
        body: dict[str, Any] = {
            "consumer_id": effective_consumer,
            "lease_seconds": lease_seconds,
            "serialization_scope": serialization_scope,
        }
        if to_address is not None and to_addresses is not None:
            raise ValueError("provide either to_address or to_addresses, not both")
        if to_addresses is not None:
            if not to_addresses:
                raise ValueError("to_addresses must not be empty")
            body["to_addresses"] = list(to_addresses)
        elif to_address is not None:
            body["to_address"] = to_address
        else:
            effective_claims = self._effective_claim_addresses()
            if len(effective_claims) == 1:
                body["to_address"] = effective_claims[0]
            elif len(effective_claims) > 1:
                body["to_addresses"] = effective_claims
            elif self._current_session_profile() is None:
                raise ValueError(
                    "to_address is required unless configured locally or provided by an agent session login"
                )
        payload = self._request_json(
            "POST",
            "/claim",
            body,
        )
        return payload["delivery"]

    def ack(self, *, delivery_id: int, claim_token: str, actor: str | None = None) -> bool:
        payload = self._request_json(
            "POST",
            "/ack",
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "actor": actor or self.default_consumer_id,
            },
        )
        return bool(payload["ok"])

    def nack(
        self,
        *,
        delivery_id: int,
        claim_token: str,
        retry_after_seconds: int = 30,
        last_error: str | None = None,
        actor: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {
            "delivery_id": delivery_id,
            "claim_token": claim_token,
            "retry_after_seconds": retry_after_seconds,
            "actor": actor or self.default_consumer_id,
        }
        if last_error is not None:
            body["last_error"] = last_error
        payload = self._request_json("POST", "/nack", body)
        return bool(payload["ok"])

    def _effective_from_address(self) -> str | None:
        if self.config.from_address:
            return self.config.from_address
        session_profile = self._current_session_profile()
        if session_profile and session_profile.get("default_from_address"):
            return str(session_profile["default_from_address"])
        defaults = self._discovered_defaults()
        if defaults.get("default_from_address"):
            return str(defaults["default_from_address"])
        return self._guess_mailbox_address(kind="from")

    def _effective_inbox_address(self) -> str | None:
        if self.config.inbox_address:
            return self.config.inbox_address
        session_profile = self._current_session_profile()
        if session_profile:
            default_inbox_address = session_profile.get("default_inbox_address")
            if isinstance(default_inbox_address, str) and default_inbox_address.strip():
                return default_inbox_address
            default_claims = session_profile.get("default_claim_addresses")
            if isinstance(default_claims, list) and len(default_claims) == 1:
                return str(default_claims[0])
        defaults = self._discovered_defaults()
        if defaults.get("default_inbox_address"):
            return str(defaults["default_inbox_address"])
        return self._guess_mailbox_address(kind="inbox")

    def _effective_claim_addresses(self) -> list[str]:
        if self.config.inbox_address:
            return [self.config.inbox_address]
        session_profile = self._current_session_profile()
        if session_profile:
            default_claims = session_profile.get("default_claim_addresses")
            if isinstance(default_claims, list) and default_claims:
                return [str(item) for item in default_claims if isinstance(item, str)]
            claim_addresses = session_profile.get("claim_addresses")
            if isinstance(claim_addresses, list) and claim_addresses:
                return [str(item) for item in claim_addresses if isinstance(item, str)]
        effective_inbox = self._effective_inbox_address()
        return [effective_inbox] if effective_inbox else []

    def _configured_login_identity(self) -> bool:
        return bool(
            self.config.project_id
            and (self.config.roles or self.config.role or self.config.session or self.config.local_part)
        )

    def _current_session_profile(self) -> dict[str, Any] | None:
        if self._session_profile is not None:
            return self._session_profile
        whoami_payload = self.whoami()
        whoami_session = whoami_payload.get("session")
        if isinstance(whoami_session, dict):
            self._session_profile = whoami_session
            if self._session_token is None:
                self._session_token = self.config.token
            return whoami_session
        if not self._configured_login_identity():
            return None
        payload = self.login()
        session_payload = payload.get("session")
        if isinstance(session_payload, dict):
            self._session_profile = session_payload
            return session_payload
        return None

    def _discovered_defaults(self) -> dict[str, Any]:
        payload = self.whoami()
        defaults = payload.get("defaults")
        if isinstance(defaults, dict):
            return defaults
        return {}

    def _guess_mailbox_address(self, *, kind: str) -> str | None:
        payload = self.whoami()
        mailboxes = payload.get("mailboxes")
        if not isinstance(mailboxes, list):
            return None

        enabled_mailboxes = [
            mailbox
            for mailbox in mailboxes
            if isinstance(mailbox, dict)
            and mailbox.get("enabled") is True
            and mailbox.get("address")
        ]
        if len(enabled_mailboxes) == 1:
            return str(enabled_mailboxes[0]["address"])

        preferred_locals = (
            ["runner", "sender", "agent", "session"]
            if kind == "from"
            else ["planner", "inbox", "agent", "session"]
        )
        for local_part in preferred_locals:
            for mailbox in enabled_mailboxes:
                address = str(mailbox["address"])
                mailbox_local = address.split("@", 1)[0].strip().lower()
                if mailbox_local == local_part:
                    return address
        return None

    def heartbeat(self, *, delivery_id: int, claim_token: str, lease_seconds: int = 60) -> bool:
        payload = self._request_json(
            "POST",
            "/heartbeat",
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "lease_seconds": lease_seconds,
            },
        )
        return bool(payload["ok"])

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
        use_bootstrap_token: bool = False,
    ) -> dict[str, Any]:
        url = self._build_url(path, query=query)
        auth_token = self._select_request_token(path=path, use_bootstrap_token=use_bootstrap_token)
        try:
            return self._perform_request(method, url, auth_token, body)
        except error.HTTPError as exc:
            if (
                exc.code == 401
                and path != "/logout"
                and not use_bootstrap_token
                and auth_token == self._session_token
                and self._configured_login_identity()
            ):
                self._session_token = None
                self._session_profile = None
                self._login_cache = None
                refreshed_token = self._select_request_token(path=path, use_bootstrap_token=False)
                if refreshed_token != auth_token:
                    return self._perform_request(method, url, refreshed_token, body)
            payload, raw_text = self._decode_error_response(exc)
            raise MailboxHTTPError(exc.code, payload=payload, body=raw_text) from exc

    def _select_request_token(self, *, path: str, use_bootstrap_token: bool) -> str:
        if use_bootstrap_token:
            return self.config.token
        if self._session_token:
            return self._session_token
        if path not in {"/healthz", "/whoami", "/login", "/logout"} and self._configured_login_identity():
            self._current_session_profile()
            if self._session_token:
                return self._session_token
        return self.config.token

    def _perform_request(
        self,
        method: str,
        url: str,
        auth_token: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "X-Mailbox-Token": auth_token,
        }
        raw_body: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=raw_body, headers=headers, method=method)
        with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
            return self._decode_json_response(response)

    def _build_url(self, path: str, *, query: dict[str, str] | None = None) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.config.base_url}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        return url

    def _decode_json_response(self, response: Any) -> dict[str, Any]:
        raw = response.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"mailbox server returned invalid JSON: {raw[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("mailbox server returned non-object JSON")
        return payload

    def _decode_error_response(self, exc: error.HTTPError) -> tuple[dict[str, Any] | None, str | None]:
        raw = exc.read().decode("utf-8", errors="replace")
        if not raw:
            return None, None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, raw
        if isinstance(payload, dict):
            return payload, raw
        return None, raw


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _optional_env_str_list(name: str) -> list[str]:
    value = _optional_env(name)
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_optional_config(prefix: str) -> dict[str, Any]:
    config_path = os.environ.get(f"{prefix}_CONFIG")
    path = Path(config_path) if config_path else Path.cwd() / DEFAULT_MAILBOX_CONFIG
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"mailbox config file must contain a JSON object: {path}")
    return data


def _optional_config_str(config: dict[str, Any], key: str) -> Optional[str]:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"mailbox config field {key} must be a string")
    value = value.strip()
    return value or None


def _optional_config_str_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise ValueError(f"mailbox config field {key} must be a string or list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"mailbox config field {key} must contain only strings")
        stripped = item.strip()
        if stripped:
            result.append(stripped)
    return result


def _merge_unique_strings(*groups: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, str):
                continue
            stripped = item.strip()
            if not stripped:
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            result.append(stripped)
    return result


def _default_consumer_id() -> str:
    return f"{socket.gethostname().lower()}-{os.getpid()}"


def _reply_subject(subject: Any) -> str | None:
    if not isinstance(subject, str) or not subject.strip():
        return None
    stripped = subject.strip()
    if stripped.lower().startswith("re:"):
        return stripped
    return f"re: {stripped}"
