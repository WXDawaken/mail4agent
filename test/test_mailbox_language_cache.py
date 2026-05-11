from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailbox_language_cache import ProtocolRuntimeDiskCache


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


class MailboxLanguageCacheTests(unittest.TestCase):
    def test_protocol_runtime_disk_cache_hits_on_second_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ProtocolRuntimeDiskCache(Path(temp_dir))
            first = cache.load_or_compile(
                protocol_name="Orders",
                protocol_version="v2",
                schema=orders_protocol_schema(),
            )
            second = cache.load_or_compile(
                protocol_name="Orders",
                protocol_version="v2",
                schema=orders_protocol_schema(),
            )

        self.assertEqual(first.cache_hit, False)
        self.assertEqual(second.cache_hit, True)
        self.assertEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.artifact, second.artifact)
        self.assertEqual(first.cache_path, second.cache_path)

    def test_protocol_runtime_disk_cache_invalidates_on_schema_change(self) -> None:
        base_schema = orders_protocol_schema()
        changed_schema = orders_protocol_schema()
        changed_schema["messages"]["Approve"]["optional"] = ["reviewer_note"]

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ProtocolRuntimeDiskCache(Path(temp_dir))
            first = cache.load_or_compile(
                protocol_name="Orders",
                protocol_version="v2",
                schema=base_schema,
            )
            second = cache.load_or_compile(
                protocol_name="Orders",
                protocol_version="v2",
                schema=changed_schema,
            )

        self.assertEqual(first.cache_hit, False)
        self.assertEqual(second.cache_hit, False)
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertNotEqual(first.cache_path, second.cache_path)
        self.assertNotEqual(first.artifact, second.artifact)


if __name__ == "__main__":
    unittest.main()
