from __future__ import annotations

import re
from typing import Any


PROTOCOL_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
COMPILED_PROTOCOL_RUNTIME_KIND = "mailbox_language_compiled_protocol_runtime"
COMPILED_PROTOCOL_MESSAGE_KIND = "mailbox_language_compiled_protocol_message"


class MailboxRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def normalize_protocol_component(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if not PROTOCOL_COMPONENT_RE.match(normalized):
        raise ValueError(f"{field_name} must use only [A-Za-z0-9._-]")
    return normalized


def parse_protocol_ref(protocol_ref: str) -> tuple[str, str]:
    if not isinstance(protocol_ref, str):
        raise ValueError("protocol must be a string")
    normalized = protocol_ref.strip()
    if not normalized:
        raise ValueError("protocol must not be empty")
    protocol_name, separator, protocol_version = normalized.partition("/")
    if not separator:
        raise ValueError("protocol must use the form Name/version")
    return (
        normalize_protocol_component(protocol_name, "protocol_name"),
        normalize_protocol_component(protocol_version, "protocol_version"),
    )


def format_protocol_ref(protocol_name: str, protocol_version: str) -> str:
    return (
        f"{normalize_protocol_component(protocol_name, 'protocol_name')}/"
        f"{normalize_protocol_component(protocol_version, 'protocol_version')}"
    )


def validate_protocol_runtime_schema(
    schema: Any,
    *,
    protocol_name: str,
    protocol_version: str,
) -> dict[str, Any]:
    normalized_name = normalize_protocol_component(protocol_name, "protocol_name")
    normalized_version = normalize_protocol_component(protocol_version, "protocol_version")
    protocol_ref = format_protocol_ref(normalized_name, normalized_version)
    if not isinstance(schema, dict):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol schema must be a JSON object for {protocol_ref}",
        )
    states = schema.get("states")
    start_state = schema.get("start")
    messages = schema.get("messages")
    transitions = schema.get("transitions")
    if not isinstance(states, list) or not all(isinstance(item, str) and item.strip() for item in states):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol states missing or invalid for {protocol_ref}",
        )
    if not isinstance(start_state, str) or not start_state.strip():
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol start state missing for {protocol_ref}",
        )
    if start_state not in states:
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol start state {start_state!r} is not declared for {protocol_ref}",
        )
    if not isinstance(messages, dict):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol messages missing or invalid for {protocol_ref}",
        )
    if not isinstance(transitions, list):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"protocol transitions missing or invalid for {protocol_ref}",
        )
    return schema


