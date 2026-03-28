from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ADDRESS_RE = re.compile(
    r"^(?P<local>[A-Za-z0-9._-]+)@(?P<project>[A-Za-z0-9._-]+)\.(?P<harness>[A-Za-z0-9._-]+)$"
)
ADDRESS_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CLAIM_SERIALIZATION_SCOPES = frozenset({"delivery", "mailbox_thread"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")



def utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))



def canonicalize_address(address: str) -> str:
    match = ADDRESS_RE.match(address.strip())
    if not match:
        raise ValueError(
            "invalid address; expected local_part@project_id.harness_id using [A-Za-z0-9._-]"
        )
    local = match.group("local").lower()
    project = match.group("project").lower()
    harness = match.group("harness").lower()
    return f"{local}@{project}.{harness}"


def normalize_claim_serialization_scope(serialization_scope: str | None) -> str:
    normalized = str(serialization_scope or "mailbox_thread").strip().lower()
    if normalized not in CLAIM_SERIALIZATION_SCOPES:
        raise ValueError("serialization_scope must be one of: delivery, mailbox_thread")
    return normalized



def split_address(address: str) -> tuple[str, str, str]:
    canonical = canonicalize_address(address)
    local, domain = canonical.split("@", 1)
    project, harness = domain.split(".", 1)
    return local, project, harness


def normalize_harness_id(harness_id: str) -> str:
    return harness_id.strip().lower()


