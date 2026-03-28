from __future__ import annotations

import unittest

from mailbox_oncall_server import derive_max_empty_polls


class OncallServerTests(unittest.TestCase):
    def test_derive_max_empty_polls_rounds_up_from_idle_timeout(self) -> None:
        self.assertEqual(
            derive_max_empty_polls(
                idle_exit_after_seconds=12.0,
                poll_interval_seconds=5.0,
            ),
            3,
        )

    def test_derive_max_empty_polls_rejects_non_positive_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "idle_exit_after_seconds"):
            derive_max_empty_polls(
                idle_exit_after_seconds=0.0,
                poll_interval_seconds=5.0,
            )
        with self.assertRaisesRegex(ValueError, "poll_interval_seconds"):
            derive_max_empty_polls(
                idle_exit_after_seconds=10.0,
                poll_interval_seconds=0.0,
            )
