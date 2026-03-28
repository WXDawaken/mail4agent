from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mailbox_language_cache import ProtocolRuntimeDiskCache
from mailbox_language_runtime import (
    MailboxRuntimeError,
    compile_protocol_runtime_schema,
    format_protocol_ref,
    normalize_protocol_component,
    parse_protocol_ref,
    resolve_transition_target_state,
    validate_message_payload,
)


def builtin_plaintext_protocol_schema() -> dict[str, Any]:
    return {
        "states": ["Open"],
        "start": "Open",
        "messages": {
            "Text": {
                "required": ["body"],
                "optional": ["subject", "attachments", "sender", "auth"],
                "allow_additional_fields": False,
            }
        },
        "transitions": [
            {"message": "Text", "from": "Open", "to": "Open"},
        ],
    }


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    column: int


@dataclass(frozen=True)
class FieldDecl:
    name: str
    optional: bool
    type_text: str


@dataclass(frozen=True)
class MessageDecl:
    name: str
    fields: tuple[FieldDecl, ...]


@dataclass(frozen=True)
class TransitionDecl:
    message: str
    from_state: str
    to_state: str


@dataclass(frozen=True)
class ProtocolDecl:
    protocol_name: str
    protocol_version: str
    states: tuple[str, ...]
    start_state: str
    messages: tuple[MessageDecl, ...]
    transitions: tuple[TransitionDecl, ...]
    is_builtin: bool = False

    @property
    def protocol_ref(self) -> str:
        return format_protocol_ref(self.protocol_name, self.protocol_version)


@dataclass(frozen=True)
class MailboxDecl:
    name: str
    accepts: tuple[str, ...]
    default_protocol: str | None
    is_shorthand: bool = False


@dataclass(frozen=True)
class ThreadRef:
    name: str
    explicit_thread_handle: bool = False


@dataclass(frozen=True)
class MessageRef:
    protocol_ref: str | None
    message_name: str


@dataclass(frozen=True)
class LetStatement:
    name: str
    expr: "StatementExpr"


@dataclass(frozen=True)
class SendStatement:
    target: ThreadRef
    message_ref: MessageRef
    payload: dict[str, Any]


@dataclass(frozen=True)
class SendTextStatement:
    mailbox_name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SpawnStatement:
    mailbox_name: str
    message_ref: MessageRef
    payload: dict[str, Any]
    from_thread: ThreadRef


@dataclass(frozen=True)
class HandoffStatement:
    from_thread: ThreadRef
    to_thread: ThreadRef


StatementExpr = SendStatement | SendTextStatement | SpawnStatement
Statement = LetStatement | SendStatement | SendTextStatement | SpawnStatement | HandoffStatement


@dataclass(frozen=True)
class SourceProgram:
    protocols: tuple[ProtocolDecl, ...]
    mailboxes: tuple[MailboxDecl, ...]
    statements: tuple[Statement, ...]