def compile_protocol_runtime_schema(
    schema: Any,
    *,
    protocol_name: str,
    protocol_version: str,
) -> dict[str, Any]:
    validated_schema = validate_protocol_runtime_schema(
        schema,
        protocol_name=protocol_name,
        protocol_version=protocol_version,
    )
    normalized_name = normalize_protocol_component(protocol_name, "protocol_name")
    normalized_version = normalize_protocol_component(protocol_version, "protocol_version")
    protocol_ref = format_protocol_ref(normalized_name, normalized_version)

    states = [str(item).strip() for item in validated_schema["states"]]
    start_state = str(validated_schema["start"]).strip()
    messages = validated_schema["messages"]
    transitions = validated_schema["transitions"]

    compiled_messages: dict[str, dict[str, Any]] = {}
    for raw_msg_type, message_schema in messages.items():
        msg_type = normalize_protocol_component(raw_msg_type, "msg_type")
        required_fields: set[str] = set()
        optional_fields: set[str] = set()
        allow_additional_fields = True
        if message_schema is not None:
            if not isinstance(message_schema, dict):
                raise MailboxRuntimeError(
                    "E_PROTOCOL_SCHEMA_INVALID",
                    f"message schema for {protocol_ref}.{msg_type} must be a JSON object or null",
                )
            allow_additional_fields = bool(message_schema.get("allow_additional_fields", True))
            if "required" in message_schema:
                required = message_schema.get("required")
                if not isinstance(required, list) or not all(
                    isinstance(item, str) and item.strip() for item in required
                ):
                    raise MailboxRuntimeError(
                        "E_PROTOCOL_SCHEMA_INVALID",
                        f"message required fields for {protocol_ref}.{msg_type} must be a list of strings",
                    )
                required_fields.update(item.strip() for item in required)
            if "optional" in message_schema:
                optional = message_schema.get("optional")
                if not isinstance(optional, list) or not all(
                    isinstance(item, str) and item.strip() for item in optional
                ):
                    raise MailboxRuntimeError(
                        "E_PROTOCOL_SCHEMA_INVALID",
                        f"message optional fields for {protocol_ref}.{msg_type} must be a list of strings",
                    )
                optional_fields.update(item.strip() for item in optional)
            if "fields" in message_schema:
                fields = message_schema.get("fields")
                if not isinstance(fields, dict):
                    raise MailboxRuntimeError(
                        "E_PROTOCOL_SCHEMA_INVALID",
                        f"message fields for {protocol_ref}.{msg_type} must be a JSON object",
                    )
                for field_name, field_schema in fields.items():
                    if not isinstance(field_name, str) or not field_name.strip():
                        raise MailboxRuntimeError(
                            "E_PROTOCOL_SCHEMA_INVALID",
                            f"message field names for {protocol_ref}.{msg_type} must be non-empty strings",
                        )
                    normalized_field_name = field_name.strip()
                    if field_schema is None:
                        optional_fields.add(normalized_field_name)
                        continue
                    if not isinstance(field_schema, dict):
                        raise MailboxRuntimeError(
                            "E_PROTOCOL_SCHEMA_INVALID",
                            f"message field schema for {protocol_ref}.{msg_type}.{normalized_field_name} must be an object or null",
                        )
                    if bool(field_schema.get("required")):
                        required_fields.add(normalized_field_name)
                    else:
                        optional_fields.add(normalized_field_name)
        compiled_messages[msg_type] = {
            "kind": COMPILED_PROTOCOL_MESSAGE_KIND,
            "msg_type": msg_type,
            "required_fields": sorted(required_fields),
            "optional_fields": sorted(optional_fields),
            "allow_additional_fields": allow_additional_fields,
        }

    transition_table: dict[str, dict[str, str]] = {}
    for item in transitions:
        if not isinstance(item, dict):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transitions for {protocol_ref} must contain only objects",
            )
        raw_message = item.get("message")
        raw_from_state = item.get("from")
        raw_to_state = item.get("to")
        if not isinstance(raw_message, str) or not raw_message.strip():
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition message for {protocol_ref} is invalid",
            )
        if not isinstance(raw_from_state, str) or not raw_from_state.strip():
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition source state for {protocol_ref} is invalid",
            )
        if not isinstance(raw_to_state, str) or not raw_to_state.strip():
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition target for {protocol_ref}.{raw_message.strip()} from {raw_from_state.strip()} is invalid",
            )
        msg_type = normalize_protocol_component(raw_message, "msg_type")
        from_state = raw_from_state.strip()
        to_state = raw_to_state.strip()
        if msg_type not in compiled_messages:
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition references undeclared message {msg_type} in {protocol_ref}",
            )
        if from_state not in states:
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition references undeclared source state {from_state!r} in {protocol_ref}",
            )
        if to_state not in states:
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition references undeclared target state {to_state!r} in {protocol_ref}",
            )
        per_state = transition_table.setdefault(from_state, {})
        if msg_type in per_state and per_state[msg_type] != to_state:
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"message {msg_type} from state {from_state} is ambiguous in {protocol_ref}",
            )
        per_state[msg_type] = to_state

    return {
        "kind": COMPILED_PROTOCOL_RUNTIME_KIND,
        "protocol": protocol_ref,
        "protocol_name": normalized_name,
        "protocol_version": normalized_version,
        "states": states,
        "start_state": start_state,
        "messages": compiled_messages,
        "transition_table": transition_table,
    }


def is_compiled_protocol_runtime_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("kind") == COMPILED_PROTOCOL_RUNTIME_KIND


def lookup_message_schema(schema: dict[str, Any], *, msg_type: str) -> Any:
    normalized_msg_type = normalize_protocol_component(msg_type, "msg_type")
    if is_compiled_protocol_runtime_schema(schema):
        messages = schema.get("messages")
        if not isinstance(messages, dict):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"compiled protocol messages missing or invalid for {schema.get('protocol') or 'unknown protocol'}",
            )
        return messages.get(normalized_msg_type)
    messages = schema.get("messages")
    if not isinstance(messages, dict):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            "protocol messages missing or invalid",
        )
    return messages.get(normalized_msg_type)


