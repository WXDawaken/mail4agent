from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oncall_exec_app_server import AppServerOncallExecutor
from oncall_supervisor import ClaimedDeliveryExecutionContext


class _FakeLineStream:
    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._queue.put(line + "\n")

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item


class _FakeStdin:
    def __init__(self, owner: "_FakeAppServerProcess") -> None:
        self.owner = owner
        self.closed = False
        self._buffer = ""

    def write(self, data: str) -> int:
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.owner.handle_request(line.strip())
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeAppServerProcess:
    def __init__(self, *, mode: str, thread_id: str = "thread-app-1") -> None:
        self.mode = mode
        self.thread_id = thread_id
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeLineStream()
        self.stderr = _FakeLineStream()
        self.requests: list[dict[str, object]] = []
        self._returncode: int | None = None
        self.turn_count = 0

    def handle_request(self, raw_line: str) -> None:
        request = json.loads(raw_line)
        self.requests.append(request)
        request_id = request["id"]
        method = request["method"]
        if method == "initialize":
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"protocolVersion": "2"},
                    }
                )
            )
            return
        if method == "thread/start":
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"thread": {"id": self.thread_id}},
                    }
                )
            )
            return
        if method != "turn/start":
            raise AssertionError(f"unexpected method: {method}")
        self.turn_count += 1
        turn_id = f"turn-app-{self.turn_count}"
        if self.mode == "success":
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "id": f"item-{self.turn_count}",
                                "phase": "final_answer",
                                "text": f"operator summary from app-server #{self.turn_count}",
                            }
                        },
                    }
                )
            )
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"turn": {"id": turn_id, "status": "running"}},
                    }
                )
            )
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"id": turn_id, "status": "completed"}},
                    }
                )
            )
            return
        if self.mode == "turn-failed":
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"turn": {"id": turn_id, "status": "running"}},
                    }
                )
            )
            self.stderr.push("app-server stderr warning")
            self.stdout.push(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "turn": {
                                "id": turn_id,
                                "status": "failed",
                                "error": {"message": "backend failed"},
                            }
                        },
                    }
                )
            )
            return
        if self.mode == "transport-error":
            self.stdout.close()
            return
        raise AssertionError(f"unsupported fake mode: {self.mode}")

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = 0
        self.stdout.close()
        self.stderr.close()

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = 1
        self.stdout.close()
        self.stderr.close()