class _Parser:
    def __init__(self, source: str):
        self._tokens = _tokenize(source)
        self._index = 0

    def parse_program(self) -> SourceProgram:
        protocols: list[ProtocolDecl] = []
        mailboxes: list[MailboxDecl] = []
        statements: list[Statement] = []
        while not self._at_end():
            if self._peek_keyword("builtin") or self._peek_keyword("protocol"):
                protocols.append(self._parse_protocol_decl())
                continue
            if self._peek_keyword("mailbox"):
                mailboxes.append(self._parse_mailbox_decl())
                continue
            statements.append(self._parse_statement())
        return SourceProgram(tuple(protocols), tuple(mailboxes), tuple(statements))

    def _parse_protocol_decl(self) -> ProtocolDecl:
        is_builtin = self._match_keyword("builtin")
        self._expect_keyword("protocol")
        protocol_name, protocol_version = self._parse_protocol_ref()
        self._expect("{")
        states: list[str] = []
        start_state: str | None = None
        messages: list[MessageDecl] = []
        transitions: list[TransitionDecl] = []
        while not self._match("}"):
            if self._peek_keyword("state"):
                self._advance()
                states.append(self._expect_identifier("state name"))
                self._expect(";")
                continue
            if self._peek_keyword("start"):
                self._advance()
                start_state = self._expect_identifier("start state")
                self._expect(";")
                continue
            if self._peek_keyword("message"):
                messages.append(self._parse_message_decl())
                continue
            if self._peek_keyword("on"):
                transitions.append(self._parse_transition_decl())
                continue
            token = self._peek()
            raise MailboxRuntimeError(
                "E_SOURCE_PARSE_INVALID",
                f"unexpected token in protocol declaration at {token.line}:{token.column}: {token.text!r}",
            )
        if start_state is None:
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"protocol {format_protocol_ref(protocol_name, protocol_version)} is missing a start state",
            )
        return ProtocolDecl(
            protocol_name=protocol_name,
            protocol_version=protocol_version,
            states=tuple(states),
            start_state=start_state,
            messages=tuple(messages),
            transitions=tuple(transitions),
            is_builtin=is_builtin,
        )

    def _parse_message_decl(self) -> MessageDecl:
        self._expect_keyword("message")
        message_name = self._expect_identifier("message name")
        self._expect("{")
        fields: list[FieldDecl] = []
        while not self._match("}"):
            field_name = self._expect_identifier("field name")
            optional = self._match("?")
            self._expect(":")
            type_tokens: list[str] = []
            depth = 0
            while True:
                token = self._peek()
                if token.kind == "EOF":
                    raise MailboxRuntimeError(
                        "E_SOURCE_PARSE_INVALID",
                        f"unterminated field declaration for {message_name}",
                    )
                if token.text == ";" and depth == 0:
                    break
                if token.text in {"[", "<"}:
                    depth += 1
                elif token.text in {"]", ">"} and depth > 0:
                    depth -= 1
                type_tokens.append(self._advance().text)
            self._expect(";")
            fields.append(FieldDecl(field_name, optional, "".join(type_tokens).strip()))
        return MessageDecl(message_name, tuple(fields))

    def _parse_transition_decl(self) -> TransitionDecl:
        self._expect_keyword("on")
        message = self._expect_identifier("message name")
        self._expect_keyword("from")
        from_state = self._expect_identifier("source state")
        self._expect("->")
        to_state = self._expect_identifier("target state")
        self._expect(";")
        return TransitionDecl(message, from_state, to_state)

    def _parse_mailbox_decl(self) -> MailboxDecl:
        self._expect_keyword("mailbox")
        name = self._expect_identifier("mailbox name")
        if self._match(":"):
            accepts = [self._format_protocol_ref(self._parse_protocol_ref())]
            while self._match("|"):
                accepts.append(self._format_protocol_ref(self._parse_protocol_ref()))
            self._expect(";")
            default_protocol = accepts[0] if accepts[0].startswith("PlainText/") else None
            return MailboxDecl(name, tuple(accepts), default_protocol, True)

        self._expect("{")
        accepts: list[str] = []
        default_protocol: str | None = None
        while not self._match("}"):
            if self._peek_keyword("accepts"):
                self._advance()
                self._expect("[")
                accepts.append(self._format_protocol_ref(self._parse_protocol_ref()))
                while self._match(","):
                    accepts.append(self._format_protocol_ref(self._parse_protocol_ref()))
                self._expect("]")
                self._expect(";")
                continue
            if self._peek_keyword("default"):
                self._advance()
                default_protocol = self._format_protocol_ref(self._parse_protocol_ref())
                self._expect(";")
                continue
            token = self._peek()
            raise MailboxRuntimeError(
                "E_SOURCE_PARSE_INVALID",
                f"unexpected token in mailbox declaration at {token.line}:{token.column}: {token.text!r}",
            )
        return MailboxDecl(name, tuple(accepts), default_protocol, False)

    def _parse_statement(self) -> Statement:
        if self._peek_keyword("let"):
            self._advance()
            name = self._expect_identifier("binding name")
            if self._match(":"):
                while not self._match("="):
                    token = self._advance()
                    if token.kind == "EOF":
                        raise MailboxRuntimeError(
                            "E_SOURCE_PARSE_INVALID",
                            f"unterminated type annotation for let {name}",
                        )
            else:
                self._expect("=")
            expr = self._parse_statement_expr()
            self._expect(";")
            return LetStatement(name, expr)

        if self._peek_keyword("handoff"):
            self._advance()
            from_thread = self._parse_thread_ref()
            self._expect("->")
            to_thread = self._parse_thread_ref()
            self._expect(";")
            return HandoffStatement(from_thread, to_thread)

        expr = self._parse_statement_expr()
        self._expect(";")
        return expr

    def _parse_statement_expr(self) -> StatementExpr:
        if self._peek_keyword("send"):
            self._advance()
            if self._match_keyword("text"):
                self._expect_keyword("to")
                mailbox_name = self._expect_identifier("mailbox name")
                payload = {"body": self._parse_literal_value()} if self._peek().kind == "STRING" else self._parse_payload_block()
                return SendTextStatement(mailbox_name, payload)
            self._expect_keyword("to")
            target = self._parse_thread_ref()
            self._expect_keyword("using")
            message_ref = self._parse_message_ref()
            payload = self._parse_payload_block()
            return SendStatement(target, message_ref, payload)

        if self._peek_keyword("spawn"):
            self._advance()
            self._expect_keyword("to")
            mailbox_name = self._expect_identifier("mailbox name")
            self._expect_keyword("using")
            message_ref = self._parse_message_ref()
            payload = self._parse_payload_block()
            self._expect_keyword("from")
            from_thread = self._parse_thread_ref()
            return SpawnStatement(mailbox_name, message_ref, payload, from_thread)

        token = self._peek()
        raise MailboxRuntimeError(
            "E_SOURCE_PARSE_INVALID",
            f"unexpected statement token at {token.line}:{token.column}: {token.text!r}",
        )

    def _parse_thread_ref(self) -> ThreadRef:
        explicit_thread_handle = self._match("#")
        name = self._expect_identifier("thread or mailbox reference")
        return ThreadRef(name, explicit_thread_handle)

    def _parse_message_ref(self) -> MessageRef:
        first = self._expect_identifier("message or protocol name")
        if self._match("/"):
            protocol_version = self._parse_protocol_version()
            self._expect(".")
            message_name = self._expect_identifier("message name")
            return MessageRef(format_protocol_ref(first, protocol_version), message_name)
        return MessageRef(None, first)

    def _parse_protocol_ref(self) -> tuple[str, str]:
        protocol_name = self._expect_identifier("protocol name")
        self._expect("/")
        protocol_version = self._parse_protocol_version()
        return protocol_name, protocol_version

    def _parse_protocol_version(self) -> str:
        token = self._advance()
        if token.kind in {"IDENT", "NUMBER"}:
            return normalize_protocol_component(token.text, "protocol_version")
        if token.kind == "STRING":
            return normalize_protocol_component(json.loads(token.text), "protocol_version")
        raise MailboxRuntimeError(
            "E_SOURCE_PARSE_INVALID",
            f"invalid protocol version token at {token.line}:{token.column}: {token.text!r}",
        )

    def _parse_payload_block(self) -> dict[str, Any]:
        self._expect("{")
        payload: dict[str, Any] = {}
        while not self._match("}"):
            field_name = self._expect_identifier("payload field")
            self._expect(":")
            payload[field_name] = self._parse_value()
            self._expect(";")
        return payload

    def _parse_value(self) -> Any:
        token = self._peek()
        if token.kind in {"STRING", "NUMBER"}:
            return self._parse_literal_value()
        if token.text == "[":
            self._advance()
            items: list[Any] = []
            if not self._match("]"):
                while True:
                    items.append(self._parse_value())
                    if self._match("]"):
                        break
                    self._expect(",")
            return items
        if token.kind == "IDENT":
            self._advance()
            if token.text == "true":
                return True
            if token.text == "false":
                return False
            if token.text == "null":
                return None
            return {"kind": "var_ref", "name": token.text}
        raise MailboxRuntimeError(
            "E_SOURCE_PARSE_INVALID",
            f"unexpected value token at {token.line}:{token.column}: {token.text!r}",
        )

    def _parse_literal_value(self) -> Any:
        token = self._advance()
        if token.kind == "STRING":
            return json.loads(token.text)
        if token.kind == "NUMBER":
            return float(token.text) if "." in token.text else int(token.text)
        raise MailboxRuntimeError(
            "E_SOURCE_PARSE_INVALID",
            f"expected literal value, got {token.text!r}",
        )

    def _format_protocol_ref(self, protocol_parts: tuple[str, str]) -> str:
        return format_protocol_ref(protocol_parts[0], protocol_parts[1])

    def _expect_identifier(self, label: str) -> str:
        token = self._advance()
        if token.kind != "IDENT":
            raise MailboxRuntimeError(
                "E_SOURCE_PARSE_INVALID",
                f"expected {label} at {token.line}:{token.column}, got {token.text!r}",
            )
        return normalize_protocol_component(token.text, label.replace(" ", "_"))

    def _peek(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        if token.kind != "EOF":
            self._index += 1
        return token

    def _match(self, expected: str) -> bool:
        token = self._peek()
        if token.text != expected:
            return False
        self._advance()
        return True

    def _expect(self, expected: str) -> None:
        token = self._advance()
        if token.text != expected:
            raise MailboxRuntimeError(
                "E_SOURCE_PARSE_INVALID",
                f"expected {expected!r} at {token.line}:{token.column}, got {token.text!r}",
            )

    def _peek_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return token.kind == "IDENT" and token.text == keyword

    def _match_keyword(self, keyword: str) -> bool:
        if not self._peek_keyword(keyword):
            return False
        self._advance()
        return True

    def _expect_keyword(self, keyword: str) -> None:
        token = self._advance()
        if token.kind != "IDENT" or token.text != keyword:
            raise MailboxRuntimeError(
                "E_SOURCE_PARSE_INVALID",
                f"expected keyword {keyword!r} at {token.line}:{token.column}, got {token.text!r}",
            )

    def _at_end(self) -> bool:
        return self._peek().kind == "EOF"


@dataclass
class _StaticThreadBinding:
    protocol_ref: str
    mailbox_name: str
    mailbox_address: str | None
    state: str


def lower_source_program(
    source: str,
    *,
    mailbox_addresses: dict[str, str] | None = None,
    inputs: dict[str, Any] | None = None,
    from_address: str | None = None,
    cache_dir: str | None = None,
) -> dict[str, Any]:
    program = _Parser(source).parse_program()
    normalized_inputs = _normalize_inputs(inputs)
    normalized_mailbox_addresses = _normalize_mailbox_addresses(mailbox_addresses)
    protocol_entries = _build_protocol_entries(program, cache_dir=cache_dir)
    protocol_schemas = {entry["protocol"]: entry["schema"] for entry in protocol_entries}
    mailbox_entries = _build_mailbox_entries(
        program,
        protocol_schemas=protocol_schemas,
        mailbox_addresses=normalized_mailbox_addresses,
    )
    mailbox_table = {entry["mailbox"]: entry for entry in mailbox_entries}
    operations, thread_bindings = _lower_statements(
        program,
        protocol_schemas=protocol_schemas,
        mailbox_table=mailbox_table,
        inputs=normalized_inputs,
        from_address=from_address,
    )
    return {
        "kind": "dsl_program_lowered",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "protocols": protocol_entries,
        "mailboxes": mailbox_entries,
        "operations": operations,
        "thread_bindings": thread_bindings,
        "requires_from_address": any(item["kind"] == "message_operation" for item in operations),
    }


def _build_protocol_entries(program: SourceProgram, *, cache_dir: str | None) -> list[dict[str, Any]]:
    protocol_decls: dict[str, ProtocolDecl] = {}
    for decl in program.protocols:
        if decl.protocol_ref in protocol_decls:
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"duplicate protocol declaration: {decl.protocol_ref}",
            )
        protocol_decls[decl.protocol_ref] = decl

    referenced_protocols = _collect_referenced_protocols(program)
    if "PlainText/v1" in referenced_protocols and "PlainText/v1" not in protocol_decls:
        protocol_decls["PlainText/v1"] = ProtocolDecl(
            protocol_name="PlainText",
            protocol_version="v1",
            states=("Open",),
            start_state="Open",
            messages=(
                MessageDecl(
                    name="Text",
                    fields=(
                        FieldDecl("subject", True, "String"),
                        FieldDecl("body", False, "String"),
                        FieldDecl("attachments", True, "[Attachment]"),
                        FieldDecl("sender", True, "Principal"),
                        FieldDecl("auth", True, "AuthContext"),
                    ),
                ),
            ),
            transitions=(TransitionDecl("Text", "Open", "Open"),),
            is_builtin=True,
        )

    protocol_entries: list[dict[str, Any]] = []
    for protocol_ref in sorted(protocol_decls.keys()):
        decl = protocol_decls[protocol_ref]
        schema = _protocol_decl_to_schema(decl)
        protocol_name, protocol_version = parse_protocol_ref(protocol_ref)
        if cache_dir:
            cached = ProtocolRuntimeDiskCache(cache_dir).load_or_compile(
                protocol_name=protocol_name,
                protocol_version=protocol_version,
                schema=schema,
            )
            compiled_artifact = cached.artifact
            cache_hit = cached.cache_hit
            cache_path = str(cached.cache_path)
            source_sha256 = cached.source_sha256
        else:
            compiled_artifact = compile_protocol_runtime_schema(
                schema,
                protocol_name=protocol_name,
                protocol_version=protocol_version,
            )
            cache_hit = False
            cache_path = None
            source_sha256 = f"nocache:{protocol_ref}"
        protocol_entries.append(
            {
                "kind": "protocol_schema",
                "protocol": protocol_ref,
                "schema": schema,
                "compiled_artifact": compiled_artifact,
                "cache_hit": cache_hit,
                "cache_path": cache_path,
                "source_sha256": source_sha256,
                "is_builtin": decl.is_builtin,
            }
        )
    return protocol_entries


