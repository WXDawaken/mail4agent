from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailbox_worker import ConsumeConfig
from oncall_registry import OncallRegistry
from oncall_supervisor import (
    ClaimedDeliveryExecutionContext,
    ClaimedDeliveryExecutionResult,
    OncallSupervisorConfig,
    run_oncall_supervisor,
)


class FakeMailboxClient:
    def __init__(
        self,
        deliveries: list[dict[str, object]],
        *,
        threads_by_id: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._deliveries = list(deliveries)
        self._threads_by_id = dict(threads_by_id or {})
        self.claim_calls: list[dict[str, object]] = []
        self.ack_calls: list[dict[str, object]] = []
        self.nack_calls: list[dict[str, object]] = []

    def claim(
        self,
        *,
        to_address: str | None = None,
        to_addresses: list[str] | None = None,
        consumer_id: str | None = None,
        lease_seconds: int = 60,
        serialization_scope: str = "mailbox_thread",
    ) -> dict[str, object] | None:
        self.claim_calls.append(
            {
                "to_address": to_address,
                "to_addresses": list(to_addresses or []),
                "consumer_id": consumer_id,
                "lease_seconds": lease_seconds,
                "serialization_scope": serialization_scope,
            }
        )
        if self._deliveries:
            return dict(self._deliveries.pop(0))
        return None

    def ack(self, *, delivery_id: int, claim_token: str, actor: str | None = None) -> bool:
        self.ack_calls.append(
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "actor": actor,
            }
        )
        return True

    def nack(
        self,
        *,
        delivery_id: int,
        claim_token: str,
        retry_after_seconds: int = 30,
        last_error: str | None = None,
        actor: str | None = None,
    ) -> bool:
        self.nack_calls.append(
            {
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "retry_after_seconds": retry_after_seconds,
                "last_error": last_error,
                "actor": actor,
            }
        )
        return True

    def heartbeat(self, *, delivery_id: int, claim_token: str, lease_seconds: int = 60) -> bool:
        return True

    def get_thread(
        self,
        *,
        thread_id: str | None = None,
        message_id: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, object] | None:
        if thread_id is not None:
            payload = self._threads_by_id.get(thread_id)
            if payload is not None:
                return json.loads(json.dumps(payload))
        if allow_missing:
            return None
        raise RuntimeError(f"thread not found: {thread_id or message_id}")