class OncallExecAppServerTests(unittest.TestCase):
    def test_build_exposes_app_server_execution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / ".tmp_oncall"
            with patch(
                "oncall_exec_app_server._resolve_app_server_command_prefix",
                return_value=("cmd.exe", "/c", "codex.cmd"),
            ):
                executor = AppServerOncallExecutor.build(
                    root=root,
                    role="operator",
                    runtime_dir=runtime_dir,
                    reasoning_effort="high",
                    bootstrap_summary={
                        "project_id": "mail4agent",
                        "harness_id": "local",
                    },
                )

            self.assertEqual(executor.execution_metadata["execution_mode"], "codex-app-server")
            self.assertEqual(executor.execution_metadata["backend_name"], "app-server")
            self.assertEqual(executor.execution_metadata["worker_kind"], "app-server-thread")
            self.assertTrue(executor.execution_metadata["supports_worker_reuse"])
            self.assertEqual(executor.execution_metadata["reasoning_effort"], "high")
            self.assertEqual(executor.execution_metadata["worker_idle_timeout_seconds"], 900.0)
            self.assertEqual(executor.execution_metadata["worker_max_age_seconds"], 3600.0)
            self.assertEqual(executor.execution_metadata["handler_cwd"], str(root))
            self.assertEqual(executor.execution_metadata["workspace_dir"], str(root))
            self.assertEqual(executor.execution_metadata["workspace_root_dir"], str(root))
            self.assertEqual(
                executor.execution_metadata["codex_home_dir"],
                str((root / ".codex_home_oncall").resolve()),
            )
            self.assertEqual(
                executor.execution_metadata["mailbox_client_path"],
                str((root / "client.py").resolve()),
            )
            self.assertEqual(
                executor.execution_metadata["last_message_path"],
                str(runtime_dir / "operator-oncall-last-message.txt"),
            )
            self.assertEqual(
                executor.command,
                ("cmd.exe", "/c", "codex.cmd", "app-server", "--listen", "stdio://"),
            )

    def test_build_can_target_custom_workspace_and_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            workspace_dir = Path(temp_dir) / "workspace"
            codex_home_dir = Path(temp_dir) / "codex_home"
            root.mkdir(parents=True, exist_ok=True)
            runtime_dir = Path(temp_dir) / "runtime"
            with patch(
                "oncall_exec_app_server._resolve_app_server_command_prefix",
                return_value=("codex",),
            ):
                executor = AppServerOncallExecutor.build(
                    root=root,
                    role="operator",
                    runtime_dir=runtime_dir,
                    reasoning_effort="high",
                    bootstrap_summary={
                        "project_id": "mail4agent",
                        "harness_id": "local",
                    },
                    workspace_dir=workspace_dir,
                    codex_home_dir=codex_home_dir,
                    worker_idle_timeout_seconds=120.0,
                    worker_max_age_seconds=None,
                )

            self.assertEqual(executor.cwd, workspace_dir.resolve())
            self.assertEqual(executor.execution_metadata["workspace_dir"], str(workspace_dir.resolve()))
            self.assertEqual(executor.execution_metadata["workspace_root_dir"], str(workspace_dir.resolve()))
            self.assertEqual(executor.execution_metadata["codex_home_dir"], str(codex_home_dir.resolve()))
            self.assertEqual(executor.execution_metadata["worker_idle_timeout_seconds"], 120.0)
            self.assertEqual(executor.execution_metadata["worker_max_age_seconds"], None)
            self.assertEqual(executor.prompt_path, (workspace_dir / "docs/operator-oncall-prompt.txt").resolve())
            self.assertEqual(executor.command, ("codex", "app-server", "--listen", "stdio://"))

    def test_resolve_workspace_assignment_accepts_child_workspace_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_workspace = root / "child-workspace"
            child_workspace.mkdir(parents=True, exist_ok=True)
            runtime_dir = root / "runtime"
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            assignment = executor.resolve_workspace_assignment(
                {
                    **self._sample_delivery(),
                    "payload": {
                        "workspace_dir": "child-workspace",
                    },
                },
                None,
            )

            self.assertEqual(assignment["workspace_dir"], str(child_workspace.resolve()))
            self.assertEqual(assignment["workspace_root_dir"], str(root.resolve()))
            self.assertEqual(assignment["workspace_source"], "delivery.payload.workspace_dir")
            self.assertEqual(assignment["mailbox_client_path"], str((root / "client.py").resolve()))

    def test_execute_claimed_delivery_runs_stdio_protocol_and_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="success")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ), patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                result = executor.execute_claimed_delivery(
                    self._execution_context(delivery=self._sample_delivery())
                )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.metadata["worker_id"], "thread-app-1")
            self.assertEqual(result.metadata["app_server_thread_id"], "thread-app-1")
            self.assertEqual(result.metadata["app_server_turn_id"], "turn-app-1")
            self.assertEqual(result.metadata["app_server_turn_status"], "completed")
            self.assertEqual(result.metadata["handoff_summary"], "operator summary from app-server #1")
            prompt_text = str(fake_process.requests[2]["params"]["input"][0]["text"])
            self.assertIn("Workspace root:", prompt_text)
            self.assertIn(str(root), prompt_text)
            self.assertIn("delivery_id: 101", prompt_text)
            self.assertIn(f'python "{(root / "client.py").resolve()}" reply --delivery-file', prompt_text)
            self.assertEqual(
                executor.last_message_path.read_text(encoding="utf-8").strip(),
                "operator summary from app-server #1",
            )
            executor.close()

    def test_execute_claimed_delivery_surfaces_turn_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="turn-failed")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ), patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                result = executor.execute_claimed_delivery(
                    self._execution_context(delivery=self._sample_delivery())
                )

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.metadata["app_server_turn_status"], "failed")
            self.assertEqual(result.metadata["turn_error"], "backend failed")
            self.assertEqual(result.metadata["app_server_stderr_tail"], ["app-server stderr warning"])
            self.assertNotIn("handoff_summary", result.metadata)
            executor.close()

    def test_execute_claimed_delivery_converts_transport_error_to_failure_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="transport-error")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ), patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                result = executor.execute_claimed_delivery(
                    self._execution_context(delivery=self._sample_delivery())
                )

            self.assertEqual(result.exit_code, 1)
            self.assertIn("execution_error", result.metadata)
            self.assertIn("app-server exited", result.metadata["execution_error"])
            executor.close()

    def test_execute_claimed_delivery_reuses_live_worker_for_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="success")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ) as mocked_popen, patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                first_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery=self._sample_delivery(),
                        thread_assignment={"worker_id": "app-server-thread-placeholder"},
                    )
                )
                second_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "delivery_id": 102,
                            "message_id": "msg-102",
                        },
                        thread_assignment={
                            "worker_id": "thread-app-1",
                            "previous_worker_id": "thread-app-1",
                            "reused_worker": True,
                        },
                        existing_thread_state={
                            "worker_id": "thread-app-1",
                            "last_processed_message_id": "msg-101",
                            "handoff_summary": "should not be injected during live reuse",
                        },
                    )
                )

            self.assertEqual(first_result.exit_code, 0)
            self.assertEqual(second_result.exit_code, 0)
            self.assertEqual(first_result.metadata["worker_id"], "thread-app-1")
            self.assertEqual(second_result.metadata["worker_id"], "thread-app-1")
            self.assertEqual(second_result.metadata["app_server_turn_id"], "turn-app-2")
            self.assertEqual(second_result.metadata["handoff_summary"], "operator summary from app-server #2")
            self.assertTrue(executor.can_reuse_worker("thread-app-1"))
            self.assertEqual(mocked_popen.call_count, 1)
            request_methods = [str(request["method"]) for request in fake_process.requests]
            self.assertEqual(
                request_methods,
                ["initialize", "thread/start", "turn/start", "turn/start"],
            )
            second_prompt = str(fake_process.requests[3]["params"]["input"][0]["text"])
            self.assertIn("message_id: msg-102", second_prompt)
            self.assertNotIn("Recovered thread context from the previous oncall run", second_prompt)
            self.assertFalse(second_result.metadata["recovered_handoff_summary_used"])
            executor.close()

    def test_execute_claimed_delivery_includes_recovered_handoff_summary_for_new_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="success", thread_id="thread-app-2")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ), patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "delivery_id": 104,
                            "message_id": "msg-104",
                        },
                        thread_assignment={
                            "worker_id": "thread-app-old",
                            "previous_worker_id": "thread-app-old",
                            "reused_worker": False,
                            "recovery_reason": "previous_worker_not_available",
                        },
                        existing_thread_state={
                            "worker_id": "thread-app-old",
                            "last_processed_message_id": "msg-103",
                            "handoff_summary": "previous operator summary line 1\nprevious operator summary line 2",
                        },
                    )
                )

            prompt_text = str(fake_process.requests[2]["params"]["input"][0]["text"])
            self.assertIn("Recovered thread context from the previous oncall run", prompt_text)
            self.assertIn("recovery_reason: previous_worker_not_available", prompt_text)
            self.assertIn("previous_worker_id: thread-app-old", prompt_text)
            self.assertIn("last_processed_message_id: msg-103", prompt_text)
            self.assertIn("previous operator summary line 1", prompt_text)
            self.assertIn("previous operator summary line 2", prompt_text)
            self.assertTrue(result.metadata["recovered_handoff_summary_used"])
            self.assertEqual(result.metadata["recovered_recovery_reason"], "previous_worker_not_available")
            self.assertEqual(result.metadata["recovered_last_processed_message_id"], "msg-103")
            self.assertEqual(result.metadata["recovered_previous_worker_id"], "thread-app-old")
            executor.close()

    def test_can_reuse_worker_prunes_idle_worker_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            fake_process = _FakeAppServerProcess(mode="success")
            executor = self._build_executor(
                root=root,
                runtime_dir=runtime_dir,
                worker_idle_timeout_seconds=5.0,
                worker_max_age_seconds=None,
            )

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                return_value=fake_process,
            ), patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                executor.execute_claimed_delivery(
                    self._execution_context(delivery=self._sample_delivery())
                )

            worker = executor.workers["thread-app-1"]
            worker.last_used_at -= 30.0
            executor.on_idle()

            self.assertFalse(executor.can_reuse_worker("thread-app-1"))
            self.assertNotIn("thread-app-1", executor.workers)

    def test_execute_claimed_delivery_recreates_worker_after_max_age(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            first_process = _FakeAppServerProcess(mode="success", thread_id="thread-app-1")
            second_process = _FakeAppServerProcess(mode="success", thread_id="thread-app-2")
            executor = self._build_executor(
                root=root,
                runtime_dir=runtime_dir,
                worker_idle_timeout_seconds=None,
                worker_max_age_seconds=10.0,
            )

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                side_effect=[first_process, second_process],
            ) as mocked_popen, patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                first_result = executor.execute_claimed_delivery(
                    self._execution_context(delivery=self._sample_delivery())
                )
                executor.workers["thread-app-1"].started_at -= 30.0
                second_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "delivery_id": 103,
                            "message_id": "msg-103",
                        },
                        thread_assignment={"worker_id": "thread-app-1"},
                    )
                )

            self.assertEqual(first_result.metadata["worker_id"], "thread-app-1")
            self.assertEqual(second_result.metadata["worker_id"], "thread-app-2")
            self.assertEqual(mocked_popen.call_count, 2)
            executor.close()

    def test_execute_claimed_delivery_keeps_separate_live_workers_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir(parents=True, exist_ok=True)
            workspace_b.mkdir(parents=True, exist_ok=True)
            runtime_dir = root / "runtime"
            self._write_runtime_files(root, runtime_dir)
            first_process = _FakeAppServerProcess(mode="success", thread_id="thread-app-a")
            second_process = _FakeAppServerProcess(mode="success", thread_id="thread-app-b")
            executor = self._build_executor(root=root, runtime_dir=runtime_dir)

            with patch(
                "oncall_exec_app_server.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["client.py"], returncode=0, stdout="session-token\n", stderr=""),
            ), patch(
                "oncall_exec_app_server.subprocess.Popen",
                side_effect=[first_process, second_process],
            ) as mocked_popen, patch(
                "oncall_exec_app_server._copy_codex_auth_files",
                return_value=None,
            ):
                first_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "payload": {"workspace_dir": "workspace-a"},
                        },
                        thread_assignment={
                            "workspace_dir": str(workspace_a.resolve()),
                            "workspace_root_dir": str(root.resolve()),
                            "workspace_source": "delivery.payload.workspace_dir",
                            "worker_binding_key": "operator@example.test::thread-101::workspace-a",
                        },
                    )
                )
                second_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "delivery_id": 102,
                            "message_id": "msg-102",
                            "payload": {"workspace_dir": "workspace-b"},
                        },
                        thread_assignment={
                            "workspace_dir": str(workspace_b.resolve()),
                            "workspace_root_dir": str(root.resolve()),
                            "workspace_source": "delivery.payload.workspace_dir",
                            "worker_binding_key": "operator@example.test::thread-101::workspace-b",
                        },
                    )
                )
                third_result = executor.execute_claimed_delivery(
                    self._execution_context(
                        delivery={
                            **self._sample_delivery(),
                            "delivery_id": 103,
                            "message_id": "msg-103",
                            "payload": {"workspace_dir": "workspace-a"},
                        },
                        thread_assignment={
                            "worker_id": "thread-app-a",
                            "previous_worker_id": "thread-app-a",
                            "reused_worker": True,
                            "workspace_dir": str(workspace_a.resolve()),
                            "workspace_root_dir": str(root.resolve()),
                            "workspace_source": "delivery.payload.workspace_dir",
                            "worker_binding_key": "operator@example.test::thread-101::workspace-a",
                        },
                        existing_thread_state={
                            "workspace_dir": str(workspace_a.resolve()),
                            "worker_id": "thread-app-a",
                        },
                    )
                )

            self.assertEqual(first_result.metadata["worker_id"], "thread-app-a")
            self.assertEqual(second_result.metadata["worker_id"], "thread-app-b")
            self.assertEqual(third_result.metadata["worker_id"], "thread-app-a")
            self.assertEqual(third_result.metadata["workspace_dir"], str(workspace_a.resolve()))
            self.assertEqual(mocked_popen.call_count, 2)
            self.assertEqual(first_process.requests[1]["params"]["cwd"], str(workspace_a.resolve()))
            self.assertEqual(second_process.requests[1]["params"]["cwd"], str(workspace_b.resolve()))
            self.assertEqual(first_process.requests[2]["params"]["cwd"], str(workspace_a.resolve()))
            self.assertEqual(second_process.requests[2]["params"]["cwd"], str(workspace_b.resolve()))
            self.assertEqual(first_process.requests[3]["params"]["cwd"], str(workspace_a.resolve()))
            executor.close()

    def _build_executor(
        self,
        *,
        root: Path,
        runtime_dir: Path,
        worker_idle_timeout_seconds: float | None = 900.0,
        worker_max_age_seconds: float | None = 3600.0,
    ) -> AppServerOncallExecutor:
        with patch(
            "oncall_exec_app_server._resolve_app_server_command_prefix",
            return_value=("codex",),
        ):
            return AppServerOncallExecutor.build(
                root=root,
                role="operator",
                runtime_dir=runtime_dir,
                reasoning_effort="high",
                bootstrap_summary={
                    "project_id": "mail4agent",
                    "harness_id": "local",
                },
                worker_idle_timeout_seconds=worker_idle_timeout_seconds,
                worker_max_age_seconds=worker_max_age_seconds,
            )

    def _write_runtime_files(self, root: Path, runtime_dir: Path) -> None:
        (root / "docs").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "operator-oncall-prompt.txt").write_text(
            "Follow the mailbox task and keep the repo update bounded.\n",
            encoding="utf-8",
        )
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "harness.token").write_text("harness-token\n", encoding="utf-8")
        (runtime_dir / "operator.mailbox_client.json").write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:8787",
                    "project_id": "mail4agent",
                    "role": "operator",
                    "local_part": "operator",
                    "mailbox_type": "group",
                    "agent_name": "operator-agent",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _sample_delivery(self) -> dict[str, object]:
        return {
            "delivery_id": 101,
            "message_id": "msg-101",
            "thread_id": "thread-101",
            "claim_token": "claim-101",
            "from": "planner@example.test",
            "to": "operator@example.test",
        }

    def _execution_context(
        self,
        *,
        delivery: dict[str, object],
        thread_assignment: dict[str, object] | None = None,
        existing_thread_state: dict[str, object] | None = None,
    ) -> ClaimedDeliveryExecutionContext:
        return ClaimedDeliveryExecutionContext(
            delivery=delivery,
            thread_assignment=dict(thread_assignment or {}),
            existing_thread_state=dict(existing_thread_state or {}) or None,
        )