def _build_mailbox_entries(
    program: SourceProgram,
    *,
    protocol_schemas: dict[str, dict[str, Any]],
    mailbox_addresses: dict[str, str],
) -> list[dict[str, Any]]:
    mailbox_entries: list[dict[str, Any]] = []
    seen_mailboxes: set[str] = set()
    for decl in program.mailboxes:
        if decl.name in seen_mailboxes:
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"duplicate mailbox declaration: {decl.name}",
            )
        seen_mailboxes.add(decl.name)
        if not decl.accepts:
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"mailbox {decl.name} must declare at least one accepted protocol",
            )
        accepts: list[str] = []
        plain_text_refs: list[str] = []
        for protocol_ref in decl.accepts:
            if protocol_ref not in protocol_schemas:
                raise MailboxRuntimeError(
                    "E_SOURCE_DECLARATION_INVALID",
                    f"mailbox {decl.name} references unknown protocol {protocol_ref}",
                )
            if protocol_ref in accepts:
                raise MailboxRuntimeError(
                    "E_SOURCE_DECLARATION_INVALID",
                    f"mailbox {decl.name} repeats accepted protocol {protocol_ref}",
                )
            accepts.append(protocol_ref)
            if protocol_ref.startswith("PlainText/"):
                plain_text_refs.append(protocol_ref)
        if len(plain_text_refs) > 1:
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"mailbox {decl.name} accepts more than one PlainText version",
            )
        if decl.default_protocol is not None:
            if decl.default_protocol not in accepts:
                raise MailboxRuntimeError(
                    "E_SOURCE_DECLARATION_INVALID",
                    f"mailbox {decl.name} default protocol must appear in accepts",
                )
            if not decl.default_protocol.startswith("PlainText/"):
                raise MailboxRuntimeError(
                    "E_SOURCE_DECLARATION_INVALID",
                    f"mailbox {decl.name} default protocol must be PlainText/* in v0",
                )
        if decl.is_shorthand and len(accepts) > 1 and not accepts[0].startswith("PlainText/"):
            raise MailboxRuntimeError(
                "E_SOURCE_DECLARATION_INVALID",
                f"mailbox shorthand for {decl.name} must start with PlainText/* when accepting multiple protocols",
            )
        mailbox_entries.append(
            {
                "kind": "mailbox_binding",
                "mailbox": decl.name,
                "address": mailbox_addresses.get(decl.name),
                "accepts": accepts,
                "default_protocol": decl.default_protocol,
            }
        )
    return mailbox_entries


