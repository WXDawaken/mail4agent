from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from test.mail4agent_test_support import (
    OPERATOR_ADDRESS,
    PLANNER_ADDRESS,
    REVIEWER_ADDRESS,
    MailboxHTTPFeatureTestCase,
    request_json,
)


ROOT = Path(__file__).resolve().parents[1]


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


def plaintext_protocol_schema() -> dict[str, object]:
    return {
        "states": ["Init", "Open"],
        "start": "Init",
        "messages": {
            "Text": {
                "required": ["body"],
                "optional": ["subject"],
                "allow_additional_fields": False,
            }
        },
        "transitions": [
            {"message": "Text", "from": "Init", "to": "Open"},
        ],
    }


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


def run_stdio_jsonl(
    env: dict[str, str],
    requests: list[dict[str, Any] | str],
    *args: str,
) -> list[dict[str, Any]]:
    stdin_lines: list[str] = []
    for item in requests:
        if isinstance(item, str):
            stdin_lines.append(item)
        else:
            stdin_lines.append(json.dumps(item, ensure_ascii=False))
    stdin_text = "\n".join(stdin_lines) + "\n"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "mailbox_language_stdio.py"), *args],
        cwd=ROOT,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "mailbox_language_stdio.py exited with a non-zero status\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
    responses: list[dict[str, Any]] = []
    for raw_line in completed.stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                "mailbox_language_stdio.py emitted invalid JSON\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            ) from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"mailbox_language_stdio.py emitted non-object JSON: {payload!r}")
        responses.append(payload)
    return responses