class OncallSupervisorTests(unittest.TestCase):
    def test_registry_tracks_custom_summary_file_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(
                runtime_dir=runtime_dir,
                role="operator",
                raw_summary_file="logs\\custom-summary.json",
            )

            registry.mark_started(
                consumer_id="operator-oncall-1",
                started_at="2026-03-28T00:00:00Z",
            )
            summary = {
                "ok": True,
                "role": "operator",
                "consumer_id": "operator-oncall-1",
                "runtime_dir": str(runtime_dir),
                "summary_file": str(registry.summary_path),
                "started_at": "2026-03-28T00:00:00Z",
                "completed_at": "2026-03-28T00:05:00Z",
                "last_delivery_id": 101,
                "last_thread_id": "thread-101",
            }

            registry.record_completion(summary)

            self.assertEqual(
                registry.summary_path,
                (runtime_dir / "logs" / "custom-summary.json").resolve(),
            )
            self.assertTrue(registry.registry_path.exists())
            saved_summary = json.loads(registry.summary_path.read_text(encoding="utf-8"))
            saved_registry = json.loads(registry.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_summary["last_delivery_id"], 101)
            self.assertEqual(saved_registry["status"], "succeeded")
            self.assertEqual(saved_registry["last_thread_id"], "thread-101")

    def test_registry_inspect_state_lists_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_completion(
                {
                    "ok": True,
                    "role": "operator",
                    "consumer_id": "operator-oncall-1",
                    "runtime_dir": str(runtime_dir),
                    "summary_file": str(registry.summary_path),
                    "started_at": "2026-03-28T00:00:00Z",
                    "completed_at": "2026-03-28T00:05:00Z",
                    "last_delivery_id": 101,
                    "last_thread_id": "thread-101",
                }
            )
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-101",
                    "worker_kind": "test-worker",
                    "worker_id": "worker-101",
                    "completed_at": "2026-03-28T00:05:00Z",
                },
                status="acked",
            )

            inspected = registry.inspect_state()

            self.assertEqual(inspected["role"], "operator")
            self.assertEqual(inspected["role_registry"]["status"], "succeeded")
            self.assertEqual(len(inspected["threads"]), 1)
            self.assertEqual(inspected["threads"][0]["thread_id"], "thread-101")

    def test_run_oncall_supervisor_writes_summary_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 101,
                        "message_id": "msg-101",
                        "thread_id": "thread-101",
                        "claim_token": "claim-101",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ],
                threads_by_id={
                    "thread-101": _build_thread_payload(
                        thread_id="thread-101",
                        messages=[
                            _message(
                                message_id="msg-101",
                                thread_id="thread-101",
                                from_address="planner@example.test",
                                to_addresses=["operator@example.test"],
                                payload={"task_id": "demo"},
                            ),
                            _message(
                                message_id="reply-101",
                                thread_id="thread-101",
                                in_reply_to_message_id="msg-101",
                                from_address="operator@example.test",
                                to_addresses=["planner@example.test"],
                                payload={
                                    "ok": True,
                                    "task_status": "completed",
                                    "task_terminal": True,
                                    "resolution_summary": "task completed in one turn",
                                },
                            ),
                        ],
                    )
                },
            )
            config = _build_supervisor_config(runtime_dir)
            seen_delivery_ids: list[int] = []

            result = run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: _record_successful_execution(seen_delivery_ids, context),
            )

            self.assertEqual(seen_delivery_ids, [101])
            self.assertEqual(result["acked"], 1)
            self.assertEqual(result["nacked"], 0)
            self.assertEqual(result["last_delivery_id"], 101)
            self.assertEqual(result["last_thread_id"], "thread-101")
            self.assertEqual(result["last_processed_message_id"], "msg-101")
            self.assertEqual(result["registry_file"], str(registry.registry_path))
            self.assertEqual(len(client.ack_calls), 1)
            saved_summary = json.loads(registry.summary_path.read_text(encoding="utf-8"))
            saved_registry = json.loads(registry.registry_path.read_text(encoding="utf-8"))
            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-101",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(saved_summary["last_thread_id"], "thread-101")
            self.assertEqual(saved_registry["status"], "succeeded")
            self.assertEqual(thread_registry["status"], "acked")
            self.assertEqual(thread_registry["last_processed_message_id"], "msg-101")
            self.assertEqual(thread_registry["handoff_summary"], "operator completed the task")
            self.assertFalse(thread_registry["supports_worker_reuse"])
            self.assertFalse(thread_registry["reused_worker"])
            self.assertEqual(thread_registry["recovery_reason"], None)
            self.assertEqual(thread_registry["task_status"], "completed")
            self.assertTrue(thread_registry["task_terminal"])
            self.assertEqual(thread_registry["task_status_message_id"], "reply-101")
            self.assertEqual(thread_registry["task_resolution_summary"], "task completed in one turn")
            self.assertEqual(saved_registry["last_task_status"], "completed")

    def test_run_oncall_supervisor_infers_waiting_on_peer_from_legacy_reply_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 111,
                        "message_id": "msg-111",
                        "thread_id": "thread-111",
                        "claim_token": "claim-111",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ],
                threads_by_id={
                    "thread-111": _build_thread_payload(
                        thread_id="thread-111",
                        messages=[
                            _message(
                                message_id="msg-111",
                                thread_id="thread-111",
                                from_address="planner@example.test",
                                to_addresses=["operator@example.test"],
                                payload={"task_id": "demo"},
                            ),
                            _message(
                                message_id="reply-111",
                                thread_id="thread-111",
                                in_reply_to_message_id="msg-111",
                                from_address="operator@example.test",
                                to_addresses=["planner@example.test"],
                                payload={
                                    "ok": True,
                                    "engine_request_sent": True,
                                    "requested_engine_capability": "shared snapshot contract",
                                },
                            ),
                        ],
                    )
                },
            )

            result = run_oncall_supervisor(
                client,
                _build_supervisor_config(runtime_dir),
                registry,
                lambda context: _record_successful_execution([], context),
            )

            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-111",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["task_status"], "waiting_on_peer")
            self.assertFalse(result["task_terminal"])
            self.assertEqual(thread_registry["task_status"], "waiting_on_peer")
            self.assertEqual(thread_registry["task_resolution_summary"], "shared snapshot contract")

    def test_run_oncall_supervisor_records_failure_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 202,
                        "message_id": "msg-202",
                        "thread_id": "thread-202",
                        "claim_token": "claim-202",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(runtime_dir)

            with self.assertRaisesRegex(RuntimeError, "boom"):
                run_oncall_supervisor(
                    client,
                    config,
                    registry,
                    lambda _context: (_ for _ in ()).throw(RuntimeError("boom")),
                )

            saved_summary = json.loads(registry.summary_path.read_text(encoding="utf-8"))
            saved_registry = json.loads(registry.registry_path.read_text(encoding="utf-8"))
            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-202",
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(saved_summary["ok"])
            self.assertEqual(saved_summary["last_delivery_id"], 202)
            self.assertEqual(saved_summary["last_thread_id"], "thread-202")
            self.assertEqual(saved_registry["status"], "failed")
            self.assertEqual(saved_registry["last_delivery_id"], 202)
            self.assertEqual(thread_registry["status"], "failed")
            self.assertEqual(thread_registry["last_processed_message_id"], "msg-202")
            self.assertEqual(client.ack_calls, [])
            self.assertEqual(client.nack_calls, [])

    def test_run_oncall_supervisor_reuses_existing_worker_binding_when_backend_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-303",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "sticky-worker-303",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:00:00Z",
                    "last_processed_message_id": "msg-old",
                },
                status="acked",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 303,
                        "message_id": "msg-303",
                        "thread_id": "thread-303",
                        "claim_token": "claim-303",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                },
            )

            result = run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: _record_successful_execution([], context),
            )

            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-303",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["worker_id"], "sticky-worker-303")
            self.assertTrue(result["reused_worker"])
            self.assertEqual(result["previous_worker_id"], "sticky-worker-303")
            self.assertEqual(thread_registry["worker_id"], "sticky-worker-303")
            self.assertTrue(thread_registry["reused_worker"])

    def test_run_oncall_supervisor_replaces_stale_running_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-404",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "stale-worker-404",
                    "supports_worker_reuse": True,
                    "lease_until": "2020-01-01T00:00:00Z",
                    "completed_at": "2020-01-01T00:00:00Z",
                },
                status="running",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 404,
                        "message_id": "msg-404",
                        "thread_id": "thread-404",
                        "claim_token": "claim-404",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                },
            )

            result = run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: _record_successful_execution([], context),
            )

            self.assertNotEqual(result["worker_id"], "stale-worker-404")
            self.assertFalse(result["reused_worker"])
            self.assertEqual(result["recovery_reason"], "replaced_stale_running_binding")

    def test_run_oncall_supervisor_replaces_missing_reusable_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-505",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "missing-worker-505",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:00:00Z",
                },
                status="acked",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 505,
                        "message_id": "msg-505",
                        "thread_id": "thread-505",
                        "claim_token": "claim-505",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                },
                can_reuse_worker=lambda worker_id: worker_id != "missing-worker-505",
            )

            result = run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: _record_successful_execution([], context),
            )

            self.assertNotEqual(result["worker_id"], "missing-worker-505")
            self.assertFalse(result["reused_worker"])
            self.assertEqual(result["previous_worker_id"], "missing-worker-505")
            self.assertEqual(result["recovery_reason"], "previous_worker_not_available")

    def test_run_oncall_supervisor_persists_recovery_summary_metadata_to_thread_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-606",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "missing-worker-606",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:00:00Z",
                    "handoff_summary": "previous summary",
                    "last_processed_message_id": "msg-605",
                },
                status="acked",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 606,
                        "message_id": "msg-606",
                        "thread_id": "thread-606",
                        "claim_token": "claim-606",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                },
                can_reuse_worker=lambda worker_id: worker_id != "missing-worker-606",
            )

            run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: ClaimedDeliveryExecutionResult(
                    exit_code=0,
                    metadata={
                        "backend_name": "sticky-test",
                        "execution_mode": "sticky-test",
                        "worker_kind": "sticky-test-worker",
                        "handoff_summary": "current summary",
                        "recovered_handoff_summary_used": True,
                        "recovered_recovery_reason": "previous_worker_not_available",
                        "recovered_last_processed_message_id": "msg-605",
                        "recovered_previous_worker_id": "missing-worker-606",
                    },
                ),
            )

            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-606",
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(thread_registry["recovered_handoff_summary_used"])
            self.assertEqual(thread_registry["recovered_recovery_reason"], "previous_worker_not_available")
            self.assertEqual(thread_registry["recovered_last_processed_message_id"], "msg-605")
            self.assertEqual(thread_registry["recovered_previous_worker_id"], "missing-worker-606")

    def test_run_oncall_supervisor_selects_existing_binding_for_requested_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-707",
                    "workspace_dir": "E:\\repo\\workspace-a",
                    "workspace_root_dir": "E:\\repo",
                    "workspace_source": "delivery.payload.workspace_dir",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "sticky-worker-a",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:00:00Z",
                },
                status="acked",
            )
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-707",
                    "workspace_dir": "E:\\repo\\workspace-b",
                    "workspace_root_dir": "E:\\repo",
                    "workspace_source": "delivery.payload.workspace_dir",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "sticky-worker-b",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:01:00Z",
                },
                status="acked",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 707,
                        "message_id": "msg-707",
                        "thread_id": "thread-707",
                        "claim_token": "claim-707",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                    "workspace_dir": "E:\\repo",
                    "workspace_root_dir": "E:\\repo",
                },
                resolve_workspace=lambda _delivery, _existing: {
                    "workspace_dir": "E:\\repo\\workspace-a",
                    "workspace_root_dir": "E:\\repo",
                    "workspace_source": "delivery.payload.workspace_dir",
                },
            )

            result = run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: _record_successful_execution([], context),
            )

            self.assertEqual(result["worker_id"], "sticky-worker-a")
            self.assertEqual(result["previous_worker_id"], "sticky-worker-a")
            self.assertTrue(result["reused_worker"])
            self.assertEqual(result["workspace_dir"], "E:\\repo\\workspace-a")

    def test_run_oncall_supervisor_preserves_bindings_by_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            registry = OncallRegistry.create(runtime_dir=runtime_dir, role="operator")
            registry.record_thread_state(
                {
                    "consumer_id": "operator-oncall-1",
                    "mailbox_address": "operator@example.test",
                    "thread_id": "thread-808",
                    "workspace_dir": "E:\\repo\\workspace-a",
                    "workspace_root_dir": "E:\\repo",
                    "workspace_source": "delivery.payload.workspace_dir",
                    "worker_kind": "sticky-test-worker",
                    "worker_id": "sticky-worker-a",
                    "supports_worker_reuse": True,
                    "lease_until": "2099-01-01T00:00:00Z",
                    "completed_at": "2026-03-28T00:00:00Z",
                },
                status="acked",
            )
            client = FakeMailboxClient(
                [
                    {
                        "delivery_id": 808,
                        "message_id": "msg-808",
                        "thread_id": "thread-808",
                        "claim_token": "claim-808",
                        "from": "planner@example.test",
                        "to": "operator@example.test",
                    }
                ]
            )
            config = _build_supervisor_config(
                runtime_dir,
                execution_metadata={
                    "backend_name": "sticky-test",
                    "worker_kind": "sticky-test-worker",
                    "supports_worker_reuse": True,
                    "workspace_dir": "E:\\repo",
                    "workspace_root_dir": "E:\\repo",
                },
                resolve_workspace=lambda _delivery, _existing: {
                    "workspace_dir": "E:\\repo\\workspace-b",
                    "workspace_root_dir": "E:\\repo",
                    "workspace_source": "delivery.payload.workspace_dir",
                },
            )

            run_oncall_supervisor(
                client,
                config,
                registry,
                lambda context: ClaimedDeliveryExecutionResult(
                    exit_code=0,
                    metadata={
                        "backend_name": "sticky-test",
                        "worker_kind": "sticky-test-worker",
                        "worker_id": "sticky-worker-b",
                        "workspace_dir": "E:\\repo\\workspace-b",
                        "workspace_root_dir": "E:\\repo",
                        "workspace_source": "delivery.payload.workspace_dir",
                    },
                ),
            )

            thread_registry = json.loads(
                registry.thread_registry_path(
                    mailbox_address="operator@example.test",
                    thread_id="thread-808",
                ).read_text(encoding="utf-8")
            )
            bindings = thread_registry["bindings_by_workspace"]
            self.assertIn("e:/repo/workspace-a", bindings)
            self.assertIn("e:/repo/workspace-b", bindings)
            self.assertEqual(bindings["e:/repo/workspace-a"]["worker_id"], "sticky-worker-a")
            self.assertEqual(bindings["e:/repo/workspace-b"]["worker_id"], "sticky-worker-b")
            self.assertEqual(thread_registry["workspace_dir"], "E:\\repo\\workspace-b")