def _lower_statements(
    program: SourceProgram,
    *,
    protocol_schemas: dict[str, dict[str, Any]],
    mailbox_table: dict[str, dict[str, Any]],
    inputs: dict[str, Any],
    from_address: str | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    operations: list[dict[str, Any]] = []
    thread_env: dict[str, _StaticThreadBinding] = {}
    for statement in program.statements:
        bind_name: str | None = None
        expr: Statement | StatementExpr = statement
        if isinstance(statement, LetStatement):
            bind_name = statement.name
            if bind_name in thread_env:
                raise MailboxRuntimeError(
                    "E_SOURCE_DECLARATION_INVALID",
                    f"thread binding {bind_name} is already defined",
                )
            expr = statement.expr

        if isinstance(expr, SendTextStatement):
            mailbox_entry = _require_mailbox(mailbox_table, expr.mailbox_name)
            protocol_ref = _resolve_plaintext_protocol_for_mailbox(mailbox_entry)
            payload = _resolve_payload(expr.payload, inputs)
            schema = protocol_schemas[protocol_ref]
            validate_message_payload(
                protocol_ref=protocol_ref,
                msg_type="Text",
                payload=payload,
                message_schema=schema["messages"]["Text"],
            )
            next_state = resolve_transition_target_state(
                protocol_ref=protocol_ref,
                schema=schema,
                from_state=str(schema["start"]),
                msg_type="Text",
            )
            operations.append(
                {
                    "kind": "message_operation",
                    "bind": bind_name,
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "send",
                        "target_kind": "mailbox",
                        "mailbox": expr.mailbox_name,
                        "to_address": mailbox_entry.get("address"),
                        "from_address": from_address,
                        "protocol": protocol_ref,
                        "message": "Text",
                        "payload": payload,
                    },
                }
            )
            if bind_name is not None:
                thread_env[bind_name] = _StaticThreadBinding(
                    protocol_ref=protocol_ref,
                    mailbox_name=expr.mailbox_name,
                    mailbox_address=mailbox_entry.get("address"),
                    state=next_state,
                )
            continue

        if isinstance(expr, SendStatement):
            if expr.target.explicit_thread_handle:
                raise MailboxRuntimeError(
                    "E_SOURCE_TYPE_INVALID",
                    f"explicit thread handles like #{expr.target.name} are not supported in the first DSL slice",
                )
            if expr.target.name in mailbox_table:
                protocol_ref = _require_qualified_message_ref(expr.message_ref, context=f"mailbox {expr.target.name}")
                mailbox_entry = mailbox_table[expr.target.name]
                if protocol_ref not in mailbox_entry["accepts"]:
                    raise MailboxRuntimeError(
                        "E_MAILBOX_PROTOCOL_NOT_ACCEPTED",
                        f"mailbox {expr.target.name} does not accept {protocol_ref}",
                    )
                payload = _resolve_payload(expr.payload, inputs)
                schema = protocol_schemas[protocol_ref]
                msg_type = expr.message_ref.message_name
                message_schema = schema["messages"].get(msg_type)
                if message_schema is None:
                    raise MailboxRuntimeError(
                        "E_MESSAGE_UNKNOWN",
                        f"message {msg_type} is not declared in {protocol_ref}",
                    )
                validate_message_payload(protocol_ref=protocol_ref, msg_type=msg_type, payload=payload, message_schema=message_schema)
                next_state = resolve_transition_target_state(
                    protocol_ref=protocol_ref,
                    schema=schema,
                    from_state=str(schema["start"]),
                    msg_type=msg_type,
                )
                operations.append(
                    {
                        "kind": "message_operation",
                        "bind": bind_name,
                        "artifact": {
                            "kind": "message_envelope",
                            "op": "send",
                            "target_kind": "mailbox",
                            "mailbox": expr.target.name,
                            "to_address": mailbox_entry.get("address"),
                            "from_address": from_address,
                            "protocol": protocol_ref,
                            "message": msg_type,
                            "payload": payload,
                        },
                    }
                )
                if bind_name is not None:
                    thread_env[bind_name] = _StaticThreadBinding(
                        protocol_ref=protocol_ref,
                        mailbox_name=expr.target.name,
                        mailbox_address=mailbox_entry.get("address"),
                        state=next_state,
                    )
                continue

            thread_binding = _require_thread_binding(thread_env, expr.target.name)
            if bind_name is not None:
                raise MailboxRuntimeError(
                    "E_SOURCE_TYPE_INVALID",
                    "send to an existing thread returns unit and cannot be bound with let in the first DSL slice",
                )
            protocol_ref = _resolve_thread_message_protocol(expr.message_ref, thread_binding.protocol_ref)
            if protocol_ref != thread_binding.protocol_ref:
                raise MailboxRuntimeError(
                    "E_THREAD_PROTOCOL_MISMATCH",
                    f"thread {expr.target.name} is bound to {thread_binding.protocol_ref}, not {protocol_ref}",
                )
            payload = _resolve_payload(expr.payload, inputs)
            schema = protocol_schemas[protocol_ref]
            msg_type = expr.message_ref.message_name
            message_schema = schema["messages"].get(msg_type)
            if message_schema is None:
                raise MailboxRuntimeError(
                    "E_MESSAGE_UNKNOWN",
                    f"message {msg_type} is not declared in {protocol_ref}",
                )
            validate_message_payload(protocol_ref=protocol_ref, msg_type=msg_type, payload=payload, message_schema=message_schema)
            next_state = resolve_transition_target_state(
                protocol_ref=protocol_ref,
                schema=schema,
                from_state=thread_binding.state,
                msg_type=msg_type,
            )
            operations.append(
                {
                    "kind": "message_operation",
                    "bind": None,
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "send",
                        "target_kind": "thread",
                        "thread_var": expr.target.name,
                        "to_address": thread_binding.mailbox_address,
                        "mailbox": thread_binding.mailbox_name,
                        "from_address": from_address,
                        "protocol": protocol_ref,
                        "message": msg_type,
                        "payload": payload,
                    },
                }
            )
            thread_binding.state = next_state
            continue

        if isinstance(expr, SpawnStatement):
            if bind_name is None:
                raise MailboxRuntimeError("E_SOURCE_TYPE_INVALID", "spawn expressions must be bound with let in the first DSL slice")
            if expr.from_thread.explicit_thread_handle:
                raise MailboxRuntimeError(
                    "E_SOURCE_TYPE_INVALID",
                    f"explicit thread handles like #{expr.from_thread.name} are not supported in the first DSL slice",
                )
            mailbox_entry = _require_mailbox(mailbox_table, expr.mailbox_name)
            protocol_ref = _require_qualified_message_ref(expr.message_ref, context=f"mailbox {expr.mailbox_name}")
            if protocol_ref not in mailbox_entry["accepts"]:
                raise MailboxRuntimeError(
                    "E_MAILBOX_PROTOCOL_NOT_ACCEPTED",
                    f"mailbox {expr.mailbox_name} does not accept {protocol_ref}",
                )
            parent_binding = _require_thread_binding(thread_env, expr.from_thread.name)
            payload = _resolve_payload(expr.payload, inputs)
            schema = protocol_schemas[protocol_ref]
            msg_type = expr.message_ref.message_name
            message_schema = schema["messages"].get(msg_type)
            if message_schema is None:
                raise MailboxRuntimeError("E_MESSAGE_UNKNOWN", f"message {msg_type} is not declared in {protocol_ref}")
            validate_message_payload(protocol_ref=protocol_ref, msg_type=msg_type, payload=payload, message_schema=message_schema)
            next_state = resolve_transition_target_state(
                protocol_ref=protocol_ref,
                schema=schema,
                from_state=str(schema["start"]),
                msg_type=msg_type,
            )
            operations.append(
                {
                    "kind": "message_operation",
                    "bind": bind_name,
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "spawn",
                        "target_kind": "mailbox",
                        "mailbox": expr.mailbox_name,
                        "to_address": mailbox_entry.get("address"),
                        "from_address": from_address,
                        "protocol": protocol_ref,
                        "message": msg_type,
                        "payload": payload,
                        "parent_thread_var": expr.from_thread.name,
                        "parent_protocol": parent_binding.protocol_ref,
                    },
                }
            )
            thread_env[bind_name] = _StaticThreadBinding(
                protocol_ref=protocol_ref,
                mailbox_name=expr.mailbox_name,
                mailbox_address=mailbox_entry.get("address"),
                state=next_state,
            )
            continue

        if isinstance(expr, HandoffStatement):
            if expr.from_thread.explicit_thread_handle or expr.to_thread.explicit_thread_handle:
                raise MailboxRuntimeError("E_SOURCE_TYPE_INVALID", "explicit thread handles are not supported in the first DSL slice")
            _require_thread_binding(thread_env, expr.from_thread.name)
            _require_thread_binding(thread_env, expr.to_thread.name)
            operations.append(
                {
                    "kind": "handoff_operation",
                    "artifact": {
                        "kind": "handoff_event",
                        "from_thread_var": expr.from_thread.name,
                        "to_thread_var": expr.to_thread.name,
                    },
                }
            )
            continue

        raise AssertionError(f"unexpected statement type: {type(expr)!r}")

    thread_bindings = {
        name: {
            "protocol": binding.protocol_ref,
            "mailbox": binding.mailbox_name,
            "address": binding.mailbox_address,
            "state": binding.state,
        }
        for name, binding in thread_env.items()
    }
    return operations, thread_bindings