def validate_message_payload(
    *,
    protocol_ref: str,
    msg_type: str,
    payload: dict[str, Any],
    message_schema: Any,
) -> None:
    if message_schema is None:
        return
    if not isinstance(message_schema, dict):
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"message schema for {protocol_ref}.{msg_type} must be a JSON object or null",
        )
    if message_schema.get("kind") == COMPILED_PROTOCOL_MESSAGE_KIND:
        required = message_schema.get("required_fields")
        optional = message_schema.get("optional_fields")
        if not isinstance(required, list) or not isinstance(optional, list):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"compiled message schema for {protocol_ref}.{msg_type} is invalid",
            )
        required_fields = {str(item).strip() for item in required if isinstance(item, str) and item.strip()}
        optional_fields = {str(item).strip() for item in optional if isinstance(item, str) and item.strip()}
        missing_fields = sorted(field for field in required_fields if field not in payload)
        if missing_fields:
            raise MailboxRuntimeError(
                "E_PAYLOAD_SCHEMA_INVALID",
                f"payload missing required fields for {protocol_ref}.{msg_type}: {', '.join(missing_fields)}",
            )
        allow_additional_fields = bool(message_schema.get("allow_additional_fields", True))
        if not allow_additional_fields:
            allowed_fields = required_fields | optional_fields
            extra_fields = sorted(key for key in payload.keys() if key not in allowed_fields)
            if extra_fields:
                raise MailboxRuntimeError(
                    "E_PAYLOAD_SCHEMA_INVALID",
                    f"payload has unexpected fields for {protocol_ref}.{msg_type}: {', '.join(extra_fields)}",
                )
        return
    required_fields: set[str] = set()
    optional_fields: set[str] = set()
    if "required" in message_schema:
        required = message_schema.get("required")
        if not isinstance(required, list) or not all(isinstance(item, str) and item.strip() for item in required):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"message required fields for {protocol_ref}.{msg_type} must be a list of strings",
            )
        required_fields.update(item.strip() for item in required)
    if "optional" in message_schema:
        optional = message_schema.get("optional")
        if not isinstance(optional, list) or not all(isinstance(item, str) and item.strip() for item in optional):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"message optional fields for {protocol_ref}.{msg_type} must be a list of strings",
            )
        optional_fields.update(item.strip() for item in optional)
    if "fields" in message_schema:
        fields = message_schema.get("fields")
        if not isinstance(fields, dict):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"message fields for {protocol_ref}.{msg_type} must be a JSON object",
            )
        for field_name, field_schema in fields.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise MailboxRuntimeError(
                    "E_PROTOCOL_SCHEMA_INVALID",
                    f"message field names for {protocol_ref}.{msg_type} must be non-empty strings",
                )
            normalized_field_name = field_name.strip()
            if field_schema is None:
                optional_fields.add(normalized_field_name)
                continue
            if not isinstance(field_schema, dict):
                raise MailboxRuntimeError(
                    "E_PROTOCOL_SCHEMA_INVALID",
                    f"message field schema for {protocol_ref}.{msg_type}.{normalized_field_name} must be an object or null",
                )
            if bool(field_schema.get("required")):
                required_fields.add(normalized_field_name)
            else:
                optional_fields.add(normalized_field_name)

    missing_fields = sorted(field for field in required_fields if field not in payload)
    if missing_fields:
        raise MailboxRuntimeError(
            "E_PAYLOAD_SCHEMA_INVALID",
            f"payload missing required fields for {protocol_ref}.{msg_type}: {', '.join(missing_fields)}",
        )

    allow_additional_fields = bool(message_schema.get("allow_additional_fields", True))
    if not allow_additional_fields:
        allowed_fields = required_fields | optional_fields
        extra_fields = sorted(key for key in payload.keys() if key not in allowed_fields)
        if extra_fields:
            raise MailboxRuntimeError(
                "E_PAYLOAD_SCHEMA_INVALID",
                f"payload has unexpected fields for {protocol_ref}.{msg_type}: {', '.join(extra_fields)}",
            )


def resolve_transition_target_state(
    *,
    protocol_ref: str,
    schema: dict[str, Any],
    from_state: str,
    msg_type: str,
) -> str:
    if is_compiled_protocol_runtime_schema(schema):
        transition_table = schema.get("transition_table")
        if not isinstance(transition_table, dict):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"compiled transition table missing or invalid for {protocol_ref}",
            )
        per_state = transition_table.get(from_state)
        if not isinstance(per_state, dict):
            raise MailboxRuntimeError(
                "E_STATE_TRANSITION_INVALID",
                f"message {msg_type} is not valid from state {from_state} in {protocol_ref}",
            )
        to_state = per_state.get(normalize_protocol_component(msg_type, "msg_type"))
        if not isinstance(to_state, str) or not to_state.strip():
            raise MailboxRuntimeError(
                "E_STATE_TRANSITION_INVALID",
                f"message {msg_type} is not valid from state {from_state} in {protocol_ref}",
            )
        return to_state
    transitions = schema.get("transitions")
    assert isinstance(transitions, list)
    candidate_states: list[str] = []
    for item in transitions:
        if not isinstance(item, dict):
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transitions for {protocol_ref} must contain only objects",
            )
        if item.get("message") != msg_type or item.get("from") != from_state:
            continue
        to_state = item.get("to")
        if not isinstance(to_state, str) or not to_state.strip():
            raise MailboxRuntimeError(
                "E_PROTOCOL_SCHEMA_INVALID",
                f"protocol transition target for {protocol_ref}.{msg_type} from {from_state} is invalid",
            )
        candidate_states.append(to_state.strip())
    if not candidate_states:
        raise MailboxRuntimeError(
            "E_STATE_TRANSITION_INVALID",
            f"message {msg_type} is not valid from state {from_state} in {protocol_ref}",
        )
    if len(candidate_states) > 1:
        raise MailboxRuntimeError(
            "E_PROTOCOL_SCHEMA_INVALID",
            f"message {msg_type} from state {from_state} is ambiguous in {protocol_ref}",
        )
    return candidate_states[0]
