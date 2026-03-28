from __future__ import annotations

import unittest

from mailbox_language_runtime import (
    MailboxRuntimeError,
    format_protocol_ref,
    parse_protocol_ref,
    resolve_transition_target_state,
    validate_message_payload,
    validate_protocol_runtime_schema,
)


def orders_protocol_schema() -> dict[str, object]:
    return {
        "states": ["Init", "AwaitDecision", "Done"],
        "start": "Init",
        "messages": {
            "QuoteReq": {
                "required": ["order_id", "items"],
                "optional": [],
                "allow_additional_fields": False,
            },
            "Approve": {
                "required": ["order_id"],
                "optional": [],
                "allow_additional_fields": False,
            },
        },
        "transitions": [
            {"message": "QuoteReq", "from": "Init", "to": "AwaitDecision"},
            {"message": "Approve", "from": "AwaitDecision", "to": "Done"},
        ],
    }


class MailboxLanguageRuntimeSharedTests(unittest.TestCase):
    def test_parse_protocol_ref_normalizes_valid_protocol(self) -> None:
        protocol_name, protocol_version = parse_protocol_ref("Orders/v2")
        self.assertEqual(protocol_name, "Orders")
        self.assertEqual(protocol_version, "v2")
        self.assertEqual(format_protocol_ref(protocol_name, protocol_version), "Orders/v2")

    def test_validate_protocol_runtime_schema_rejects_invalid_start_state(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as ctx:
            validate_protocol_runtime_schema(
                {
                    "states": ["Init", "Done"],
                    "start": "Missing",
                    "messages": {"QuoteReq": {}},
                    "transitions": [],
                },
                protocol_name="Orders",
                protocol_version="v2",
            )
        self.assertEqual(ctx.exception.code, "E_PROTOCOL_SCHEMA_INVALID")

    def test_validate_message_payload_rejects_missing_and_extra_fields(self) -> None:
        protocol_ref = "Orders/v2"
        message_schema = orders_protocol_schema()["messages"]["QuoteReq"]

        with self.assertRaises(MailboxRuntimeError) as missing_ctx:
            validate_message_payload(
                protocol_ref=protocol_ref,
                msg_type="QuoteReq",
                payload={"order_id": "123"},
                message_schema=message_schema,
            )
        self.assertEqual(missing_ctx.exception.code, "E_PAYLOAD_SCHEMA_INVALID")

        with self.assertRaises(MailboxRuntimeError) as extra_ctx:
            validate_message_payload(
                protocol_ref=protocol_ref,
                msg_type="QuoteReq",
                payload={"order_id": "123", "items": ["sku-1"], "unexpected": True},
                message_schema=message_schema,
            )
        self.assertEqual(extra_ctx.exception.code, "E_PAYLOAD_SCHEMA_INVALID")

    def test_resolve_transition_target_state_returns_expected_state(self) -> None:
        schema = validate_protocol_runtime_schema(
            orders_protocol_schema(),
            protocol_name="Orders",
            protocol_version="v2",
        )
        next_state = resolve_transition_target_state(
            protocol_ref="Orders/v2",
            schema=schema,
            from_state="Init",
            msg_type="QuoteReq",
        )
        self.assertEqual(next_state, "AwaitDecision")


if __name__ == "__main__":
    unittest.main()