def _require_mailbox(mailbox_table: dict[str, dict[str, Any]], mailbox_name: str) -> dict[str, Any]:
    mailbox = mailbox_table.get(mailbox_name)
    if mailbox is None:
        raise MailboxRuntimeError("E_SOURCE_REFERENCE_UNKNOWN", f"unknown mailbox reference: {mailbox_name}")
    return mailbox


def _require_thread_binding(thread_env: dict[str, _StaticThreadBinding], name: str) -> _StaticThreadBinding:
    binding = thread_env.get(name)
    if binding is None:
        raise MailboxRuntimeError("E_SOURCE_REFERENCE_UNKNOWN", f"unknown thread binding: {name}")
    return binding


def _require_qualified_message_ref(message_ref: MessageRef, *, context: str) -> str:
    if message_ref.protocol_ref is None:
        raise MailboxRuntimeError(
            "E_SOURCE_TYPE_INVALID",
            f"{context} requires a fully-qualified Protocol/version.Message reference",
        )
    return message_ref.protocol_ref


def _resolve_thread_message_protocol(message_ref: MessageRef, thread_protocol_ref: str) -> str:
    return message_ref.protocol_ref or thread_protocol_ref


def _resolve_plaintext_protocol_for_mailbox(mailbox_entry: dict[str, Any]) -> str:
    plain_text_refs = [item for item in mailbox_entry["accepts"] if str(item).startswith("PlainText/")]
    if len(plain_text_refs) != 1:
        raise MailboxRuntimeError(
            "E_MAILBOX_TEXT_NOT_ACCEPTED",
            f"mailbox {mailbox_entry['mailbox']} does not accept exactly one PlainText protocol",
        )
    return str(plain_text_refs[0])