def normalize_address_component(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if not ADDRESS_COMPONENT_RE.match(normalized):
        raise ValueError(f"{field_name} must use only [A-Za-z0-9._-]")
    return normalized


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def resolve_default_inbox_address(
    *,
    claim_addresses: list[str],
    default_claim_addresses: list[str],
    role_claim_addresses: list[str],
    default_role_claim_addresses: list[str],
) -> str | None:
    for candidates in (
        default_role_claim_addresses,
        default_claim_addresses,
        role_claim_addresses,
        claim_addresses,
    ):
        unique_candidates = unique_preserving_order(candidates)
        if unique_candidates:
            return unique_candidates[0]
    return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, iterations: int = 240_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_hex, digest_hex = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()
    return hmac.compare_digest(candidate, digest_hex)


_UNSET = object()
RETRY_QUEUE_ERROR_SUMMARY_LIMIT = 120


def summarize_retry_error(last_error: str | None, *, limit: int = RETRY_QUEUE_ERROR_SUMMARY_LIMIT) -> str:
    if not last_error:
        return ""
    normalized = " ".join(last_error.split())
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


@dataclass(frozen=True)
class MailboxRef:
    mailbox_id: int
    address: str
    local_part: str
    project_id: str
    harness_id: str
    mailbox_type: str


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS harnesses (
    harness_id          TEXT PRIMARY KEY,
    display_name        TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS projects (
    project_pk          INTEGER PRIMARY KEY,
    project_id          TEXT NOT NULL,
    harness_id          TEXT NOT NULL,
    display_name        TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(project_id, harness_id),
    FOREIGN KEY (harness_id) REFERENCES harnesses(harness_id)
);

CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id              INTEGER PRIMARY KEY,
    local_part              TEXT NOT NULL,
    project_pk              INTEGER NOT NULL,
    harness_id              TEXT NOT NULL,
    mailbox_type            TEXT NOT NULL CHECK (mailbox_type IN ('session', 'role', 'group')),
    address_canonical       TEXT NOT NULL UNIQUE,
    enabled                 INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    accept_messages         INTEGER NOT NULL DEFAULT 1 CHECK (accept_messages IN (0, 1)),
    metadata_json           TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (project_pk) REFERENCES projects(project_pk),
    FOREIGN KEY (harness_id) REFERENCES harnesses(harness_id),
    UNIQUE(local_part, project_pk)
);

CREATE TABLE IF NOT EXISTS harness_tokens (
    token_id             INTEGER PRIMARY KEY,
    harness_id           TEXT NOT NULL,
    token_name           TEXT,
    token_hash           TEXT NOT NULL UNIQUE,
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at           TEXT NOT NULL,
    last_used_at         TEXT,
    FOREIGN KEY (harness_id) REFERENCES harnesses(harness_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_tokens_harness
ON harness_tokens(harness_id, enabled, token_id DESC);

CREATE INDEX IF NOT EXISTS idx_harness_tokens_enabled
ON harness_tokens(enabled, token_id);

CREATE TABLE IF NOT EXISTS harness_client_defaults (
    harness_id             TEXT PRIMARY KEY,
    default_from_address   TEXT,
    default_inbox_address  TEXT,
    updated_at             TEXT NOT NULL,
    FOREIGN KEY (harness_id) REFERENCES harnesses(harness_id)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    agent_session_id       INTEGER PRIMARY KEY,
    harness_id             TEXT NOT NULL,
    project_id             TEXT NOT NULL,
    agent_name             TEXT,
    session_name           TEXT,
    token_hash             TEXT NOT NULL UNIQUE,
    enabled                INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    metadata_json          TEXT,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    last_used_at           TEXT,
    FOREIGN KEY (harness_id) REFERENCES harnesses(harness_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_harness
ON agent_sessions(harness_id, enabled, expires_at, agent_session_id DESC);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_enabled
ON agent_sessions(enabled, expires_at, agent_session_id);

CREATE TABLE IF NOT EXISTS agent_session_mailboxes (
    agent_session_mailbox_id INTEGER PRIMARY KEY,
    agent_session_id         INTEGER NOT NULL,
    mailbox_id               INTEGER NOT NULL,
    can_send                 INTEGER NOT NULL DEFAULT 1 CHECK (can_send IN (0, 1)),
    can_claim                INTEGER NOT NULL DEFAULT 1 CHECK (can_claim IN (0, 1)),
    is_default_from          INTEGER NOT NULL DEFAULT 0 CHECK (is_default_from IN (0, 1)),
    is_default_claim         INTEGER NOT NULL DEFAULT 0 CHECK (is_default_claim IN (0, 1)),
    created_at               TEXT NOT NULL,
    FOREIGN KEY (agent_session_id) REFERENCES agent_sessions(agent_session_id) ON DELETE CASCADE,
    FOREIGN KEY (mailbox_id) REFERENCES mailboxes(mailbox_id),
    UNIQUE(agent_session_id, mailbox_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_session_mailboxes_session
ON agent_session_mailboxes(agent_session_id, can_send, can_claim, agent_session_mailbox_id);

CREATE INDEX IF NOT EXISTS idx_agent_session_mailboxes_mailbox
ON agent_session_mailboxes(mailbox_id, agent_session_id);

CREATE TABLE IF NOT EXISTS admin_accounts (
    admin_id              INTEGER PRIMARY KEY,
    username              TEXT NOT NULL UNIQUE,
    password_hash         TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at            TEXT NOT NULL,
    last_login_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_accounts_enabled
ON admin_accounts(enabled, username);

CREATE TABLE IF NOT EXISTS admin_bootstrap_tokens (
    bootstrap_token_id    INTEGER PRIMARY KEY,
    token_hash            TEXT NOT NULL UNIQUE,
    created_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    used_at               TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_bootstrap_active
ON admin_bootstrap_tokens(used_at, expires_at, bootstrap_token_id);

CREATE TABLE IF NOT EXISTS mailbox_bindings (
    binding_id               INTEGER PRIMARY KEY,
    mailbox_id               INTEGER NOT NULL,
    session_id               TEXT,
    run_id                   TEXT,
    consumer_id              TEXT,
    bind_mode                TEXT NOT NULL DEFAULT 'exclusive' CHECK (bind_mode IN ('exclusive', 'shared')),
    active                   INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_heartbeat_at        TEXT,
    lease_until              TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (mailbox_id) REFERENCES mailboxes(mailbox_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_bindings_one_active_exclusive
ON mailbox_bindings(mailbox_id)
WHERE active = 1 AND bind_mode = 'exclusive';

CREATE INDEX IF NOT EXISTS idx_mailbox_bindings_mailbox_active
ON mailbox_bindings(mailbox_id, active);

CREATE TABLE IF NOT EXISTS routing_policies (
    policy_id                INTEGER PRIMARY KEY,
    from_harness_id          TEXT,
    from_project_id          TEXT,
    from_mailbox_type        TEXT CHECK (from_mailbox_type IN ('session', 'role', 'group')),
    to_harness_id            TEXT,
    to_project_id            TEXT,
    to_mailbox_type          TEXT CHECK (to_mailbox_type IN ('session', 'role', 'group')),
    effect                   TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    priority                 INTEGER NOT NULL DEFAULT 0,
    enabled                  INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    description              TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_routing_policies_match
ON routing_policies(
    enabled,
    priority DESC,
    from_harness_id,
    from_project_id,
    to_harness_id,
    to_project_id
);

CREATE TABLE IF NOT EXISTS messages (
    message_id               TEXT PRIMARY KEY,
    from_mailbox_id          INTEGER NOT NULL,
    reply_to_mailbox_id      INTEGER,
    thread_id                TEXT NOT NULL,
    in_reply_to_message_id   TEXT,
    correlation_id           TEXT,
    workflow_id              TEXT,
    subject                  TEXT,
    message_type             TEXT NOT NULL DEFAULT 'generic',
    priority                 INTEGER NOT NULL DEFAULT 0,
    idempotency_key          TEXT,
    payload_json             TEXT NOT NULL,
    headers_json             TEXT,
    deliver_after            TEXT,
    expires_at               TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (from_mailbox_id) REFERENCES mailboxes(mailbox_id),
    FOREIGN KEY (reply_to_mailbox_id) REFERENCES mailboxes(mailbox_id),
    FOREIGN KEY (in_reply_to_message_id) REFERENCES messages(message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency
ON messages(from_mailbox_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_thread
ON messages(thread_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_correlation
ON messages(correlation_id);

CREATE INDEX IF NOT EXISTS idx_messages_workflow
ON messages(workflow_id);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id              INTEGER PRIMARY KEY,
    message_id               TEXT NOT NULL,
    to_mailbox_id            INTEGER NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN ('queued', 'claimed', 'acked', 'dead', 'expired')),
    consumer_id              TEXT,
    claim_token              TEXT,
    claimed_at               TEXT,
    lease_until              TEXT,
    acked_at                 TEXT,
    dead_at                  TEXT,
    expires_at               TEXT,
    attempt_count            INTEGER NOT NULL DEFAULT 0,
    max_attempts             INTEGER NOT NULL DEFAULT 8,
    last_error               TEXT,
    available_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (to_mailbox_id) REFERENCES mailboxes(mailbox_id),
    UNIQUE(message_id, to_mailbox_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_claim
ON deliveries(
    to_mailbox_id,
    status,
    available_at,
    lease_until,
    created_at
);

CREATE INDEX IF NOT EXISTS idx_deliveries_consumer
ON deliveries(consumer_id, status, lease_until);

CREATE INDEX IF NOT EXISTS idx_deliveries_message
ON deliveries(message_id);

CREATE TABLE IF NOT EXISTS mailbox_thread_reads (
    mailbox_thread_read_id    INTEGER PRIMARY KEY,
    mailbox_id                INTEGER NOT NULL,
    thread_id                 TEXT NOT NULL,
    last_read_message_id      TEXT NOT NULL,
    marked_read_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (mailbox_id) REFERENCES mailboxes(mailbox_id) ON DELETE CASCADE,
    FOREIGN KEY (last_read_message_id) REFERENCES messages(message_id),
    UNIQUE(mailbox_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_mailbox_thread_reads_mailbox
ON mailbox_thread_reads(mailbox_id, thread_id);

CREATE TABLE IF NOT EXISTS mailbox_events (
    event_id                 INTEGER PRIMARY KEY,
    event_type               TEXT NOT NULL,
    message_id               TEXT,
    delivery_id              INTEGER,
    mailbox_id               INTEGER,
    actor                    TEXT,
    details_json             TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_mailbox_events_message
ON mailbox_events(message_id, created_at);

CREATE INDEX IF NOT EXISTS idx_mailbox_events_delivery
ON mailbox_events(delivery_id, created_at);
"""


class SQLiteMailbox:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._agent_session_lock = threading.RLock()
        self._agent_sessions_by_token: dict[str, dict[str, Any]] = {}
        self._agent_session_tokens_by_identity: dict[tuple[Any, ...], str] = {}
        self._next_agent_session_id = 1

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def _event(self, conn: sqlite3.Connection, event_type: str, **kwargs: Any) -> None:
        conn.execute(
            """
            INSERT INTO mailbox_events(event_type, message_id, delivery_id, mailbox_id, actor, details_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                kwargs.get("message_id"),
                kwargs.get("delivery_id"),
                kwargs.get("mailbox_id"),
                kwargs.get("actor"),
                json.dumps(kwargs.get("details"), ensure_ascii=False) if "details" in kwargs else None,
            ),
        )

    def _load_message(self, conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT
                m.message_id,
                m.thread_id,
                m.in_reply_to_message_id,
                m.correlation_id,
                m.workflow_id,
                m.subject,
                m.message_type,
                m.priority,
                m.payload_json,
                m.headers_json,
                m.created_at,
                src.address_canonical AS from_address
            FROM messages m
            JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
            WHERE m.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if not row:
            return None

        recipients = conn.execute(
            """
            SELECT dst.address_canonical AS to_address
            FROM deliveries d
            JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
            WHERE d.message_id = ?
            ORDER BY d.delivery_id ASC
            """,
            (message_id,),
        ).fetchall()
        return {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "in_reply_to_message_id": row["in_reply_to_message_id"],
            "correlation_id": row["correlation_id"],
            "workflow_id": row["workflow_id"],
            "subject": row["subject"],
            "message_type": row["message_type"],
            "priority": row["priority"],
            "payload": json.loads(row["payload_json"]),
            "headers": json.loads(row["headers_json"]) if row["headers_json"] else None,
            "created_at": row["created_at"],
            "from": row["from_address"],
            "to": [recipient["to_address"] for recipient in recipients],
        }

    def _resolve_thread_id(
        self,
        conn: sqlite3.Connection,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> str | None:
        if thread_id is not None:
            normalized_thread_id = thread_id.strip()
            if not normalized_thread_id:
                raise ValueError("thread_id must not be empty")
            return normalized_thread_id
        if message_id is None:
            return None
        row = conn.execute(
            """
            SELECT thread_id
            FROM messages
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
        return str(row["thread_id"]) if row else None

    def _load_thread_messages(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        *,
        visible_message_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if visible_message_ids is None:
            rows = conn.execute(
                """
                SELECT message_id
                FROM messages
                WHERE thread_id = ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (thread_id,),
            ).fetchall()
        else:
            if not visible_message_ids:
                return []
            placeholders = ", ".join("?" for _ in visible_message_ids)
            rows = conn.execute(
                f"""
                SELECT message_id
                FROM messages
                WHERE thread_id = ?
                  AND message_id IN ({placeholders})
                ORDER BY created_at ASC, message_id ASC
                """,
                (thread_id, *visible_message_ids),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = self._load_message(conn, str(row["message_id"]))
            if message is not None:
                messages.append(message)
        return messages

    def upsert_harness(self, harness_id: str, display_name: Optional[str] = None, enabled: bool = True) -> None:
        harness_id = normalize_harness_id(harness_id)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO harnesses(harness_id, display_name, enabled, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(harness_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = excluded.enabled
                """,
                (harness_id, display_name, int(enabled), now),
            )

    def create_harness_token(self, harness_id: str, token_name: Optional[str] = None) -> dict[str, Any]:
        harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM harnesses WHERE harness_id = ? AND enabled = 1",
                (harness_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"harness not found or disabled: {harness_id}")

            for _ in range(4):
                token = secrets.token_urlsafe(32)
                token_hash = hash_token(token)
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO harness_tokens(harness_id, token_name, token_hash, enabled, created_at)
                        VALUES (?, ?, ?, 1, ?)
                        """,
                        (harness_id, token_name, token_hash, utc_now()),
                    )
                except sqlite3.IntegrityError:
                    continue
                return {"token_id": int(cursor.lastrowid), "token": token}

        raise RuntimeError("failed to create unique harness token")

    def disable_harness_token(self, token_id: int) -> bool:
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE harness_tokens
                SET enabled = 0
                WHERE token_id = ? AND enabled = 1
                """,
                (token_id,),
            ).rowcount
            return updated == 1

    def list_harness_tokens(self, harness_id: str) -> list[dict[str, Any]]:
        harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT token_id, harness_id, token_name, enabled, created_at, last_used_at
                FROM harness_tokens
                WHERE harness_id = ?
                ORDER BY token_id ASC
                """,
                (harness_id,),
            ).fetchall()
            return [
                {
                    "token_id": row["token_id"],
                    "harness_id": row["harness_id"],
                    "token_name": row["token_name"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"],
                }
                for row in rows
            ]

    def list_harnesses(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT harness_id, display_name, enabled, created_at
                FROM harnesses
                ORDER BY harness_id ASC
                """
            ).fetchall()
            return [
                {
                    "harness_id": row["harness_id"],
                    "display_name": row["display_name"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def get_harness_defaults(self, harness_id: str) -> dict[str, Any]:
        harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT default_from_address, default_inbox_address, updated_at
                FROM harness_client_defaults
                WHERE harness_id = ?
                """,
                (harness_id,),
            ).fetchone()
            return {
                "harness_id": harness_id,
                "default_from_address": row["default_from_address"] if row else None,
                "default_inbox_address": row["default_inbox_address"] if row else None,
                "updated_at": row["updated_at"] if row else None,
            }

    def set_harness_defaults(
        self,
        harness_id: str,
        *,
        default_from_address: Any = _UNSET,
        default_inbox_address: Any = _UNSET,
    ) -> dict[str, Any]:
        harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            harness = conn.execute(
                "SELECT 1 FROM harnesses WHERE harness_id = ? AND enabled = 1",
                (harness_id,),
            ).fetchone()
            if not harness:
                raise ValueError(f"harness not found or disabled: {harness_id}")

            current = conn.execute(
                """
                SELECT default_from_address, default_inbox_address
                FROM harness_client_defaults
                WHERE harness_id = ?
                """,
                (harness_id,),
            ).fetchone()
            resolved_from = current["default_from_address"] if current else None
            resolved_inbox = current["default_inbox_address"] if current else None

            if default_from_address is not _UNSET:
                resolved_from = None if default_from_address is None else str(default_from_address).strip() or None
            if default_inbox_address is not _UNSET:
                resolved_inbox = None if default_inbox_address is None else str(default_inbox_address).strip() or None

            if resolved_from is not None:
                self.resolve_address_for_harness(resolved_from, harness_id)
            if resolved_inbox is not None:
                self.resolve_address_for_harness(resolved_inbox, harness_id)

            now = utc_now()
            conn.execute(
                """
                INSERT INTO harness_client_defaults(harness_id, default_from_address, default_inbox_address, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(harness_id) DO UPDATE SET
                    default_from_address = excluded.default_from_address,
                    default_inbox_address = excluded.default_inbox_address,
                    updated_at = excluded.updated_at
                """,
                (harness_id, resolved_from, resolved_inbox, now),
            )
            return {
                "harness_id": harness_id,
                "default_from_address": resolved_from,
                "default_inbox_address": resolved_inbox,
                "updated_at": now,
            }

    def _require_enabled_project(self, conn: sqlite3.Connection, harness_id: str, project_id: str) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM projects
            WHERE harness_id = ? AND project_id = ? AND enabled = 1
            """,
            (harness_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError(f"project not found or disabled: {project_id}.{harness_id}")

    def _resolve_or_create_login_mailbox(
        self,
        harness_id: str,
        project_id: str,
        *,
        local_part: str,
        mailbox_type: str,
        accept_messages: bool,
        metadata: Optional[dict[str, Any]],
    ) -> tuple[MailboxRef, bool]:
        address = f"{local_part}@{project_id}.{harness_id}"
        created = False
        try:
            mailbox = self.resolve_address_for_harness(address, harness_id)
        except ValueError:
            mailbox = self.upsert_mailbox(
                address=address,
                mailbox_type=mailbox_type,
                enabled=True,
                accept_messages=accept_messages,
                metadata=metadata,
            )
            created = True
        return mailbox, created

    def _load_agent_session(
        self,
        conn: sqlite3.Connection,
        agent_session_id: int,
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT
                agent_session_id,
                harness_id,
                project_id,
                agent_name,
                session_name,
                metadata_json,
                created_at,
                expires_at,
                last_used_at
            FROM agent_sessions
            WHERE agent_session_id = ? AND enabled = 1
            """,
            (agent_session_id,),
        ).fetchone()
        if not row:
            return None

        mailbox_rows = conn.execute(
            """
            SELECT
                m.address_canonical AS address,
                m.mailbox_type,
                m.accept_messages,
                sm.can_send,
                sm.can_claim,
                sm.is_default_from,
                sm.is_default_claim,
                sm.agent_session_mailbox_id
            FROM agent_session_mailboxes sm
            JOIN mailboxes m ON m.mailbox_id = sm.mailbox_id
            WHERE sm.agent_session_id = ?
              AND m.enabled = 1
            ORDER BY sm.is_default_from DESC, sm.agent_session_mailbox_id ASC
            """,
            (agent_session_id,),
        ).fetchall()

        allowed_addresses: list[str] = []
        send_as_addresses: list[str] = []
        claim_addresses: list[str] = []
        default_claim_addresses: list[str] = []
        role_claim_addresses: list[str] = []
        default_role_claim_addresses: list[str] = []
        default_from_address: str | None = None

        for mailbox_row in mailbox_rows:
            address = str(mailbox_row["address"])
            mailbox_type = str(mailbox_row["mailbox_type"])
            allowed_addresses.append(address)
            if bool(mailbox_row["can_send"]):
                send_as_addresses.append(address)
                if bool(mailbox_row["is_default_from"]) and default_from_address is None:
                    default_from_address = address
            if bool(mailbox_row["can_claim"]) and bool(mailbox_row["accept_messages"]):
                claim_addresses.append(address)
                if mailbox_type == "role":
                    role_claim_addresses.append(address)
                if bool(mailbox_row["is_default_claim"]):
                    default_claim_addresses.append(address)
                    if mailbox_type == "role":
                        default_role_claim_addresses.append(address)

        if default_from_address is None and send_as_addresses:
            default_from_address = send_as_addresses[0]
        if not default_claim_addresses and claim_addresses:
            default_claim_addresses = claim_addresses.copy()
        default_inbox_address = resolve_default_inbox_address(
            claim_addresses=claim_addresses,
            default_claim_addresses=default_claim_addresses,
            role_claim_addresses=role_claim_addresses,
            default_role_claim_addresses=default_role_claim_addresses,
        )

        return {
            "agent_session_id": row["agent_session_id"],
            "harness_id": row["harness_id"],
            "project_id": row["project_id"],
            "agent_name": row["agent_name"],
            "session_name": row["session_name"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            "allowed_addresses": unique_preserving_order(allowed_addresses),
            "send_as_addresses": unique_preserving_order(send_as_addresses),
            "claim_addresses": unique_preserving_order(claim_addresses),
            "default_from_address": default_from_address,
            "default_claim_addresses": unique_preserving_order(default_claim_addresses),
            "default_inbox_address": default_inbox_address,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"],
        }

    def _snapshot_agent_session_request(self, prepared: dict[str, Any]) -> dict[str, Any]:
        return {
            "harness_id": prepared["harness_id"],
            "project_id": prepared["project_id"],
            "agent_name": prepared["agent_name"],
            "session_name": prepared["session_name"],
            "roles": list(prepared["roles"]),
            "mailbox_specs": [dict(spec) for spec in prepared["mailbox_specs"]],
            "metadata_payload": json.loads(json.dumps(prepared["metadata_payload"], ensure_ascii=False)),
            "expires_in_seconds": int(prepared["expires_in_seconds"]),
        }

    def _agent_session_identity_key(self, prepared: dict[str, Any]) -> tuple[Any, ...]:
        mailbox_specs = tuple(
            (
                str(spec["local_part"]),
                str(spec["mailbox_type"]),
                bool(spec["can_send"]),
                bool(spec["can_claim"]),
                bool(spec["default_from"]),
                bool(spec["default_claim"]),
            )
            for spec in prepared["mailbox_specs"]
        )
        return (
            prepared["harness_id"],
            prepared["project_id"],
            prepared["agent_name"],
            prepared["session_name"],
            tuple(prepared["roles"]),
            mailbox_specs,
        )

    def _cleanup_expired_agent_sessions_locked(self, now: str) -> None:
        expired_tokens = [
            token
            for token, record in self._agent_sessions_by_token.items()
            if str(record["expires_at"]) <= now
        ]
        for token in expired_tokens:
            self._remove_agent_session_token_locked(token)

    def _remove_agent_session_token_locked(self, token: str) -> dict[str, Any] | None:
        record = self._agent_sessions_by_token.pop(token, None)
        if not record:
            return None
        identity_key = record.get("identity_key")
        if identity_key is not None and self._agent_session_tokens_by_identity.get(identity_key) == token:
            del self._agent_session_tokens_by_identity[identity_key]
        return record

    def _build_agent_session_payload(
        self,
        conn: sqlite3.Connection,
        record: dict[str, Any],
        *,
        created_mailboxes: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        session_request = record["session_request"]
        allowed_addresses: list[str] = []
        send_as_addresses: list[str] = []
        claim_addresses: list[str] = []
        default_claim_addresses: list[str] = []
        role_claim_addresses: list[str] = []
        default_role_claim_addresses: list[str] = []
        default_from_address: str | None = None

        for spec in session_request["mailbox_specs"]:
            address = f"{spec['local_part']}@{session_request['project_id']}.{session_request['harness_id']}"
            row = conn.execute(
                """
                SELECT
                    m.address_canonical AS address,
                    m.accept_messages
                FROM mailboxes m
                WHERE m.address_canonical = ?
                  AND m.enabled = 1
                """,
                (address,),
            ).fetchone()
            if not row:
                continue
            canonical_address = str(row["address"])
            allowed_addresses.append(canonical_address)
            if bool(spec["can_send"]):
                send_as_addresses.append(canonical_address)
                if bool(spec["default_from"]) and default_from_address is None:
                    default_from_address = canonical_address
            if bool(spec["can_claim"]) and bool(row["accept_messages"]):
                claim_addresses.append(canonical_address)
                if str(spec["mailbox_type"]) == "role":
                    role_claim_addresses.append(canonical_address)
                if bool(spec["default_claim"]):
                    default_claim_addresses.append(canonical_address)
                    if str(spec["mailbox_type"]) == "role":
                        default_role_claim_addresses.append(canonical_address)

        if default_from_address is None and send_as_addresses:
            default_from_address = send_as_addresses[0]
        if not default_claim_addresses and claim_addresses:
            default_claim_addresses = claim_addresses.copy()
        default_inbox_address = resolve_default_inbox_address(
            claim_addresses=claim_addresses,
            default_claim_addresses=default_claim_addresses,
            role_claim_addresses=role_claim_addresses,
            default_role_claim_addresses=default_role_claim_addresses,
        )

        session_payload = {
            "agent_session_id": int(record["agent_session_id"]),
            "harness_id": session_request["harness_id"],
            "project_id": session_request["project_id"],
            "agent_name": session_request["agent_name"],
            "session_name": session_request["session_name"],
            "metadata": session_request["metadata_payload"],
            "allowed_addresses": unique_preserving_order(allowed_addresses),
            "send_as_addresses": unique_preserving_order(send_as_addresses),
            "claim_addresses": unique_preserving_order(claim_addresses),
            "default_from_address": default_from_address,
            "default_claim_addresses": unique_preserving_order(default_claim_addresses),
            "default_inbox_address": default_inbox_address,
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "last_used_at": record["last_used_at"],
        }
        if created_mailboxes is not None:
            session_payload["created_mailboxes"] = created_mailboxes
        return session_payload

    def _prepare_agent_session_request(
        self,
        harness_id: str,
        project_id: str,
        *,
        role: Optional[str] = None,
        roles: Optional[list[str]] = None,
        session_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        local_part: Optional[str] = None,
        mailbox_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        expires_in_seconds: int = 86_400,
    ) -> dict[str, Any]:
        caller_harness_id = normalize_harness_id(harness_id)
        normalized_project_id = normalize_address_component(project_id, "project_id")
        normalized_agent_name = (
            normalize_address_component(agent_name, "agent_name")
            if agent_name is not None and agent_name.strip()
            else None
        )
        normalized_session_name = (
            normalize_address_component(session_name, "session")
            if session_name is not None and session_name.strip()
            else None
        )
        normalized_local_part = (
            normalize_address_component(local_part, "local_part")
            if local_part is not None and local_part.strip()
            else None
        )

        requested_roles = list(roles or [])
        if role is not None:
            requested_roles.append(role)
        normalized_roles = unique_preserving_order(
            [normalize_address_component(item, "role") for item in requested_roles if str(item).strip()]
        )

        if normalized_local_part and normalized_roles and normalized_local_part not in normalized_roles:
            raise ValueError("local_part cannot differ from role/roles in login requests")

        local_part_spec: dict[str, Any] | None = None
        if not normalized_roles and normalized_local_part:
            resolved_mailbox_type = mailbox_type or "session"
            if resolved_mailbox_type not in {"session", "role", "group"}:
                raise ValueError("mailbox_type must be one of: session, role, group")
            local_part_spec = {
                "local_part": normalized_local_part,
                "mailbox_type": resolved_mailbox_type,
                "can_send": True,
                "can_claim": True,
                "default_from": True,
                "default_claim": True,
            }

        if not normalized_roles and not normalized_session_name and local_part_spec is None:
            raise ValueError("login requires at least one role, roles entry, local_part, or session")
        if expires_in_seconds <= 0:
            raise ValueError("expires_in_seconds must be greater than zero")

        role_specs = [
            {
                "local_part": normalized_role,
                "mailbox_type": "role",
                "can_send": True,
                "can_claim": True,
                "default_from": index == 0,
                "default_claim": True,
            }
            for index, normalized_role in enumerate(normalized_roles)
        ]

        session_spec: dict[str, Any] | None = None
        if normalized_session_name is not None:
            session_spec = {
                "local_part": f"session_{normalized_session_name}",
                "mailbox_type": mailbox_type or "session",
                "can_send": True,
                "can_claim": True,
                "default_from": not role_specs and local_part_spec is None,
                "default_claim": True,
            }
            if session_spec["mailbox_type"] not in {"session", "role", "group"}:
                raise ValueError("mailbox_type must be one of: session, role, group")

        mailbox_specs = role_specs.copy()
        if local_part_spec is not None:
            mailbox_specs.append(local_part_spec)
        if session_spec is not None:
            mailbox_specs.append(session_spec)

        metadata_payload = {
            "roles": normalized_roles,
            "session": normalized_session_name,
            "agent_name": normalized_agent_name,
            "login_metadata": metadata,
        }
        return {
            "harness_id": caller_harness_id,
            "project_id": normalized_project_id,
            "agent_name": normalized_agent_name,
            "session_name": normalized_session_name,
            "roles": normalized_roles,
            "mailbox_specs": mailbox_specs,
            "metadata_payload": metadata_payload,
            "expires_in_seconds": expires_in_seconds,
        }

    def preview_agent_session_for_harness(
        self,
        harness_id: str,
        project_id: str,
        *,
        role: Optional[str] = None,
        roles: Optional[list[str]] = None,
        session_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        local_part: Optional[str] = None,
        mailbox_type: Optional[str] = None,
        accept_messages: bool = True,
        metadata: Optional[dict[str, Any]] = None,
        expires_in_seconds: int = 86_400,
    ) -> dict[str, Any]:
        prepared = self._prepare_agent_session_request(
            harness_id,
            project_id,
            role=role,
            roles=roles,
            session_name=session_name,
            agent_name=agent_name,
            local_part=local_part,
            mailbox_type=mailbox_type,
            metadata=metadata,
            expires_in_seconds=expires_in_seconds,
        )
        with self.connect() as conn:
            self._require_enabled_project(conn, prepared["harness_id"], prepared["project_id"])
            mailboxes: list[dict[str, Any]] = []
            allowed_addresses: list[str] = []
            send_as_addresses: list[str] = []
            claim_addresses: list[str] = []
            default_claim_addresses: list[str] = []
            role_claim_addresses: list[str] = []
            default_role_claim_addresses: list[str] = []
            default_from_address: str | None = None
            created_mailboxes: list[str] = []

            for spec in prepared["mailbox_specs"]:
                address = f"{spec['local_part']}@{prepared['project_id']}.{prepared['harness_id']}"
                row = conn.execute(
                    """
                    SELECT
                        m.mailbox_id,
                        m.mailbox_type,
                        m.enabled,
                        m.accept_messages
                    FROM mailboxes m
                    WHERE m.address_canonical = ?
                    """,
                    (address,),
                ).fetchone()
                exists = row is not None and bool(row["enabled"])
                effective_accept_messages = bool(row["accept_messages"]) if row is not None else bool(accept_messages)
                effective_mailbox_type = row["mailbox_type"] if row is not None else spec["mailbox_type"]
                allowed_addresses.append(address)
                if bool(spec["can_send"]):
                    send_as_addresses.append(address)
                    if bool(spec["default_from"]) and default_from_address is None:
                        default_from_address = address
                if bool(spec["can_claim"]) and effective_accept_messages:
                    claim_addresses.append(address)
                    if str(effective_mailbox_type) == "role":
                        role_claim_addresses.append(address)
                    if bool(spec["default_claim"]):
                        default_claim_addresses.append(address)
                        if str(effective_mailbox_type) == "role":
                            default_role_claim_addresses.append(address)
                if not exists:
                    created_mailboxes.append(address)
                mailboxes.append(
                    {
                        "address": address,
                        "mailbox_type": effective_mailbox_type,
                        "exists": exists,
                        "accept_messages": effective_accept_messages,
                        "can_send": bool(spec["can_send"]),
                        "can_claim": bool(spec["can_claim"]),
                        "is_default_from": bool(spec["default_from"]),
                        "is_default_claim": bool(spec["default_claim"]),
                    }
                )

        if default_from_address is None and send_as_addresses:
            default_from_address = send_as_addresses[0]
        if not default_claim_addresses and claim_addresses:
            default_claim_addresses = claim_addresses.copy()
        default_inbox_address = resolve_default_inbox_address(
            claim_addresses=claim_addresses,
            default_claim_addresses=default_claim_addresses,
            role_claim_addresses=role_claim_addresses,
            default_role_claim_addresses=default_role_claim_addresses,
        )

        return {
            "harness_id": prepared["harness_id"],
            "project_id": prepared["project_id"],
            "agent_name": prepared["agent_name"],
            "session_name": prepared["session_name"],
            "roles": prepared["roles"],
            "mailboxes": mailboxes,
            "allowed_addresses": unique_preserving_order(allowed_addresses),
            "send_as_addresses": unique_preserving_order(send_as_addresses),
            "claim_addresses": unique_preserving_order(claim_addresses),
            "default_from_address": default_from_address,
            "default_claim_addresses": unique_preserving_order(default_claim_addresses),
            "default_inbox_address": default_inbox_address,
            "created_mailboxes": created_mailboxes,
            "expires_in_seconds": prepared["expires_in_seconds"],
            "metadata": prepared["metadata_payload"],
        }

    def create_agent_session_for_harness(
        self,
        harness_id: str,
        project_id: str,
        *,
        role: Optional[str] = None,
        roles: Optional[list[str]] = None,
        session_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        local_part: Optional[str] = None,
        mailbox_type: Optional[str] = None,
        accept_messages: bool = True,
        metadata: Optional[dict[str, Any]] = None,
        expires_in_seconds: int = 86_400,
    ) -> dict[str, Any]:
        prepared = self._prepare_agent_session_request(
            harness_id,
            project_id,
            role=role,
            roles=roles,
            session_name=session_name,
            agent_name=agent_name,
            local_part=local_part,
            mailbox_type=mailbox_type,
            metadata=metadata,
            expires_in_seconds=expires_in_seconds,
        )
        created_mailboxes: list[str] = []

        with self.connect() as conn:
            self._require_enabled_project(conn, prepared["harness_id"], prepared["project_id"])

        for spec in prepared["mailbox_specs"]:
            mailbox, created = self._resolve_or_create_login_mailbox(
                prepared["harness_id"],
                prepared["project_id"],
                local_part=str(spec["local_part"]),
                mailbox_type=str(spec["mailbox_type"]),
                accept_messages=accept_messages,
                metadata=metadata,
            )
            if created:
                created_mailboxes.append(mailbox.address)

        identity_key = self._agent_session_identity_key(prepared)
        reused = False
        with self._agent_session_lock:
            now = utc_now()
            self._cleanup_expired_agent_sessions_locked(now)
            existing_token = self._agent_session_tokens_by_identity.get(identity_key)
            record = self._agent_sessions_by_token.get(existing_token) if existing_token else None
            if record and str(record["expires_at"]) > now:
                reused = True
            else:
                record = None
                created_at = now
                expires_at = utc_after(prepared["expires_in_seconds"])
                for _ in range(4):
                    plain_token = secrets.token_urlsafe(32)
                    if plain_token not in self._agent_sessions_by_token:
                        record = {
                            "agent_session_id": self._next_agent_session_id,
                            "token": plain_token,
                            "identity_key": identity_key,
                            "session_request": self._snapshot_agent_session_request(prepared),
                            "created_at": created_at,
                            "expires_at": expires_at,
                            "last_used_at": None,
                        }
                        self._next_agent_session_id += 1
                        self._agent_sessions_by_token[plain_token] = record
                        self._agent_session_tokens_by_identity[identity_key] = plain_token
                        break
                if record is None:
                    raise RuntimeError("failed to create unique agent session token")

            with self.connect() as conn:
                session_payload = self._build_agent_session_payload(
                    conn,
                    record,
                    created_mailboxes=created_mailboxes,
                )

            return {
                "session_token": str(record["token"]),
                "reused": reused,
                "session": session_payload,
            }

    def has_admin_account(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM admin_accounts WHERE enabled = 1 LIMIT 1"
            ).fetchone()
            return row is not None

    def create_admin_bootstrap_token(self, expires_in_seconds: int = 3600) -> dict[str, str]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM admin_accounts WHERE enabled = 1 LIMIT 1").fetchone():
                    raise ValueError("admin account already exists")
                now = utc_now()
                conn.execute(
                    """
                    DELETE FROM admin_bootstrap_tokens
                    WHERE used_at IS NOT NULL OR expires_at <= ?
                    """,
                    (now,),
                )
                conn.execute("DELETE FROM admin_bootstrap_tokens")
                token = secrets.token_urlsafe(32)
                expires_at = utc_after(expires_in_seconds)
                conn.execute(
                    """
                    INSERT INTO admin_bootstrap_tokens(token_hash, created_at, expires_at, used_at)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (hash_token(token), now, expires_at),
                )
                conn.commit()
                return {"token": token, "expires_at": expires_at}
            except Exception:
                conn.rollback()
                raise

    def is_valid_admin_bootstrap_token(self, token: str) -> bool:
        if not isinstance(token, str):
            return False
        token = token.strip()
        if not token:
            return False
        candidate_hash = hash_token(token)
        now = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT token_hash
                FROM admin_bootstrap_tokens
                WHERE used_at IS NULL AND expires_at > ?
                ORDER BY bootstrap_token_id ASC
                """,
                (now,),
            ).fetchall()
            matched = False
            for row in rows:
                if hmac.compare_digest(row["token_hash"], candidate_hash):
                    matched = True
            return matched

    def create_initial_admin_account(self, token: str, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        if not normalized_username:
            raise ValueError("username must not be empty")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        candidate_hash = hash_token(token.strip())
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM admin_accounts WHERE enabled = 1 LIMIT 1").fetchone():
                    raise PermissionError("admin account already exists")
                rows = conn.execute(
                    """
                    SELECT bootstrap_token_id, token_hash
                    FROM admin_bootstrap_tokens
                    WHERE used_at IS NULL AND expires_at > ?
                    ORDER BY bootstrap_token_id ASC
                    """,
                    (now,),
                ).fetchall()
                matched_token_id: int | None = None
                for row in rows:
                    if hmac.compare_digest(row["token_hash"], candidate_hash):
                        matched_token_id = int(row["bootstrap_token_id"])
                if matched_token_id is None:
                    raise PermissionError("invalid or expired setup token")
                conn.execute(
                    """
                    INSERT INTO admin_accounts(username, password_hash, enabled, created_at, last_login_at)
                    VALUES (?, ?, 1, ?, NULL)
                    """,
                    (normalized_username, hash_password(password), now),
                )
                conn.execute(
                    """
                    UPDATE admin_bootstrap_tokens
                    SET used_at = ?
                    WHERE bootstrap_token_id = ?
                    """,
                    (now, matched_token_id),
                )
                conn.commit()
                return {"username": normalized_username, "created_at": now}
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(f"admin username already exists: {normalized_username}") from exc
            except Exception:
                conn.rollback()
                raise

    def authenticate_admin_credentials(self, username: str, password: str) -> str | None:
        normalized_username = username.strip().lower()
        if not normalized_username or not password:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT admin_id, username, password_hash
                FROM admin_accounts
                WHERE username = ? AND enabled = 1
                """,
                (normalized_username,),
            ).fetchone()
            if not row:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            conn.execute(
                "UPDATE admin_accounts SET last_login_at = ? WHERE admin_id = ?",
                (utc_now(), row["admin_id"]),
            )
            return row["username"]

    def list_projects(self, harness_id: Optional[str] = None) -> list[dict[str, Any]]:
        normalized_harness_id = normalize_harness_id(harness_id) if harness_id else None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, harness_id, display_name, enabled, created_at
                FROM projects
                WHERE (? IS NULL OR harness_id = ?)
                ORDER BY harness_id ASC, project_id ASC
                """,
                (normalized_harness_id, normalized_harness_id),
            ).fetchall()
            return [
                {
                    "project_id": row["project_id"],
                    "harness_id": row["harness_id"],
                    "display_name": row["display_name"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def list_mailboxes(
        self, harness_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        normalized_harness_id = normalize_harness_id(harness_id) if harness_id else None
        normalized_project_id = project_id.strip().lower() if project_id else None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.mailbox_id,
                    m.address_canonical,
                    m.local_part,
                    p.project_id,
                    m.harness_id,
                    m.mailbox_type,
                    m.enabled,
                    m.accept_messages,
                    m.metadata_json,
                    m.created_at,
                    m.updated_at
                FROM mailboxes m
                JOIN projects p ON p.project_pk = m.project_pk
                WHERE (? IS NULL OR m.harness_id = ?)
                  AND (? IS NULL OR p.project_id = ?)
                ORDER BY m.harness_id ASC, p.project_id ASC, m.local_part ASC
                """,
                (
                    normalized_harness_id,
                    normalized_harness_id,
                    normalized_project_id,
                    normalized_project_id,
                ),
            ).fetchall()
            return [
                {
                    "mailbox_id": row["mailbox_id"],
                    "address": row["address_canonical"],
                    "local_part": row["local_part"],
                    "project_id": row["project_id"],
                    "harness_id": row["harness_id"],
                    "mailbox_type": row["mailbox_type"],
                    "enabled": bool(row["enabled"]),
                    "accept_messages": bool(row["accept_messages"]),
                    "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def authenticate_harness_token(self, token: str) -> str | None:
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token:
            return None
        candidate_hash = hash_token(token)
        matched_token_id: int | None = None
        matched_harness_id: str | None = None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT token_id, harness_id, token_hash
                FROM harness_tokens
                WHERE enabled = 1
                ORDER BY token_id ASC
                """
            ).fetchall()
            for row in rows:
                if hmac.compare_digest(row["token_hash"], candidate_hash):
                    matched_token_id = int(row["token_id"])
                    matched_harness_id = row["harness_id"]
            if matched_token_id is not None and matched_harness_id is not None:
                conn.execute(
                    "UPDATE harness_tokens SET last_used_at = ? WHERE token_id = ?",
                    (utc_now(), matched_token_id),
                )
                return matched_harness_id
        return None

    def authenticate_agent_session_token(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token:
            return None
        now = utc_now()
        with self._agent_session_lock:
            self._cleanup_expired_agent_sessions_locked(now)
            matched_record: dict[str, Any] | None = None
            for candidate_token, candidate_record in self._agent_sessions_by_token.items():
                if hmac.compare_digest(candidate_token, token):
                    matched_record = candidate_record
            if matched_record is None:
                return None
            matched_record["last_used_at"] = now
            with self.connect() as conn:
                return self._build_agent_session_payload(conn, matched_record)

    def invalidate_agent_session_token(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token:
            return None
        now = utc_now()
        with self._agent_session_lock:
            self._cleanup_expired_agent_sessions_locked(now)
            matched_token: str | None = None
            matched_record: dict[str, Any] | None = None
            for candidate_token, candidate_record in self._agent_sessions_by_token.items():
                if hmac.compare_digest(candidate_token, token):
                    matched_token = candidate_token
                    matched_record = candidate_record
            if matched_token is None or matched_record is None:
                return None
            removed_record = self._remove_agent_session_token_locked(matched_token)
            if removed_record is None:
                return None
            removed_record["last_used_at"] = now
            with self.connect() as conn:
                return self._build_agent_session_payload(conn, removed_record)

    def authenticate_mailbox_principal(self, token: str) -> dict[str, Any] | None:
        session = self.authenticate_agent_session_token(token)
        if session:
            return {"kind": "agent_session", **session}
        harness_id = self.authenticate_harness_token(token)
        if harness_id:
            return {"kind": "harness", "harness_id": harness_id}
        return None

    def upsert_project(
        self, project_id: str, harness_id: str, display_name: Optional[str] = None, enabled: bool = True
    ) -> None:
        project_id = project_id.lower()
        harness_id = normalize_harness_id(harness_id)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(project_id, harness_id, display_name, enabled, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, harness_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = excluded.enabled
                """,
                (project_id, harness_id, display_name, int(enabled), now),
            )

    def upsert_mailbox(
        self,
        address: str,
        mailbox_type: str = "session",
        enabled: bool = True,
        accept_messages: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MailboxRef:
        if mailbox_type not in {"session", "role", "group"}:
            raise ValueError("mailbox_type must be one of: session, role, group")
        local, project_id, harness_id = split_address(address)
        canonical = f"{local}@{project_id}.{harness_id}"
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT project_pk FROM projects WHERE project_id = ? AND harness_id = ? AND enabled = 1",
                (project_id, harness_id),
            ).fetchone()
            if not row:
                raise ValueError(f"project not found or disabled: {project_id}.{harness_id}")
            project_pk = row["project_pk"]
            conn.execute(
                """
                INSERT INTO mailboxes(
                    local_part, project_pk, harness_id, mailbox_type, address_canonical,
                    enabled, accept_messages, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address_canonical) DO UPDATE SET
                    mailbox_type = excluded.mailbox_type,
                    enabled = excluded.enabled,
                    accept_messages = excluded.accept_messages,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    local,
                    project_pk,
                    harness_id,
                    mailbox_type,
                    canonical,
                    int(enabled),
                    int(accept_messages),
                    json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                    now,
                    now,
                ),
            )
        return self.resolve_address(canonical)

    def resolve_address(self, address: str) -> MailboxRef:
        canonical = canonicalize_address(address)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT m.mailbox_id, m.address_canonical, m.local_part, p.project_id, m.harness_id, m.mailbox_type
                FROM mailboxes m
                JOIN projects p ON p.project_pk = m.project_pk
                WHERE m.address_canonical = ? AND m.enabled = 1
                """,
                (canonical,),
            ).fetchone()
            if not row:
                raise ValueError(f"mailbox not found or disabled: {canonical}")
            return MailboxRef(
                mailbox_id=row["mailbox_id"],
                address=row["address_canonical"],
                local_part=row["local_part"],
                project_id=row["project_id"],
                harness_id=row["harness_id"],
                mailbox_type=row["mailbox_type"],
            )

    def resolve_address_for_harness(
        self, address: str, harness_id: str, allow_cross_harness: bool = False
    ) -> MailboxRef:
        canonical = canonicalize_address(address)
        if not allow_cross_harness:
            _, _, address_harness_id = split_address(canonical)
            caller_harness_id = normalize_harness_id(harness_id)
            if address_harness_id != caller_harness_id:
                raise PermissionError(
                    f"address {canonical} belongs to harness {address_harness_id}, not caller harness {caller_harness_id}"
                )
        return self.resolve_address(canonical)

    def login_mailbox_for_harness(
        self,
        harness_id: str,
        project_id: str,
        *,
        role: Optional[str] = None,
        local_part: Optional[str] = None,
        mailbox_type: Optional[str] = None,
        accept_messages: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        caller_harness_id = normalize_harness_id(harness_id)
        normalized_project_id = project_id.strip().lower()
        normalized_role = role.strip().lower() if role is not None else None
        normalized_local_part = local_part.strip().lower() if local_part is not None else None

        if normalized_role and normalized_local_part and normalized_role != normalized_local_part:
            raise ValueError("role and local_part must match when both are provided")

        chosen_local_part = normalized_local_part or normalized_role
        if not chosen_local_part:
            raise ValueError("either role or local_part is required")

        resolved_mailbox_type = mailbox_type or ("role" if normalized_role else "session")
        if resolved_mailbox_type not in {"session", "role", "group"}:
            raise ValueError("mailbox_type must be one of: session, role, group")

        address = f"{chosen_local_part}@{normalized_project_id}.{caller_harness_id}"
        created = False
        try:
            mailbox = self.resolve_address_for_harness(address, caller_harness_id)
        except ValueError:
            mailbox = self.upsert_mailbox(
                address=address,
                mailbox_type=resolved_mailbox_type,
                enabled=True,
                accept_messages=accept_messages,
                metadata=metadata,
            )
            created = True

        return {
            "address": mailbox.address,
            "from_address": mailbox.address,
            "inbox_address": mailbox.address,
            "mailbox_id": mailbox.mailbox_id,
            "local_part": mailbox.local_part,
            "project_id": mailbox.project_id,
            "harness_id": mailbox.harness_id,
            "mailbox_type": mailbox.mailbox_type,
            "role": normalized_role,
            "created": created,
        }

    def bind_mailbox(
        self,
        address: str,
        *,
        session_id: Optional[str],
        run_id: Optional[str],
        consumer_id: Optional[str],
        bind_mode: str = "exclusive",
        lease_seconds: int = 60,
    ) -> int:
        mailbox = self.resolve_address(address)
        if bind_mode not in {"exclusive", "shared"}:
            raise ValueError("bind_mode must be exclusive or shared")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if bind_mode == "exclusive":
                    conn.execute(
                        "UPDATE mailbox_bindings SET active = 0, updated_at = ? WHERE mailbox_id = ? AND active = 1",
                        (utc_now(), mailbox.mailbox_id),
                    )
                cursor = conn.execute(
                    """
                    INSERT INTO mailbox_bindings(
                        mailbox_id, session_id, run_id, consumer_id, bind_mode,
                        active, last_heartbeat_at, lease_until, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        mailbox.mailbox_id,
                        session_id,
                        run_id,
                        consumer_id,
                        bind_mode,
                        utc_now(),
                        utc_after(lease_seconds),
                        utc_now(),
                        utc_now(),
                    ),
                )
                binding_id = int(cursor.lastrowid)
                self._event(
                    conn,
                    "mailbox.bound",
                    mailbox_id=mailbox.mailbox_id,
                    actor=consumer_id,
                    details={"binding_id": binding_id, "session_id": session_id, "run_id": run_id},
                )
                conn.commit()
                return binding_id
            except Exception:
                conn.rollback()
                raise

    def add_routing_policy(
        self,
        *,
        effect: str,
        priority: int = 0,
        from_harness_id: Optional[str] = None,
        from_project_id: Optional[str] = None,
        from_mailbox_type: Optional[str] = None,
        to_harness_id: Optional[str] = None,
        to_project_id: Optional[str] = None,
        to_mailbox_type: Optional[str] = None,
        description: Optional[str] = None,
        enabled: bool = True,
    ) -> int:
        if effect not in {"allow", "deny"}:
            raise ValueError("effect must be allow or deny")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO routing_policies(
                    from_harness_id, from_project_id, from_mailbox_type,
                    to_harness_id, to_project_id, to_mailbox_type,
                    effect, priority, enabled, description, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    from_harness_id.lower() if from_harness_id else None,
                    from_project_id.lower() if from_project_id else None,
                    from_mailbox_type,
                    to_harness_id.lower() if to_harness_id else None,
                    to_project_id.lower() if to_project_id else None,
                    to_mailbox_type,
                    effect,
                    priority,
                    int(enabled),
                    description,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def allow_same_project(self, project_id: str, harness_id: str, priority: int = 100) -> int:
        return self.add_routing_policy(
            effect="allow",
            priority=priority,
            from_project_id=project_id,
            from_harness_id=harness_id,
            to_project_id=project_id,
            to_harness_id=harness_id,
            description="same project same harness",
        )

    def allow_cross_harness_same_project(
        self, project_id: str, from_harness_id: str, to_harness_id: str, priority: int = 90
    ) -> int:
        return self.add_routing_policy(
            effect="allow",
            priority=priority,
            from_project_id=project_id,
            from_harness_id=from_harness_id,
            to_project_id=project_id,
            to_harness_id=to_harness_id,
            description="cross harness same project",
        )

    def deny_all(self, priority: int = -1000) -> int:
        return self.add_routing_policy(effect="deny", priority=priority, description="default deny")

    def is_route_allowed(self, from_address: str, to_address: str) -> bool:
        src = self.resolve_address(from_address)
        dst = self.resolve_address(to_address)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT effect, priority, policy_id
                FROM routing_policies
                WHERE enabled = 1
                  AND (from_harness_id IS NULL OR from_harness_id = ?)
                  AND (from_project_id IS NULL OR from_project_id = ?)
                  AND (from_mailbox_type IS NULL OR from_mailbox_type = ?)
                  AND (to_harness_id IS NULL OR to_harness_id = ?)
                  AND (to_project_id IS NULL OR to_project_id = ?)
                  AND (to_mailbox_type IS NULL OR to_mailbox_type = ?)
                ORDER BY priority DESC, policy_id DESC
                LIMIT 1
                """,
                (
                    src.harness_id,
                    src.project_id,
                    src.mailbox_type,
                    dst.harness_id,
                    dst.project_id,
                    dst.mailbox_type,
                ),
            ).fetchone()
            return bool(row and row["effect"] == "allow")

    def send(
        self,
        *,
        from_address: str,
        to_address: str,
        payload: dict[str, Any],
        subject: Optional[str] = None,
        message_type: str = "generic",
        priority: int = 0,
        thread_id: Optional[str] = None,
        in_reply_to_message_id: Optional[str] = None,
        reply_to_address: Optional[str] = None,
        correlation_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        headers: Optional[dict[str, Any]] = None,
        deliver_after_seconds: int = 0,
        expires_in_seconds: Optional[int] = None,
        max_attempts: int = 8,
        bypass_routing: bool = False,
    ) -> dict[str, Any]:
        src = self.resolve_address(from_address)
        dst = self.resolve_address(to_address)
        if not bypass_routing and not self.is_route_allowed(src.address, dst.address):
            raise PermissionError(f"route denied: {src.address} -> {dst.address}")
        now = utc_now()
        deliver_after = utc_after(deliver_after_seconds) if deliver_after_seconds > 0 else now
        expires_at = utc_after(expires_in_seconds) if expires_in_seconds is not None else None
        reply_to_mailbox_id = self.resolve_address(reply_to_address).mailbox_id if reply_to_address else None
        message_id = str(uuid.uuid4())
        thread_id = thread_id or in_reply_to_message_id or str(uuid.uuid4())

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    existing = conn.execute(
                        "SELECT message_id, thread_id FROM messages WHERE from_mailbox_id = ? AND idempotency_key = ?",
                        (src.mailbox_id, idempotency_key),
                    ).fetchone()
                    if existing:
                        delivery = conn.execute(
                            "SELECT delivery_id FROM deliveries WHERE message_id = ? AND to_mailbox_id = ?",
                            (existing["message_id"], dst.mailbox_id),
                        ).fetchone()
                        conn.commit()
                        return {
                            "message_id": existing["message_id"],
                            "delivery_id": delivery["delivery_id"] if delivery else None,
                            "thread_id": existing["thread_id"],
                            "deduplicated": True,
                        }

                conn.execute(
                    """
                    INSERT INTO messages(
                        message_id, from_mailbox_id, reply_to_mailbox_id, thread_id,
                        in_reply_to_message_id, correlation_id, workflow_id, subject,
                        message_type, priority, idempotency_key, payload_json,
                        headers_json, deliver_after, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        src.mailbox_id,
                        reply_to_mailbox_id,
                        thread_id,
                        in_reply_to_message_id,
                        correlation_id,
                        workflow_id,
                        subject,
                        message_type,
                        priority,
                        idempotency_key,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(headers, ensure_ascii=False) if headers is not None else None,
                        deliver_after,
                        expires_at,
                        now,
                    ),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO deliveries(
                        message_id, to_mailbox_id, status, expires_at, attempt_count,
                        max_attempts, available_at, created_at, updated_at
                    ) VALUES (?, ?, 'queued', ?, 0, ?, ?, ?, ?)
                    """,
                    (message_id, dst.mailbox_id, expires_at, max_attempts, deliver_after, now, now),
                )
                delivery_id = int(cursor.lastrowid)
                self._event(
                    conn,
                    "message.sent",
                    message_id=message_id,
                    delivery_id=delivery_id,
                    mailbox_id=dst.mailbox_id,
                    actor=src.address,
                    details={"from": src.address, "to": dst.address, "subject": subject},
                )
                conn.commit()
                return {
                    "message_id": message_id,
                    "delivery_id": delivery_id,
                    "thread_id": thread_id,
                    "deduplicated": False,
                }
            except Exception:
                conn.rollback()
                raise

    def claim_any(
        self,
        *,
        to_addresses: list[str],
        consumer_id: str,
        lease_seconds: int = 60,
        serialization_scope: str = "mailbox_thread",
    ) -> Optional[dict[str, Any]]:
        if not to_addresses:
            raise ValueError("to_addresses must contain at least one address")
        normalized_scope = normalize_claim_serialization_scope(serialization_scope)
        resolved_mailboxes = [self.resolve_address(address) for address in to_addresses]
        mailbox_ids = [mailbox.mailbox_id for mailbox in resolved_mailboxes]
        placeholders = ", ".join("?" for _ in mailbox_ids)
        claim_token = str(uuid.uuid4())
        lease_until = utc_after(lease_seconds)
        now = utc_now()
        thread_scope_filter = ""
        thread_scope_params: tuple[Any, ...] = ()
        if normalized_scope == "mailbox_thread":
            thread_scope_filter = """
                      AND NOT EXISTS (
                            SELECT 1
                            FROM deliveries d2
                            JOIN messages m2 ON m2.message_id = d2.message_id
                            WHERE d2.to_mailbox_id = d.to_mailbox_id
                              AND m2.thread_id = m.thread_id
                              AND d2.status = 'claimed'
                              AND d2.lease_until >= ?
                              AND d2.delivery_id <> d.delivery_id
                          )
            """
            thread_scope_params = (now,)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                candidate = conn.execute(
                    f"""
                    SELECT d.delivery_id
                    FROM deliveries d
                    JOIN messages m ON m.message_id = d.message_id
                    WHERE d.to_mailbox_id IN ({placeholders})
                      AND (
                            d.status = 'queued'
                            OR (d.status = 'claimed' AND d.lease_until < ?)
                          )
                      AND d.available_at <= ?
                      AND (d.expires_at IS NULL OR d.expires_at > ?)
                      AND d.attempt_count < d.max_attempts
                      {thread_scope_filter}
                    ORDER BY m.priority DESC, d.created_at ASC
                    LIMIT 1
                    """,
                    (*mailbox_ids, now, now, now, *thread_scope_params),
                ).fetchone()
                if not candidate:
                    conn.commit()
                    return None

                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'claimed',
                        consumer_id = ?,
                        claim_token = ?,
                        claimed_at = ?,
                        lease_until = ?,
                        attempt_count = attempt_count + 1,
                        updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (consumer_id, claim_token, now, lease_until, now, candidate["delivery_id"]),
                )
                row = conn.execute(
                    """
                    SELECT
                        d.delivery_id,
                        d.message_id,
                        d.to_mailbox_id,
                        d.status,
                        d.consumer_id,
                        d.claim_token,
                        d.claimed_at,
                        d.lease_until,
                        d.attempt_count,
                        m.thread_id,
                        m.in_reply_to_message_id,
                        m.correlation_id,
                        m.workflow_id,
                        m.subject,
                        m.message_type,
                        m.priority,
                        m.payload_json,
                        m.headers_json,
                        m.created_at AS message_created_at,
                        src.address_canonical AS from_address,
                        dst.address_canonical AS to_address
                    FROM deliveries d
                    JOIN messages m ON m.message_id = d.message_id
                    JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                    JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                    WHERE d.delivery_id = ?
                    """,
                    (candidate["delivery_id"],),
                ).fetchone()
                self._event(
                    conn,
                    "delivery.claimed",
                    message_id=row["message_id"],
                    delivery_id=row["delivery_id"],
                    mailbox_id=row["to_mailbox_id"],
                    actor=consumer_id,
                    details={"lease_until": lease_until, "serialization_scope": normalized_scope},
                )
                conn.commit()
                return {
                    "delivery_id": row["delivery_id"],
                    "message_id": row["message_id"],
                    "claim_token": row["claim_token"],
                    "from": row["from_address"],
                    "to": row["to_address"],
                    "subject": row["subject"],
                    "message_type": row["message_type"],
                    "priority": row["priority"],
                    "thread_id": row["thread_id"],
                    "in_reply_to_message_id": row["in_reply_to_message_id"],
                    "correlation_id": row["correlation_id"],
                    "workflow_id": row["workflow_id"],
                    "payload": json.loads(row["payload_json"]),
                    "headers": json.loads(row["headers_json"]) if row["headers_json"] else None,
                    "attempt_count": row["attempt_count"],
                    "claimed_at": row["claimed_at"],
                    "lease_until": row["lease_until"],
                    "serialization_scope": normalized_scope,
                }
            except Exception:
                conn.rollback()
                raise

    def claim(
        self,
        *,
        to_address: str,
        consumer_id: str,
        lease_seconds: int = 60,
        serialization_scope: str = "mailbox_thread",
    ) -> Optional[dict[str, Any]]:
        return self.claim_any(
            to_addresses=[to_address],
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            serialization_scope=serialization_scope,
        )

    def ack(self, *, delivery_id: int, claim_token: str, actor: Optional[str] = None) -> bool:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT message_id, to_mailbox_id FROM deliveries WHERE delivery_id = ? AND status = 'claimed' AND claim_token = ?",
                    (delivery_id, claim_token),
                ).fetchone()
                if not row:
                    conn.commit()
                    return False
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'acked', acked_at = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'claimed' AND claim_token = ?
                    """,
                    (now, now, delivery_id, claim_token),
                )
                self._event(
                    conn,
                    "delivery.acked",
                    message_id=row["message_id"],
                    delivery_id=delivery_id,
                    mailbox_id=row["to_mailbox_id"],
                    actor=actor,
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def nack(
        self,
        *,
        delivery_id: int,
        claim_token: str,
        retry_after_seconds: int = 30,
        last_error: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> bool:
        now = utc_now()
        retry_at = utc_after(retry_after_seconds)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT message_id, to_mailbox_id, attempt_count, max_attempts FROM deliveries WHERE delivery_id = ? AND status = 'claimed' AND claim_token = ?",
                    (delivery_id, claim_token),
                ).fetchone()
                if not row:
                    conn.commit()
                    return False
                terminal = row["attempt_count"] >= row["max_attempts"]
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = CASE WHEN attempt_count >= max_attempts THEN 'dead' ELSE 'queued' END,
                        consumer_id = NULL,
                        claim_token = NULL,
                        claimed_at = NULL,
                        lease_until = NULL,
                        available_at = CASE WHEN attempt_count >= max_attempts THEN available_at ELSE ? END,
                        dead_at = CASE WHEN attempt_count >= max_attempts THEN ? ELSE dead_at END,
                        last_error = ?,
                        updated_at = ?
                    WHERE delivery_id = ? AND status = 'claimed' AND claim_token = ?
                    """,
                    (retry_at, now, last_error, now, delivery_id, claim_token),
                )
                self._event(
                    conn,
                    "delivery.dead" if terminal else "delivery.requeued",
                    message_id=row["message_id"],
                    delivery_id=delivery_id,
                    mailbox_id=row["to_mailbox_id"],
                    actor=actor,
                    details={"retry_at": None if terminal else retry_at, "last_error": last_error},
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def heartbeat(self, *, delivery_id: int, claim_token: str, lease_seconds: int = 60) -> bool:
        now = utc_now()
        new_lease_until = utc_after(lease_seconds)
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE deliveries
                SET lease_until = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'claimed' AND claim_token = ?
                """,
                (new_lease_until, now, delivery_id, claim_token),
            ).rowcount
            return updated == 1

    def get_delivery_context(self, delivery_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    d.delivery_id,
                    d.message_id,
                    d.status,
                    dst.address_canonical AS to_address,
                    dst.harness_id AS to_harness_id,
                    src.address_canonical AS from_address,
                    src.harness_id AS from_harness_id
                FROM deliveries d
                JOIN messages m ON m.message_id = d.message_id
                JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                WHERE d.delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "delivery_id": row["delivery_id"],
                "message_id": row["message_id"],
                "status": row["status"],
                "to_address": row["to_address"],
                "to_harness_id": row["to_harness_id"],
                "from_address": row["from_address"],
                "from_harness_id": row["from_harness_id"],
            }

    def get_message(self, message_id: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            return self._load_message(conn, message_id)

    def thread_exists(self, thread_id: str) -> bool:
        with self.connect() as conn:
            normalized_thread_id = self._resolve_thread_id(conn, thread_id=thread_id)
            if normalized_thread_id is None:
                return False
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE thread_id = ?
                LIMIT 1
                """,
                (normalized_thread_id,),
            ).fetchone()
            return row is not None

    def get_thread(
        self,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
    ) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            resolved_thread_id = self._resolve_thread_id(conn, thread_id=thread_id, message_id=message_id)
            if resolved_thread_id is None:
                return None
            messages = self._load_thread_messages(conn, resolved_thread_id)
            if not messages:
                return None
            return {
                "thread_id": resolved_thread_id,
                "message_count": len(messages),
                "messages": messages,
            }

    def get_message_for_harness(self, message_id: str, harness_id: str) -> Optional[dict[str, Any]]:
        harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            allowed = conn.execute(
                """
                SELECT
                    m.message_id
                FROM messages m
                JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                WHERE m.message_id = ?
                  AND (
                        src.harness_id = ?
                        OR EXISTS (
                            SELECT 1
                            FROM deliveries d
                            JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                            WHERE d.message_id = m.message_id
                              AND dst.harness_id = ?
                        )
                      )
                """,
                (message_id, harness_id, harness_id),
            ).fetchone()
            if not allowed:
                return None
            return self._load_message(conn, message_id)

    def get_thread_for_harness(
        self,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
        harness_id: str,
    ) -> Optional[dict[str, Any]]:
        normalized_harness_id = normalize_harness_id(harness_id)
        with self.connect() as conn:
            resolved_thread_id = self._resolve_thread_id(conn, thread_id=thread_id, message_id=message_id)
            if resolved_thread_id is None:
                return None
            rows = conn.execute(
                """
                SELECT
                    m.message_id
                FROM messages m
                JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                WHERE m.thread_id = ?
                  AND (
                        src.harness_id = ?
                        OR EXISTS (
                            SELECT 1
                            FROM deliveries d
                            JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                            WHERE d.message_id = m.message_id
                              AND dst.harness_id = ?
                        )
                      )
                ORDER BY m.created_at ASC, m.message_id ASC
                """,
                (resolved_thread_id, normalized_harness_id, normalized_harness_id),
            ).fetchall()
            visible_message_ids = [str(row["message_id"]) for row in rows]
            if not visible_message_ids:
                return None
            messages = self._load_thread_messages(conn, resolved_thread_id, visible_message_ids=visible_message_ids)
            return {
                "thread_id": resolved_thread_id,
                "message_count": len(messages),
                "messages": messages,
            }

    def get_message_for_addresses(self, message_id: str, addresses: list[str]) -> Optional[dict[str, Any]]:
        canonical_addresses = unique_preserving_order([canonicalize_address(address) for address in addresses])
        if not canonical_addresses:
            return None
        placeholders = ", ".join("?" for _ in canonical_addresses)
        params = [message_id, *canonical_addresses, *canonical_addresses]
        with self.connect() as conn:
            allowed = conn.execute(
                f"""
                SELECT
                    m.message_id
                FROM messages m
                JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                WHERE m.message_id = ?
                  AND (
                        src.address_canonical IN ({placeholders})
                        OR EXISTS (
                            SELECT 1
                            FROM deliveries d
                            JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                            WHERE d.message_id = m.message_id
                              AND dst.address_canonical IN ({placeholders})
                        )
                      )
                """,
                params,
            ).fetchone()
            if not allowed:
                return None
            return self._load_message(conn, message_id)

    def get_thread_for_addresses(
        self,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
        addresses: list[str],
    ) -> Optional[dict[str, Any]]:
        canonical_addresses = unique_preserving_order([canonicalize_address(address) for address in addresses])
        if not canonical_addresses:
            return None
        placeholders = ", ".join("?" for _ in canonical_addresses)
        with self.connect() as conn:
            resolved_thread_id = self._resolve_thread_id(conn, thread_id=thread_id, message_id=message_id)
            if resolved_thread_id is None:
                return None
            rows = conn.execute(
                f"""
                SELECT
                    m.message_id
                FROM messages m
                JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
                WHERE m.thread_id = ?
                  AND (
                        src.address_canonical IN ({placeholders})
                        OR EXISTS (
                            SELECT 1
                            FROM deliveries d
                            JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                            WHERE d.message_id = m.message_id
                              AND dst.address_canonical IN ({placeholders})
                        )
                      )
                ORDER BY m.created_at ASC, m.message_id ASC
                """,
                (resolved_thread_id, *canonical_addresses, *canonical_addresses),
            ).fetchall()
            visible_message_ids = [str(row["message_id"]) for row in rows]
            if not visible_message_ids:
                return None
            messages = self._load_thread_messages(conn, resolved_thread_id, visible_message_ids=visible_message_ids)
            return {
                "thread_id": resolved_thread_id,
                "message_count": len(messages),
                "messages": messages,
            }

    def _load_visible_message_rows_for_mailbox(
        self,
        conn: sqlite3.Connection,
        *,
        mailbox_id: int,
        thread_id: str | None = None,
    ) -> list[sqlite3.Row]:
        params: list[Any] = [mailbox_id, mailbox_id]
        thread_filter = ""
        if thread_id is not None:
            thread_filter = "AND m.thread_id = ?"
            params.append(thread_id)
        return conn.execute(
            f"""
            SELECT
                m.thread_id,
                m.message_id,
                m.created_at,
                m.in_reply_to_message_id,
                src.address_canonical AS from_address
            FROM messages m
            JOIN mailboxes src ON src.mailbox_id = m.from_mailbox_id
            WHERE (
                    m.from_mailbox_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM deliveries d
                        WHERE d.message_id = m.message_id
                          AND d.to_mailbox_id = ?
                    )
                  )
              {thread_filter}
            ORDER BY m.created_at ASC, m.message_id ASC
            """,
            params,
        ).fetchall()

    def get_thread_summaries_for_mailbox(
        self,
        *,
        to_address: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        mailbox = self.resolve_address(to_address)
        if limit == 0:
            return []

        with self.connect() as conn:
            visible_rows = self._load_visible_message_rows_for_mailbox(conn, mailbox_id=mailbox.mailbox_id)
            if not visible_rows:
                return []

            read_rows = conn.execute(
                """
                SELECT
                    r.thread_id,
                    r.last_read_message_id,
                    m.created_at AS last_read_message_at
                FROM mailbox_thread_reads r
                LEFT JOIN messages m ON m.message_id = r.last_read_message_id
                WHERE r.mailbox_id = ?
                """,
                (mailbox.mailbox_id,),
            ).fetchall()

            read_state_by_thread: dict[str, tuple[str, str]] = {}
            for row in read_rows:
                last_read_message_id = row["last_read_message_id"]
                last_read_message_at = row["last_read_message_at"]
                if last_read_message_id is None or last_read_message_at is None:
                    continue
                read_state_by_thread[str(row["thread_id"])] = (
                    str(last_read_message_at),
                    str(last_read_message_id),
                )

            summaries_by_thread: dict[str, dict[str, Any]] = {}
            for row in visible_rows:
                thread_key = str(row["thread_id"])
                latest_key = (str(row["created_at"]), str(row["message_id"]))
                summary = summaries_by_thread.get(thread_key)
                if summary is None:
                    summary = {
                        "thread_id": thread_key,
                        "latest_message_id": str(row["message_id"]),
                        "latest_message_at": str(row["created_at"]),
                        "latest_from_address": str(row["from_address"]),
                        "message_count": 0,
                        "reply_count": 0,
                        "_latest_key": latest_key,
                    }
                    summaries_by_thread[thread_key] = summary
                summary["message_count"] = int(summary["message_count"]) + 1
                if row["in_reply_to_message_id"] is not None:
                    summary["reply_count"] = int(summary["reply_count"]) + 1
                if latest_key >= summary["_latest_key"]:
                    summary["latest_message_id"] = str(row["message_id"])
                    summary["latest_message_at"] = str(row["created_at"])
                    summary["latest_from_address"] = str(row["from_address"])
                    summary["_latest_key"] = latest_key

            summaries: list[dict[str, Any]] = []
            for summary in summaries_by_thread.values():
                latest_key = summary["_latest_key"]
                if not isinstance(latest_key, tuple) or len(latest_key) != 2:
                    continue
                read_key = read_state_by_thread.get(str(summary["thread_id"]))
                summaries.append(
                    {
                        "thread_id": str(summary["thread_id"]),
                        "latest_message_id": str(summary["latest_message_id"]),
                        "latest_message_at": str(summary["latest_message_at"]),
                        "latest_from_address": str(summary["latest_from_address"]),
                        "message_count": int(summary["message_count"]),
                        "reply_count": int(summary["reply_count"]),
                        "unread": read_key is None or latest_key > read_key,
                        "_latest_key": latest_key,
                    }
                )

            summaries.sort(key=lambda item: item["_latest_key"], reverse=True)
            return [
                {
                    "thread_id": str(item["thread_id"]),
                    "latest_message_id": str(item["latest_message_id"]),
                    "latest_message_at": str(item["latest_message_at"]),
                    "latest_from_address": str(item["latest_from_address"]),
                    "message_count": int(item["message_count"]),
                    "reply_count": int(item["reply_count"]),
                    "unread": bool(item["unread"]),
                }
                for item in summaries[:limit]
            ]

    def get_inbox_messages_for_mailbox(
        self,
        *,
        to_address: str,
        limit: int = 20,
        from_address: str | None = None,
        message_type: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        mailbox = self.resolve_address(to_address)
        if limit == 0:
            return []
        unread_only = bool(unread_only)

        from_mailbox_id = None
        if from_address is not None:
            normalized_from_address = str(from_address).strip()
            if normalized_from_address:
                from_mailbox_id = self.resolve_address(normalized_from_address).mailbox_id

        normalized_message_type = None
        if message_type is not None:
            normalized_message_type = str(message_type).strip()
            if not normalized_message_type:
                normalized_message_type = None

        normalized_thread_id = None
        if thread_id is not None:
            normalized_thread_id = str(thread_id).strip()
            if not normalized_thread_id:
                normalized_thread_id = None

        normalized_since = None
        if since is not None:
            normalized_since = str(since).strip()
            if not normalized_since:
                normalized_since = None
            else:
                normalized_since = (
                    parse_utc_timestamp(normalized_since)
                    .astimezone(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )

        with self.connect() as conn:
            params: list[Any] = [mailbox.mailbox_id, mailbox.mailbox_id, mailbox.mailbox_id]
            from_filter = ""
            if from_mailbox_id is not None:
                from_filter = "AND m.from_mailbox_id = ?"
                params.append(from_mailbox_id)
            message_type_filter = ""
            if normalized_message_type is not None:
                message_type_filter = "AND m.message_type = ?"
                params.append(normalized_message_type)
            thread_filter = ""
            if normalized_thread_id is not None:
                thread_filter = "AND m.thread_id = ?"
                params.append(normalized_thread_id)
            since_filter = ""
            if normalized_since is not None:
                since_filter = "AND m.created_at >= ?"
                params.append(normalized_since)
            unread_filter = ""
            if unread_only:
                unread_filter = """
                  AND (
                        last_read.message_id IS NULL
                        OR m.created_at > last_read.created_at
                        OR (m.created_at = last_read.created_at AND m.message_id > last_read.message_id)
                      )
                """
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT DISTINCT
                    m.message_id
                FROM messages m
                LEFT JOIN mailbox_thread_reads reads
                    ON reads.mailbox_id = ?
                   AND reads.thread_id = m.thread_id
                LEFT JOIN messages last_read
                    ON last_read.message_id = reads.last_read_message_id
                WHERE (
                        m.from_mailbox_id = ?
                        OR EXISTS (
                            SELECT 1
                            FROM deliveries d
                            WHERE d.message_id = m.message_id
                              AND d.to_mailbox_id = ?
                        )
                      )
                  {from_filter}
                  {message_type_filter}
                  {thread_filter}
                  {since_filter}
                  {unread_filter}
                ORDER BY m.created_at DESC, m.message_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            if not rows:
                return []
            messages: list[dict[str, Any]] = []
            for row in rows:
                loaded = self._load_message(conn, str(row["message_id"]))
                if loaded is not None:
                    messages.append(loaded)
            return messages

    def mark_thread_read(
        self,
        *,
        thread_id: str,
        to_address: str,
        actor: Optional[str] = None,
    ) -> bool:
        mailbox = self.resolve_address(to_address)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                resolved_thread_id = self._resolve_thread_id(conn, thread_id=thread_id)
                if resolved_thread_id is None:
                    conn.commit()
                    return False
                visible_rows = self._load_visible_message_rows_for_mailbox(
                    conn,
                    mailbox_id=mailbox.mailbox_id,
                    thread_id=resolved_thread_id,
                )
                if not visible_rows:
                    conn.commit()
                    return False
                latest_row = max(
                    visible_rows,
                    key=lambda row: (str(row["created_at"]), str(row["message_id"])),
                )
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO mailbox_thread_reads(mailbox_id, thread_id, last_read_message_id, marked_read_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(mailbox_id, thread_id) DO UPDATE SET
                        last_read_message_id = excluded.last_read_message_id,
                        marked_read_at = excluded.marked_read_at
                    """,
                    (mailbox.mailbox_id, resolved_thread_id, latest_row["message_id"], now),
                )
                self._event(
                    conn,
                    "thread.marked_read",
                    message_id=latest_row["message_id"],
                    mailbox_id=mailbox.mailbox_id,
                    actor=actor or mailbox.address,
                    details={"thread_id": resolved_thread_id, "to_address": mailbox.address},
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def _list_retry_queue(
        self,
        *,
        scope_clause: str,
        scope_params: list[Any],
        to_address: str | None,
        project_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        filters = [
            scope_clause,
            "d.status = 'queued'",
            "d.attempt_count > 0",
            "d.attempt_count < d.max_attempts",
        ]
        params: list[Any] = list(scope_params)
        if to_address is not None:
            filters.append("dst.address_canonical = ?")
            params.append(to_address)
        if project_id is not None:
            filters.append("p.project_id = ?")
            params.append(project_id)
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    d.delivery_id,
                    d.message_id,
                    dst.address_canonical AS to_address,
                    d.status,
                    d.attempt_count,
                    d.max_attempts,
                    d.available_at AS next_retry_at,
                    d.last_error
                FROM deliveries d
                JOIN mailboxes dst ON dst.mailbox_id = d.to_mailbox_id
                JOIN projects p ON p.project_pk = dst.project_pk
                WHERE {" AND ".join(filters)}
                ORDER BY d.available_at ASC, d.delivery_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [
            {
                "delivery_id": int(row["delivery_id"]),
                "message_id": str(row["message_id"]),
                "to": str(row["to_address"]),
                "status": str(row["status"]),
                "attempt_count": int(row["attempt_count"]),
                "max_attempts": int(row["max_attempts"]),
                "next_retry_at": str(row["next_retry_at"]),
                "last_error_summary": summarize_retry_error(
                    row["last_error"],
                    limit=RETRY_QUEUE_ERROR_SUMMARY_LIMIT,
                ),
            }
            for row in rows
        ]

    def list_retry_queue(
        self,
        *,
        to_address: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        canonical_to_address = self.resolve_address(to_address).address if to_address is not None else None
        normalized_project_id = (
            normalize_address_component(project_id, "project_id") if project_id is not None else None
        )
        return self._list_retry_queue(
            scope_clause="1 = 1",
            scope_params=[],
            to_address=canonical_to_address,
            project_id=normalized_project_id,
            limit=limit,
        )

    def list_retry_queue_for_harness(
        self,
        *,
        harness_id: str,
        to_address: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_harness_id = normalize_harness_id(harness_id)
        canonical_to_address = None
        if to_address is not None:
            canonical_to_address = self.resolve_address_for_harness(to_address, normalized_harness_id).address
        normalized_project_id = (
            normalize_address_component(project_id, "project_id") if project_id is not None else None
        )
        return self._list_retry_queue(
            scope_clause="dst.harness_id = ?",
            scope_params=[normalized_harness_id],
            to_address=canonical_to_address,
            project_id=normalized_project_id,
            limit=limit,
        )

    def list_retry_queue_for_addresses(
        self,
        *,
        addresses: list[str],
        to_address: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        canonical_addresses = unique_preserving_order([canonicalize_address(address) for address in addresses])
        if not canonical_addresses:
            return []
        canonical_to_address = canonicalize_address(to_address) if to_address is not None else None
        if canonical_to_address is not None and canonical_to_address not in set(canonical_addresses):
            return []
        normalized_project_id = (
            normalize_address_component(project_id, "project_id") if project_id is not None else None
        )
        placeholders = ", ".join("?" for _ in canonical_addresses)
        return self._list_retry_queue(
            scope_clause=f"dst.address_canonical IN ({placeholders})",
            scope_params=list(canonical_addresses),
            to_address=canonical_to_address,
            project_id=normalized_project_id,
            limit=limit,
        )


def _demo() -> None:
    db_path = Path("/mnt/data/mailbox_demo.sqlite")
    if db_path.exists():
        db_path.unlink()

    mb = SQLiteMailbox(db_path)
    mb.init_db()

    mb.upsert_harness("h1", "Harness 1")
    mb.upsert_project("alpha", "h1", "Alpha")
    mb.upsert_mailbox("sess_42@alpha.h1", mailbox_type="session")
    mb.upsert_mailbox("planner@alpha.h1", mailbox_type="role")

    mb.allow_same_project("alpha", "h1")
    mb.deny_all()

    sent = mb.send(
        from_address="sess_42@alpha.h1",
        to_address="planner@alpha.h1",
        subject="plan",
        message_type="task.plan",
        payload={"goal": "compare mailpit vs sqlite", "depth": "brief"},
        correlation_id="corr-001",
        workflow_id="wf-001",
        idempotency_key="send-001",
    )
    print("SENT", sent)

    claimed = mb.claim(to_address="planner@alpha.h1", consumer_id="consumer.planner.1", lease_seconds=120)
    print("CLAIMED", json.dumps(claimed, ensure_ascii=False, indent=2))

    if claimed:
        ok = mb.ack(delivery_id=claimed["delivery_id"], claim_token=claimed["claim_token"], actor="consumer.planner.1")
        print("ACK", ok)


if __name__ == "__main__":
    _demo()
