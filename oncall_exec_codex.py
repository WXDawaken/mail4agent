from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailbox_worker import run_subprocess_handler
from oncall_exec_common import build_handler_base_env, read_bounded_text
from oncall_supervisor import ClaimedDeliveryExecutionContext, ClaimedDeliveryExecutionResult


@dataclass(frozen=True)
class CodexOncallExecutor:
    role: str
    root: Path
    runtime_dir: Path
    reasoning_effort: str
    workspace_dir: Path
    codex_home_dir: Path | None
    command: tuple[str, ...]
    cwd: Path
    base_env: dict[str, str]
    last_message_path: Path
    runtime_delivery_path: Path
    execution_metadata: dict[str, Any]

    @classmethod
    def build(
        cls,
        *,
        root: Path,
        role: str,
        runtime_dir: Path,
        reasoning_effort: str,
        bootstrap_summary: dict[str, Any],
        workspace_dir: Path | None = None,
        codex_home_dir: Path | None = None,
    ) -> CodexOncallExecutor:
        resolved_workspace_dir = (workspace_dir or root).resolve()
        resolved_codex_home_dir = codex_home_dir.resolve() if codex_home_dir is not None else None
        launcher_path = root / "launch_oncall_agent.ps1"
        command_parts = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher_path),
            role,
            "-RuntimeDir",
            str(runtime_dir),
            "-ReasoningEffort",
            reasoning_effort,
        ]
        if resolved_workspace_dir != root.resolve():
            command_parts.extend(("-WorkspaceDir", str(resolved_workspace_dir)))
        if resolved_codex_home_dir is not None:
            command_parts.extend(("-CodexHomeDir", str(resolved_codex_home_dir)))
        command = tuple(command_parts)
        base_env = build_handler_base_env(
            bootstrap_summary=bootstrap_summary,
            role=role,
            runtime_dir=runtime_dir,
        )
        last_message_path = runtime_dir / f"{role}-oncall-last-message.txt"
        runtime_delivery_path = runtime_dir / f"{role}-current-delivery.json"
        execution_metadata = {
            "backend_name": "codex-cli",
            "execution_mode": "codex-cli",
            "worker_kind": "codex-cli-run",
            "supports_worker_reuse": False,
            "reasoning_effort": reasoning_effort,
            "handler_command": list(command),
            "handler_cwd": str(resolved_workspace_dir),
            "workspace_dir": str(resolved_workspace_dir),
            "last_message_path": str(last_message_path),
            "runtime_delivery_path": str(runtime_delivery_path),
        }
        if resolved_codex_home_dir is not None:
            execution_metadata["codex_home_dir"] = str(resolved_codex_home_dir)
        return cls(
            role=role,
            root=root,
            runtime_dir=runtime_dir,
            reasoning_effort=reasoning_effort,
            workspace_dir=resolved_workspace_dir,
            codex_home_dir=resolved_codex_home_dir,
            command=command,
            cwd=resolved_workspace_dir,
            base_env=base_env,
            last_message_path=last_message_path,
            runtime_delivery_path=runtime_delivery_path,
            execution_metadata=execution_metadata,
        )

    def execute_claimed_delivery(
        self,
        context: ClaimedDeliveryExecutionContext,
    ) -> ClaimedDeliveryExecutionResult:
        delivery = context.delivery
        try:
            self.last_message_path.unlink(missing_ok=True)
        except OSError:
            pass
        exit_code = run_subprocess_handler(
            delivery,
            list(self.command),
            cwd=str(self.cwd),
            base_env=self.base_env,
        )
        metadata = dict(self.execution_metadata)
        handoff_summary = read_bounded_text(self.last_message_path)
        if handoff_summary is not None:
            metadata["handoff_summary"] = handoff_summary
        return ClaimedDeliveryExecutionResult(
            exit_code=int(exit_code),
            metadata=metadata,
        )