def _build_supervisor_config(
    runtime_dir: Path,
    *,
    execution_metadata: dict[str, object] | None = None,
    can_reuse_worker=None,
    resolve_workspace=None,
) -> OncallSupervisorConfig:
    consume_config = ConsumeConfig(
        to_address="operator@example.test",
        to_addresses=(),
        consumer_id="operator-oncall-1",
        serialization_scope="mailbox_thread",
        lease_seconds=120,
        heartbeat_interval_seconds=10.0,
        poll_interval_seconds=0.01,
        retry_after_seconds=45,
        ack_exit_codes=frozenset({0}),
        once=True,
        max_deliveries=None,
    )
    return OncallSupervisorConfig(
        role="operator",
        watch=False,
        runtime_dir=runtime_dir,
        consumer_id="operator-oncall-1",
        claim_addresses=("operator@example.test",),
        execution_metadata=dict(
            execution_metadata
            or {
                "backend_name": "test-backend",
                "execution_mode": "test-backend",
                "handler_command": ["python", "test-handler.py"],
                "supports_worker_reuse": False,
            }
        ),
        consume_config=consume_config,
        started_at="2026-03-28T00:00:00Z",
        can_reuse_worker=can_reuse_worker,
        resolve_workspace=resolve_workspace,
    )


def _record_successful_execution(
    seen_delivery_ids: list[int],
    context: ClaimedDeliveryExecutionContext,
) -> ClaimedDeliveryExecutionResult:
    delivery = context.delivery
    seen_delivery_ids.append(int(delivery["delivery_id"]))
    return ClaimedDeliveryExecutionResult(
        exit_code=0,
        metadata={
            "backend_name": "test-backend",
            "execution_mode": "test-backend",
            "handler_command": ["python", "test-handler.py"],
            "handoff_summary": "operator completed the task",
        },
    )


def _build_thread_payload(*, thread_id: str, messages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "message_count": len(messages),
        "messages": messages,
    }


def _message(
    *,
    message_id: str,
    thread_id: str,
    from_address: str,
    to_addresses: list[str],
    payload: dict[str, object],
    in_reply_to_message_id: str | None = None,
    created_at: str = "2026-03-28T00:00:01Z",
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to_message_id": in_reply_to_message_id,
        "payload": payload,
        "created_at": created_at,
        "from": from_address,
        "to": list(to_addresses),
    }