def _resolve_payload(payload: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return {key: _resolve_value(value, inputs) for key, value in payload.items()}


def _resolve_value(value: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if value.get("kind") == "var_ref":
            name = str(value.get("name") or "")
            if name not in inputs:
                raise MailboxRuntimeError("E_SOURCE_VALUE_UNKNOWN", f"unknown input variable: {name}")
            return inputs[name]
        return {key: _resolve_value(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, inputs) for item in value]
    return value


def _normalize_inputs(inputs: dict[str, Any] | None) -> dict[str, Any]:
    if inputs is None:
        return {}
    if not isinstance(inputs, dict):
        raise MailboxRuntimeError("E_SOURCE_TYPE_INVALID", "artifact.inputs must be a JSON object when provided")
    return {normalize_protocol_component(str(key), "input_name"): value for key, value in inputs.items()}


def _normalize_mailbox_addresses(mailbox_addresses: dict[str, str] | None) -> dict[str, str]:
    if mailbox_addresses is None:
        return {}
    if not isinstance(mailbox_addresses, dict):
        raise MailboxRuntimeError(
            "E_SOURCE_TYPE_INVALID",
            "artifact.mailbox_addresses must be a JSON object when provided",
        )
    normalized: dict[str, str] = {}
    for key, value in mailbox_addresses.items():
        if not isinstance(value, str) or not value.strip():
            raise MailboxRuntimeError(
                "E_SOURCE_TYPE_INVALID",
                f"mailbox address mapping for {key!r} must be a non-empty string",
            )
        normalized[normalize_protocol_component(str(key), "mailbox_name")] = value.strip()
    return normalized


def _protocol_decl_to_schema(decl: ProtocolDecl) -> dict[str, Any]:
    messages: dict[str, dict[str, Any]] = {}
    for message in decl.messages:
        messages[message.name] = {
            "required": [field.name for field in message.fields if not field.optional],
            "optional": [field.name for field in message.fields if field.optional],
            "allow_additional_fields": False,
        }
    return {
        "states": list(decl.states),
        "start": decl.start_state,
        "messages": messages,
        "transitions": [
            {"message": item.message, "from": item.from_state, "to": item.to_state}
            for item in decl.transitions
        ],
    }


def _collect_referenced_protocols(program: SourceProgram) -> set[str]:
    refs = {decl.protocol_ref for decl in program.protocols}
    for mailbox in program.mailboxes:
        refs.update(mailbox.accepts)
        if mailbox.default_protocol is not None:
            refs.add(mailbox.default_protocol)
    for statement in program.statements:
        expr: Statement | StatementExpr = statement.expr if isinstance(statement, LetStatement) else statement
        if isinstance(expr, SendStatement) and expr.message_ref.protocol_ref is not None:
            refs.add(expr.message_ref.protocol_ref)
        elif isinstance(expr, SpawnStatement) and expr.message_ref.protocol_ref is not None:
            refs.add(expr.message_ref.protocol_ref)
        elif isinstance(expr, SendTextStatement):
            refs.add("PlainText/v1")
    return refs


def _tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    line = 1
    column = 1
    length = len(source)
    while index < length:
        ch = source[index]
        if ch in {" ", "\t", "\r"}:
            index += 1
            column += 1
            continue
        if ch == "\n":
            index += 1
            line += 1
            column = 1
            continue
        if source.startswith("->", index):
            tokens.append(Token("SYMBOL", "->", line, column))
            index += 2
            column += 2
            continue
        if ch == '"':
            start_line = line
            start_column = column
            end_index = index + 1
            escaped = False
            while end_index < length:
                current = source[end_index]
                if current == "\n" and not escaped:
                    raise MailboxRuntimeError(
                        "E_SOURCE_PARSE_INVALID",
                        f"unterminated string literal at {start_line}:{start_column}",
                    )
                if current == '"' and not escaped:
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                end_index += 1
            if end_index >= length or source[end_index] != '"':
                raise MailboxRuntimeError(
                    "E_SOURCE_PARSE_INVALID",
                    f"unterminated string literal at {start_line}:{start_column}",
                )
            text = source[index : end_index + 1]
            tokens.append(Token("STRING", text, start_line, start_column))
            index = end_index + 1
            column += len(text)
            continue
        if ch in {"{", "}", "[", "]", "(", ")", ";", ":", ",", ".", "?", "=", "|", "/", "<", ">", "#"}:
            tokens.append(Token("SYMBOL", ch, line, column))
            index += 1
            column += 1
            continue
        if ch.isdigit() or (ch == "-" and index + 1 < length and source[index + 1].isdigit()):
            start = index
            start_column = column
            index += 1
            while index < length and (source[index].isdigit() or source[index] == "."):
                index += 1
            text = source[start:index]
            tokens.append(Token("NUMBER", text, line, start_column))
            column += len(text)
            continue
        if ch.isalpha() or ch == "_":
            start = index
            start_column = column
            index += 1
            while index < length and (source[index].isalnum() or source[index] == "_"):
                index += 1
            text = source[start:index]
            tokens.append(Token("IDENT", text, line, start_column))
            column += len(text)
            continue
        raise MailboxRuntimeError("E_SOURCE_PARSE_INVALID", f"unexpected character at {line}:{column}: {ch!r}")
    tokens.append(Token("EOF", "", line, column))
    return tokens