class MailboxLanguageStdioTests(MailboxHTTPFeatureTestCase):
    def test_dsl_program_check_and_lower_return_structured_artifacts(self) -> None:
        cache_dir = self.runtime_dir / "dsl-cache"
        env = self.base_env()
        source = (
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
"""
        )
        responses = run_stdio_jsonl(
            env,
            [
                {
                    "id": "dsl-check",
                    "command": "check",
                    "cache_dir": str(cache_dir),
                    "artifact": {
                        "kind": "dsl_program",
                        "source": source,
                        "mailbox_addresses": {"reviewer_mb": REVIEWER_ADDRESS},
                        "from_address": OPERATOR_ADDRESS,
                    },
                },
                {
                    "id": "dsl-lower",
                    "command": "lower",
                    "cache_dir": str(cache_dir),
                    "artifact": {
                        "kind": "dsl_program",
                        "source": source,
                        "mailbox_addresses": {"reviewer_mb": REVIEWER_ADDRESS},
                        "from_address": OPERATOR_ADDRESS,
                    },
                },
            ],
        )

        self.assertEqual(len(responses), 2)
        checked = responses[0]
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["artifact"]["protocol_count"], 1)
        self.assertEqual(checked["artifact"]["mailbox_count"], 1)
        self.assertEqual(checked["artifact"]["operation_count"], 2)

        lowered = responses[1]
        self.assertTrue(lowered["ok"])
        self.assertEqual(lowered["artifact"]["kind"], "dsl_program_lowered")
        self.assertEqual(lowered["artifact"]["protocols"][0]["protocol"], "Orders/v2")
        self.assertEqual(lowered["artifact"]["mailboxes"][0]["address"], REVIEWER_ADDRESS)
        self.assertEqual(lowered["artifact"]["thread_bindings"]["review_t"]["state"], "Done")

    def test_protocol_check_and_lower_use_compile_cache(self) -> None:
        cache_dir = self.runtime_dir / "protocol-cache"
        env = self.base_env()
        requests = [
            {
                "id": "check-orders",
                "command": "check",
                "cache_dir": str(cache_dir),
                "artifact": {
                    "kind": "protocol_schema",
                    "protocol": "Orders/v2",
                    "schema": orders_protocol_schema(),
                },
            },
            {
                "id": "lower-orders",
                "command": "lower",
                "cache_dir": str(cache_dir),
                "artifact": {
                    "kind": "protocol_schema",
                    "protocol": "Orders/v2",
                    "schema": orders_protocol_schema(),
                },
            },
        ]

        responses = run_stdio_jsonl(env, requests)
        self.assertEqual(len(responses), 2)

        first = responses[0]
        self.assertTrue(first["ok"])
        self.assertEqual(first["id"], "check-orders")
        self.assertEqual(first["protocol"], "Orders/v2")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(Path(first["cache_path"]).exists())

        second = responses[1]
        self.assertTrue(second["ok"])
        self.assertEqual(second["id"], "lower-orders")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["protocol"], "Orders/v2")
        self.assertEqual(second["artifact"]["kind"], "mailbox_language_compiled_protocol_runtime")
        self.assertEqual(second["artifact"]["protocol"], "Orders/v2")

    def test_run_executes_register_bind_send_spawn_and_handoff(self) -> None:
        env = self.admin_env()

        setup_responses = run_stdio_jsonl(
            env,
            [
                {
                    "id": "register-orders",
                    "command": "run",
                    "artifact": {
                        "kind": "protocol_schema",
                        "protocol": "Orders/v2",
                        "schema": orders_protocol_schema(),
                    },
                },
                {
                    "id": "register-plaintext",
                    "command": "run",
                    "artifact": {
                        "kind": "protocol_schema",
                        "protocol": "PlainText/v1",
                        "schema": plaintext_protocol_schema(),
                    },
                },
                {
                    "id": "bind-reviewer",
                    "command": "run",
                    "artifact": {
                        "kind": "mailbox_binding",
                        "address": REVIEWER_ADDRESS,
                        "accepts": ["Orders/v2"],
                    },
                },
                {
                    "id": "bind-planner",
                    "command": "run",
                    "artifact": {
                        "kind": "mailbox_binding",
                        "address": PLANNER_ADDRESS,
                        "accepts": ["PlainText/v1"],
                        "default_protocol": "PlainText/v1",
                    },
                },
            ],
        )
        self.assertTrue(all(item["ok"] for item in setup_responses))

        created = run_stdio_jsonl(
            env,
            [
                {
                    "id": "send-quote",
                    "command": "run",
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "send",
                        "from_address": OPERATOR_ADDRESS,
                        "to_address": REVIEWER_ADDRESS,
                        "protocol": "Orders/v2",
                        "message": "QuoteReq",
                        "subject": "Need typed review",
                        "payload": {"order_id": "stdio-1", "items": ["sku-1"]},
                    },
                }
            ],
        )[0]
        self.assertTrue(created["ok"])
        result = created["result"]
        self.assertEqual(result["protocol"], "Orders/v2")
        self.assertEqual(result["state"], "AwaitDecision")
        parent_thread_id = str(result["thread_id"])

        advanced = run_stdio_jsonl(
            env,
            [
                {
                    "id": "approve-quote",
                    "command": "run",
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "send",
                        "from_address": OPERATOR_ADDRESS,
                        "to_address": REVIEWER_ADDRESS,
                        "thread_id": parent_thread_id,
                        "protocol": "Orders/v2",
                        "message": "Approve",
                        "payload": {"order_id": "stdio-1"},
                    },
                }
            ],
        )[0]
        self.assertTrue(advanced["ok"])
        self.assertEqual(advanced["result"]["state"], "Done")

        spawned = run_stdio_jsonl(
            env,
            [
                {
                    "id": "spawn-summary",
                    "command": "run",
                    "artifact": {
                        "kind": "message_envelope",
                        "op": "spawn",
                        "from_address": OPERATOR_ADDRESS,
                        "to_address": PLANNER_ADDRESS,
                        "parent_thread_id": parent_thread_id,
                        "protocol": "PlainText/v1",
                        "message": "Text",
                        "payload": {"body": "Order stdio-1 approved"},
                    },
                }
            ],
        )[0]
        self.assertTrue(spawned["ok"])
        child_thread_id = str(spawned["result"]["thread_id"])
        self.assertEqual(spawned["result"]["parent_thread_id"], parent_thread_id)

        handoff = run_stdio_jsonl(
            env,
            [
                {
                    "id": "handoff-summary",
                    "command": "run",
                    "artifact": {
                        "kind": "handoff_event",
                        "from_thread_id": parent_thread_id,
                        "to_thread_id": child_thread_id,
                        "actor": "stdio-test",
                        "metadata": {"reason": "approved order follow-up"},
                    },
                }
            ],
        )[0]
        self.assertTrue(handoff["ok"])
        self.assertEqual(handoff["handoff"]["actor"], "stdio-test")

        parent_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": parent_thread_id},
        )["thread"]
        self.assertEqual(parent_thread["state"], "Done")
        self.assertEqual(len(parent_thread["outgoing_handoffs"]), 1)
        self.assertEqual(parent_thread["outgoing_handoffs"][0]["related_thread_id"], child_thread_id)

        child_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": child_thread_id},
        )["thread"]
        self.assertEqual(child_thread["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(child_thread["parent_thread_id"], parent_thread_id)

    def test_dsl_program_run_executes_send_text_spawn_and_handoff(self) -> None:
        env = self.admin_env()
        source = (
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
"""
        )

        response = run_stdio_jsonl(
            env,
            [
                {
                    "id": "dsl-run",
                    "command": "run",
                    "artifact": {
                        "kind": "dsl_program",
                        "source": source,
                        "mailbox_addresses": {
                            "support_mb": PLANNER_ADDRESS,
                            "orders_mb": REVIEWER_ADDRESS,
                        },
                        "inputs": {"order_id": "123"},
                        "from_address": OPERATOR_ADDRESS,
                    },
                }
            ],
        )[0]

        self.assertTrue(response["ok"])
        run_result = response["run"]
        self.assertEqual(set(run_result["protocols_registered"]), {"Orders/v2", "PlainText/v1"})
        self.assertEqual(set(run_result["mailboxes_configured"]), {"support_mb", "orders_mb"})
        self.assertEqual(len(run_result["operations"]), 3)

        text_binding = run_result["thread_bindings"]["text_t"]
        order_binding = run_result["thread_bindings"]["order_t"]
        self.assertEqual(text_binding["protocol"], "PlainText/v1")
        self.assertEqual(order_binding["protocol"], "Orders/v2")

        text_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": text_binding["thread_id"]},
        )["thread"]
        self.assertEqual(text_thread["protocol"]["protocol"], "PlainText/v1")
        self.assertEqual(text_thread["state"], "Open")
        self.assertEqual(len(text_thread["outgoing_handoffs"]), 1)

        order_thread = request_json(
            self.base_url,
            "GET",
            "/admin/thread",
            token=self.admin_token,
            query={"thread_id": order_binding["thread_id"]},
        )["thread"]
        self.assertEqual(order_thread["protocol"]["protocol"], "Orders/v2")
        self.assertEqual(order_thread["parent_thread_id"], text_binding["thread_id"])
        self.assertEqual(len(order_thread["incoming_handoffs"]), 1)

    def test_mixed_invalid_and_valid_lines_return_structured_responses(self) -> None:
        cache_dir = self.runtime_dir / "protocol-cache"
        env = self.base_env()
        responses = run_stdio_jsonl(
            env,
            [
                "{not-json",
                {
                    "id": "invalid-schema",
                    "command": "check",
                    "cache_dir": str(cache_dir),
                    "artifact": {
                        "kind": "protocol_schema",
                        "protocol": "Broken/v1",
                        "schema": {"states": ["Init"]},
                    },
                },
                {
                    "id": "valid-schema",
                    "command": "check",
                    "cache_dir": str(cache_dir),
                    "artifact": {
                        "kind": "protocol_schema",
                        "protocol": "PlainText/v1",
                        "schema": plaintext_protocol_schema(),
                    },
                },
            ],
        )

        self.assertEqual(len(responses), 3)

        parse_error = responses[0]
        self.assertFalse(parse_error["ok"])
        self.assertEqual(parse_error["line_number"], 1)
        self.assertNotIn("error_code", parse_error)

        schema_error = responses[1]
        self.assertFalse(schema_error["ok"])
        self.assertEqual(schema_error["id"], "invalid-schema")
        self.assertEqual(schema_error["error_code"], "E_PROTOCOL_SCHEMA_INVALID")
        self.assertEqual(schema_error["line_number"], 2)

        valid = responses[2]
        self.assertTrue(valid["ok"])
        self.assertEqual(valid["id"], "valid-schema")
        self.assertEqual(valid["protocol"], "PlainText/v1")


if __name__ == "__main__":
    unittest.main()
