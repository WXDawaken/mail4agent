from __future__ import annotations

import unittest

from mailbox_language_runtime import MailboxRuntimeError
from mailbox_language_source import lower_source_program


def orders_protocol_source() -> str:
    return """
protocol Orders/v2 {
  state Init;
  state AwaitDecision;
  state Done;

  start Init;

  message QuoteReq {
    order_id: String;
    items: [OrderItem];
  }

  message Approve {
    order_id: String;
  }

  message Cancel {
    order_id: String;
    reason?: String;
  }

  on QuoteReq from Init -> AwaitDecision;
  on Approve from AwaitDecision -> Done;
  on Cancel from Init -> Done;
}
"""


class MailboxLanguageSourceTests(unittest.TestCase):
    def test_lower_source_program_supports_typed_value_bindings_and_thread_alias(self) -> None:
        lowered = lower_source_program(
            orders_protocol_source()
            + """
mailbox reviewer_mb : Orders/v2;

let order_id: String = "123";
let items: [OrderItem] = ["sku-1"];
let review_t: thread<Orders/v2> = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: order_id;
  items: items;
};
let review_alias: thread<Orders/v2> = review_t;

send to review_alias using Approve {
  order_id: order_id;
};
""",
            mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
            from_address="operator@mail4agent.codex",
        )

        self.assertEqual(len(lowered["operations"]), 2)
        self.assertEqual(lowered["operations"][0]["artifact"]["payload"]["order_id"], "123")
        self.assertEqual(lowered["operations"][0]["artifact"]["payload"]["items"], ["sku-1"])
        self.assertEqual(lowered["operations"][1]["artifact"]["thread_var"], "review_alias")
        self.assertEqual(lowered["thread_bindings"]["review_t"]["protocol"], "Orders/v2")
        self.assertEqual(lowered["thread_bindings"]["review_alias"]["protocol"], "Orders/v2")
        self.assertEqual(lowered["thread_bindings"]["review_alias"]["state"], "Done")

    def test_lower_source_program_tracks_thread_state_for_mailbox_and_thread_send(self) -> None:
        lowered = lower_source_program(
            orders_protocol_source()
            + """
mailbox reviewer_mb : Orders/v2;

let review_t = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: ["sku-1"];
};

send to review_t using Approve {
  order_id: "123";
};
""",
            mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
            from_address="operator@mail4agent.codex",
        )

        self.assertEqual(lowered["kind"], "dsl_program_lowered")
        self.assertEqual(len(lowered["protocols"]), 1)
        self.assertEqual(lowered["protocols"][0]["protocol"], "Orders/v2")
        self.assertEqual(lowered["mailboxes"][0]["address"], "reviewer@mail4agent.codex")
        self.assertEqual(len(lowered["operations"]), 2)

        created = lowered["operations"][0]
        self.assertEqual(created["bind"], "review_t")
        self.assertEqual(created["artifact"]["target_kind"], "mailbox")
        self.assertEqual(created["artifact"]["protocol"], "Orders/v2")
        self.assertEqual(created["artifact"]["message"], "QuoteReq")

        advanced = lowered["operations"][1]
        self.assertEqual(advanced["artifact"]["target_kind"], "thread")
        self.assertEqual(advanced["artifact"]["thread_var"], "review_t")
        self.assertEqual(advanced["artifact"]["message"], "Approve")

        self.assertEqual(lowered["thread_bindings"]["review_t"]["protocol"], "Orders/v2")
        self.assertEqual(lowered["thread_bindings"]["review_t"]["state"], "Done")

    def test_lower_source_program_supports_builtin_plaintext_send_text_spawn_and_handoff(self) -> None:
        lowered = lower_source_program(
            orders_protocol_source()
            + """
mailbox support_mb : PlainText/v1 | Orders/v2;
mailbox orders_mb : Orders/v2;

let text_t = send text to support_mb "Please cancel order 123";

let order_t = spawn to orders_mb using Orders/v2.Cancel {
  order_id: order_id;
  reason: "parsed from text thread";
} from text_t;

handoff text_t -> order_t;
""",
            mailbox_addresses={
                "support_mb": "planner@mail4agent.codex",
                "orders_mb": "reviewer@mail4agent.codex",
            },
            inputs={"order_id": "123"},
            from_address="operator@mail4agent.codex",
        )

        protocols = {entry["protocol"] for entry in lowered["protocols"]}
        self.assertIn("PlainText/v1", protocols)
        self.assertIn("Orders/v2", protocols)
        self.assertEqual(len(lowered["operations"]), 3)

        send_text = lowered["operations"][0]
        self.assertEqual(send_text["artifact"]["protocol"], "PlainText/v1")
        self.assertEqual(send_text["artifact"]["message"], "Text")
        self.assertEqual(send_text["artifact"]["payload"]["body"], "Please cancel order 123")

        spawned = lowered["operations"][1]
        self.assertEqual(spawned["bind"], "order_t")
        self.assertEqual(spawned["artifact"]["op"], "spawn")
        self.assertEqual(spawned["artifact"]["parent_thread_var"], "text_t")
        self.assertEqual(spawned["artifact"]["payload"]["order_id"], "123")

        handoff = lowered["operations"][2]
        self.assertEqual(handoff["kind"], "handoff_operation")
        self.assertEqual(handoff["artifact"]["from_thread_var"], "text_t")
        self.assertEqual(handoff["artifact"]["to_thread_var"], "order_t")

        self.assertEqual(lowered["thread_bindings"]["text_t"]["state"], "Open")
        self.assertEqual(lowered["thread_bindings"]["order_t"]["state"], "Done")

    def test_lower_source_program_rejects_unknown_input_variable(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox reviewer_mb : Orders/v2;

let review_t = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: missing_order_id;
  items: ["sku-1"];
};
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_SOURCE_VALUE_UNKNOWN")

    def test_lower_source_program_rejects_primitive_field_type_mismatch(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox reviewer_mb : Orders/v2;

let review_t = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: 123;
  items: ["sku-1"];
};
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_PAYLOAD_SCHEMA_INVALID")
        self.assertIn("expected String", str(context.exception))

    def test_lower_source_program_rejects_list_field_type_mismatch(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox reviewer_mb : Orders/v2;

let review_t = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: items;
};
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                inputs={"items": "sku-1"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_PAYLOAD_SCHEMA_INVALID")
        self.assertIn("expected [OrderItem]", str(context.exception))

    def test_lower_source_program_rejects_builtin_plaintext_body_type_mismatch(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox support_mb : PlainText/v1;

let text_t = send text to support_mb {
  body: 123;
};
""",
                mailbox_addresses={"support_mb": "planner@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_PAYLOAD_SCHEMA_INVALID")
        self.assertIn("expected String", str(context.exception))

    def test_lower_source_program_rejects_value_binding_type_mismatch(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox reviewer_mb : Orders/v2;

let order_id: String = 123;
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_SOURCE_TYPE_INVALID")
        self.assertIn("binding order_id expected String", str(context.exception))

    def test_lower_source_program_rejects_thread_annotation_mismatch(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
protocol Support/v1 {
  state Open;
  start Open;

  message Reply {
    body: String;
  }

  on Reply from Open -> Open;
}

mailbox reviewer_mb : Orders/v2;

let review_t: thread<Support/v1> = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: ["sku-1"];
};
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_SOURCE_TYPE_INVALID")
        self.assertIn("expression returns thread<Orders/v2>", str(context.exception))

    def test_lower_source_program_rejects_binding_send_to_existing_thread(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
mailbox reviewer_mb : Orders/v2;

let review_t = send to reviewer_mb using Orders/v2.QuoteReq {
  order_id: "123";
  items: ["sku-1"];
};

let done_t = send to review_t using Approve {
  order_id: "123";
};
""",
                mailbox_addresses={"reviewer_mb": "reviewer@mail4agent.codex"},
                from_address="operator@mail4agent.codex",
            )
        self.assertEqual(context.exception.code, "E_SOURCE_TYPE_INVALID")

    def test_lower_source_program_rejects_multi_protocol_shorthand_without_plaintext_lead(self) -> None:
        with self.assertRaises(MailboxRuntimeError) as context:
            lower_source_program(
                orders_protocol_source()
                + """
protocol Support/v1 {
  state Open;
  start Open;

  message Reply {
    body: String;
  }

  on Reply from Open -> Open;
}

mailbox bad_mb : Orders/v2 | Support/v1;
"""
            )
        self.assertEqual(context.exception.code, "E_SOURCE_DECLARATION_INVALID")


if __name__ == "__main__":
    unittest.main()
