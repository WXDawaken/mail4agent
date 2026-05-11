from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from oncall_exec_codex import CodexOncallExecutor
from oncall_supervisor import ClaimedDeliveryExecutionContext


class OncallExecCodexTests(unittest.TestCase):
    def test_build_exposes_codex_execution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / ".tmp_oncall"
            executor = CodexOncallExecutor.build(
                root=root,
                role="operator",
                runtime_dir=runtime_dir,
                reasoning_effort="high",
                bootstrap_summary={
                    "project_id": "mail4agent",
                    "harness_id": "local",
                },
            )

            self.assertEqual(executor.execution_metadata["execution_mode"], "codex-cli")
            self.assertEqual(executor.execution_metadata["backend_name"], "codex-cli")
            self.assertEqual(executor.execution_metadata["worker_kind"], "codex-cli-run")
            self.assertFalse(executor.execution_metadata["supports_worker_reuse"])
            self.assertEqual(executor.execution_metadata["reasoning_effort"], "high")
            self.assertEqual(executor.execution_metadata["handler_cwd"], str(root))
            self.assertEqual(
                executor.execution_metadata["last_message_path"],
                str(runtime_dir / "operator-oncall-last-message.txt"),
            )
            self.assertIn("launch_oncall_agent.ps1", " ".join(executor.command))

    def test_build_can_target_custom_workspace_and_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            workspace_dir = Path(temp_dir) / "workspace"
            codex_home_dir = Path(temp_dir) / "codex_home"
            root.mkdir(parents=True, exist_ok=True)
            runtime_dir = Path(temp_dir) / "runtime"
            executor = CodexOncallExecutor.build(
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
            )

            self.assertEqual(executor.cwd, workspace_dir.resolve())
            self.assertEqual(executor.execution_metadata["workspace_dir"], str(workspace_dir.resolve()))
            self.assertEqual(executor.execution_metadata["codex_home_dir"], str(codex_home_dir.resolve()))
            self.assertIn("-WorkspaceDir", executor.command)
            self.assertIn(str(workspace_dir.resolve()), executor.command)
            self.assertIn("-CodexHomeDir", executor.command)
            self.assertIn(str(codex_home_dir.resolve()), executor.command)

    def test_execute_claimed_delivery_uses_subprocess_handler_with_oncall_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / ".tmp_oncall"
            executor = CodexOncallExecutor.build(
                root=root,
                role="operator",
                runtime_dir=runtime_dir,
                reasoning_effort="xhigh",
                bootstrap_summary={
                    "project_id": "mail4agent",
                    "harness_id": "local",
                },
            )
            delivery = {
                "delivery_id": 101,
                "message_id": "msg-101",
                "thread_id": "thread-101",
                "claim_token": "claim-101",
                "from": "planner@example.test",
                "to": "operator@example.test",
            }

            def _fake_run(*args, **kwargs):
                executor.last_message_path.parent.mkdir(parents=True, exist_ok=True)
                executor.last_message_path.write_text("operator summary", encoding="utf-8")
                return 7

            with patch("oncall_exec_codex.run_subprocess_handler", side_effect=_fake_run) as mocked_run:
                result = executor.execute_claimed_delivery(
                    ClaimedDeliveryExecutionContext(
                        delivery=delivery,
                        thread_assignment={},
                    )
                )

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.metadata["execution_mode"], "codex-cli")
            self.assertEqual(result.metadata["handoff_summary"], "operator summary")
            mocked_run.assert_called_once()
            called_delivery, called_command = mocked_run.call_args.args
            self.assertEqual(called_delivery, delivery)
            self.assertEqual(called_command, list(executor.command))
            self.assertEqual(mocked_run.call_args.kwargs["cwd"], str(root))
            base_env = mocked_run.call_args.kwargs["base_env"]
            self.assertEqual(base_env["MAILBOX_ONCALL_MODE"], "1")
            self.assertEqual(base_env["MAILBOX_ONCALL_ROLE"], "operator")
            self.assertEqual(base_env["MAILBOX_ONCALL_RUNTIME_DIR"], str(runtime_dir))
            self.assertEqual(base_env["MAILBOX_HARNESS_ID"], "local")
            self.assertEqual(base_env["MAILBOX_PROJECT_ID"], "mail4agent")
