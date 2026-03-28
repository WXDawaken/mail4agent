from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oncall_exec_common import build_handler_base_env, read_bounded_text
from oncall_supervisor import ClaimedDeliveryExecutionContext, ClaimedDeliveryExecutionResult


@dataclass(frozen=True)
class AppServerRoleSpec:
    config_file: str
    prompt_file: str


ROLE_SPECS: dict[str, AppServerRoleSpec] = {
    "operator": AppServerRoleSpec(
        config_file="operator.mailbox_client.json",
        prompt_file="docs/dogfood-high-operator-oncall-prompt.txt",
    ),
}

DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS = 900.0
DEFAULT_WORKER_MAX_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class _ResolvedWorkspaceContext:
    workspace_dir: Path
    workspace_root_dir: Path
    prompt_path: Path
    mailbox_client_path: Path
    codex_home_dir: Path
    workspace_source: str | None
    requested_workspace_dir: str | None
    resolution_error: str | None
    worker_binding_key: str | None


@dataclass(frozen=True)
class AppServerOncallExecutor:
    role: str
    root: Path
    runtime_dir: Path
    reasoning_effort: str
    workspace_dir: Path
    workspace_root_dir: Path
    codex_home_dir: Path
    command: tuple[str, ...]
    cwd: Path
    base_env: dict[str, str]
    prompt_path: Path
    mailbox_client_path: Path
    runtime_config_path: Path
    runtime_delivery_path: Path
    last_message_path: Path
    worker_idle_timeout_seconds: float | None
    worker_max_age_seconds: float | None
    execution_metadata: dict[str, Any]
    workers: dict[str, "_AppServerWorker"] = field(default_factory=dict)
    worker_bindings: dict[str, str] = field(default_factory=dict)
    workers_lock: threading.Lock = field(default_factory=threading.Lock)

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
        worker_idle_timeout_seconds: float | None = DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS,
        worker_max_age_seconds: float | None = DEFAULT_WORKER_MAX_AGE_SECONDS,
    ) -> AppServerOncallExecutor:
        role_spec = ROLE_SPECS[role]
        resolved_workspace_dir = (workspace_dir or root).resolve()
        resolved_workspace_root_dir = resolved_workspace_dir
        resolved_codex_home_dir = (
            codex_home_dir.resolve()
            if codex_home_dir is not None
            else (resolved_workspace_root_dir / ".codex_home_dogfood").resolve()
        )
        prompt_path = (resolved_workspace_root_dir / role_spec.prompt_file).resolve()
        mailbox_client_path = (resolved_workspace_root_dir / "client.py").resolve()
        runtime_config_path = (runtime_dir / role_spec.config_file).resolve()
        runtime_delivery_path = (runtime_dir / f"{role}-current-delivery.json").resolve()
        last_message_path = (runtime_dir / f"{role}-oncall-last-message.txt").resolve()
        command = _build_app_server_command()
        base_env = build_handler_base_env(
            bootstrap_summary=bootstrap_summary,
            role=role,
            runtime_dir=runtime_dir,
        )
        execution_metadata = {
            "backend_name": "app-server",
            "execution_mode": "codex-app-server",
            "worker_kind": "app-server-thread",
            "supports_worker_reuse": True,
            "reasoning_effort": reasoning_effort,
            "handler_command": list(command),
            "handler_cwd": str(resolved_workspace_dir),
            "workspace_dir": str(resolved_workspace_dir),
            "workspace_root_dir": str(resolved_workspace_root_dir),
            "workspace_source": "default_workspace_dir",
            "codex_home_dir": str(resolved_codex_home_dir),
            "last_message_path": str(last_message_path),
            "runtime_delivery_path": str(runtime_delivery_path),
            "prompt_path": str(prompt_path),
            "mailbox_client_path": str(mailbox_client_path),
            "worker_idle_timeout_seconds": worker_idle_timeout_seconds,
            "worker_max_age_seconds": worker_max_age_seconds,
        }
        return cls(
            role=role,
            root=root,
            runtime_dir=runtime_dir.resolve(),
            reasoning_effort=reasoning_effort,
            workspace_dir=resolved_workspace_dir,
            workspace_root_dir=resolved_workspace_root_dir,
            codex_home_dir=resolved_codex_home_dir,
            command=command,
            cwd=resolved_workspace_dir,
            base_env=base_env,
            prompt_path=prompt_path,
            mailbox_client_path=mailbox_client_path,
            runtime_config_path=runtime_config_path,
            runtime_delivery_path=runtime_delivery_path,
            last_message_path=last_message_path,
            worker_idle_timeout_seconds=worker_idle_timeout_seconds,
            worker_max_age_seconds=worker_max_age_seconds,
            execution_metadata=execution_metadata,
        )

    def can_reuse_worker(self, worker_id: str) -> bool:
        self._prune_workers()
        worker = self._live_worker(worker_id)
        return worker is not None

    def on_idle(self) -> None:
        self._prune_workers()

    def resolve_workspace_assignment(
        self,
        delivery: dict[str, Any],
        existing_thread_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_workspace = _resolve_workspace_context(
            delivery=delivery,
            existing_thread_state=existing_thread_state,
            default_workspace_dir=self.workspace_dir,
            workspace_root_dir=self.workspace_root_dir,
            prompt_path=self.prompt_path,
            mailbox_client_path=self.mailbox_client_path,
            codex_home_dir=self.codex_home_dir,
        )
        return _workspace_context_metadata(resolved_workspace)

    def close(self) -> None:
        with self.workers_lock:
            workers = list(self.workers.values())
            self.workers.clear()
            self.worker_bindings.clear()
        for worker in workers:
            worker.close()

    def execute_claimed_delivery(
        self,
        context: ClaimedDeliveryExecutionContext,
    ) -> ClaimedDeliveryExecutionResult:
        delivery = context.delivery
        preferred_worker_id = _thread_assignment_worker_id(context.thread_assignment)
        resolved_workspace = _workspace_context_from_assignment(
            thread_assignment=context.thread_assignment,
            default_workspace_dir=self.workspace_dir,
            workspace_root_dir=self.workspace_root_dir,
            prompt_path=self.prompt_path,
            mailbox_client_path=self.mailbox_client_path,
            codex_home_dir=self.codex_home_dir,
        )
        workspace_metadata = _workspace_context_metadata(resolved_workspace)
        worker_binding_key = resolved_workspace.worker_binding_key
        self._prune_workers()
        self.last_message_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.last_message_path.unlink(missing_ok=True)
        except OSError:
            pass
        worker: _AppServerWorker | None = None
        thread_id: str | None = None
        turn_id: str | None = None
        recovery_context_metadata: dict[str, Any] = {}
        try:
            env, prompt_text, recovery_context_metadata = self._prepare_execution_context(
                context,
                resolved_workspace=resolved_workspace,
            )
            worker = self._get_or_create_worker(
                preferred_worker_id=preferred_worker_id,
                worker_binding_key=worker_binding_key,
                resolved_workspace=resolved_workspace,
                env=env,
            )
            thread_id = worker.worker_id
            turn_id, completion = worker.run_turn(
                prompt_text=prompt_text,
                reasoning_effort=self.reasoning_effort,
            )
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            if thread_id is not None:
                self._discard_worker(thread_id)
            metadata = dict(self.execution_metadata)
            if thread_id is not None:
                metadata["worker_id"] = thread_id
                metadata["app_server_thread_id"] = thread_id
            if turn_id is not None:
                metadata["app_server_turn_id"] = turn_id
            metadata["execution_error"] = str(exc)
            metadata.update(workspace_metadata)
            metadata.update(recovery_context_metadata)
            if worker is not None and worker.session.stderr_tail:
                metadata["app_server_stderr_tail"] = list(worker.session.stderr_tail)
            return ClaimedDeliveryExecutionResult(
                exit_code=1,
                metadata=metadata,
            )

        final_message = _select_final_agent_message(completion["agent_messages"])
        if final_message is not None:
            self.last_message_path.write_text(final_message + "\n", encoding="utf-8")
        stderr_tail = list(completion["stderr_tail"])
        if not stderr_tail and worker is not None:
            stderr_tail = _capture_stderr_tail(worker.session)
        metadata = {
            **dict(self.execution_metadata),
            "worker_id": thread_id,
            "app_server_thread_id": thread_id,
            "app_server_turn_id": turn_id,
            "app_server_turn_status": completion["status"],
        }
        metadata.update(workspace_metadata)
        metadata.update(recovery_context_metadata)
        if completion["error"] is not None:
            metadata["turn_error"] = completion["error"]
        elif completion["status"] != "completed":
            metadata["turn_error"] = f"turn completed with status {completion['status']}"
        if stderr_tail and completion["status"] != "completed":
            metadata["app_server_stderr_tail"] = stderr_tail
        handoff_summary = read_bounded_text(self.last_message_path)
        if handoff_summary is not None:
            metadata["handoff_summary"] = handoff_summary
        exit_code = 0 if completion["status"] == "completed" else 1
        if thread_id is not None:
            self._retire_worker_if_needed(thread_id)
        return ClaimedDeliveryExecutionResult(
            exit_code=exit_code,
            metadata=metadata,
        )

    def _get_or_create_worker(
        self,
        *,
        preferred_worker_id: str | None,
        worker_binding_key: str | None,
        resolved_workspace: _ResolvedWorkspaceContext,
        env: dict[str, str],
    ) -> "_AppServerWorker":
        self._prune_workers()
        existing_worker = self._live_worker(preferred_worker_id)
        if existing_worker is None and worker_binding_key is not None:
            bound_worker_id = self._binding_worker_id(worker_binding_key)
            existing_worker = self._live_worker(bound_worker_id)
        if existing_worker is not None:
            self._bind_worker(worker_binding_key=worker_binding_key, worker_id=existing_worker.worker_id)
            return existing_worker
        worker = _AppServerWorker.start(
            command=self.command,
            cwd=resolved_workspace.workspace_dir,
            env=env,
        )
        with self.workers_lock:
            self.workers[worker.worker_id] = worker
        self._bind_worker(worker_binding_key=worker_binding_key, worker_id=worker.worker_id)
        return worker

    def _live_worker(self, worker_id: str | None) -> "_AppServerWorker" | None:
        if worker_id is None:
            return None
        stale_worker: _AppServerWorker | None = None
        with self.workers_lock:
            worker = self.workers.get(worker_id)
            if worker is None:
                return None
            if worker.is_alive():
                return worker
            stale_worker = self.workers.pop(worker_id, None)
            self._remove_worker_bindings_locked(worker_id=worker_id)
        if stale_worker is not None:
            stale_worker.close()
        return None

    def _discard_worker(self, worker_id: str) -> None:
        stale_worker: _AppServerWorker | None = None
        with self.workers_lock:
            stale_worker = self.workers.pop(worker_id, None)
            self._remove_worker_bindings_locked(worker_id=worker_id)
        if stale_worker is not None:
            stale_worker.close()

    def _retire_worker_if_needed(self, worker_id: str) -> None:
        now = _now()
        stale_worker: _AppServerWorker | None = None
        with self.workers_lock:
            worker = self.workers.get(worker_id)
            if worker is None:
                return
            if worker.eviction_reason(
                now=now,
                worker_idle_timeout_seconds=self.worker_idle_timeout_seconds,
                worker_max_age_seconds=self.worker_max_age_seconds,
            ) is None:
                return
            stale_worker = self.workers.pop(worker_id, None)
            self._remove_worker_bindings_locked(worker_id=worker_id)
        if stale_worker is not None:
            stale_worker.close()

    def _prune_workers(self) -> None:
        now = _now()
        stale_workers: list[_AppServerWorker] = []
        with self.workers_lock:
            for worker_id, worker in list(self.workers.items()):
                if worker.eviction_reason(
                    now=now,
                    worker_idle_timeout_seconds=self.worker_idle_timeout_seconds,
                    worker_max_age_seconds=self.worker_max_age_seconds,
                ) is None:
                    continue
                stale_worker = self.workers.pop(worker_id, None)
                if stale_worker is not None:
                    self._remove_worker_bindings_locked(worker_id=worker_id)
                    stale_workers.append(stale_worker)
        for worker in stale_workers:
            worker.close()

    def _binding_worker_id(self, worker_binding_key: str) -> str | None:
        with self.workers_lock:
            value = self.worker_bindings.get(worker_binding_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _bind_worker(self, *, worker_binding_key: str | None, worker_id: str) -> None:
        if worker_binding_key is None:
            return
        with self.workers_lock:
            self.worker_bindings[worker_binding_key] = worker_id

    def _remove_worker_bindings_locked(self, *, worker_id: str) -> None:
        for binding_key, bound_worker_id in list(self.worker_bindings.items()):
            if bound_worker_id == worker_id:
                self.worker_bindings.pop(binding_key, None)

    def _prepare_execution_context(
        self,
        context: ClaimedDeliveryExecutionContext,
        *,
        resolved_workspace: _ResolvedWorkspaceContext,
    ) -> tuple[dict[str, str], str, dict[str, Any]]:
        delivery = context.delivery
        if resolved_workspace.resolution_error is not None:
            raise RuntimeError(f"workspace resolution failed: {resolved_workspace.resolution_error}")
        if not resolved_workspace.prompt_path.exists():
            raise FileNotFoundError(f"missing oncall prompt: {resolved_workspace.prompt_path}")
        if not self.runtime_config_path.exists():
            raise FileNotFoundError(f"missing runtime config: {self.runtime_config_path}")

        role_config = json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
        if not isinstance(role_config, dict):
            raise ValueError(f"runtime config must contain a JSON object: {self.runtime_config_path}")
        harness_token_path = self.runtime_dir / "harness.token"
        if not harness_token_path.exists():
            raise FileNotFoundError(f"missing harness token: {harness_token_path}")
        harness_token = harness_token_path.read_text(encoding="utf-8").strip()
        if not harness_token:
            raise ValueError(f"empty harness token file: {harness_token_path}")

        env = dict(self.base_env)
        env["MAILBOX_TIMEOUT_SECONDS"] = "15"
        env["MAILBOX_AGENT_ROLE"] = self.role
        project_id = str(role_config.get("project_id") or env.get("MAILBOX_PROJECT_ID") or "").strip()
        if project_id:
            env["MAILBOX_PROJECT_ID"] = project_id
        env["MAILBOX_TOKEN"] = harness_token
        for key, env_name in (
            ("delivery_id", "MAILBOX_DELIVERY_ID"),
            ("message_id", "MAILBOX_MESSAGE_ID"),
            ("thread_id", "MAILBOX_THREAD_ID"),
            ("claim_token", "MAILBOX_CLAIM_TOKEN"),
            ("from", "MAILBOX_DELIVERY_FROM_ADDRESS"),
            ("to", "MAILBOX_DELIVERY_TO_ADDRESS"),
        ):
            value = delivery.get(key)
            if value is not None and str(value).strip():
                env[env_name] = str(value).strip()
        env["CODEX_HOME"] = str(resolved_workspace.codex_home_dir)
        env["MAILBOX_WORKSPACE_DIR"] = str(resolved_workspace.workspace_dir)
        env["MAILBOX_WORKSPACE_ROOT_DIR"] = str(resolved_workspace.workspace_root_dir)
        if resolved_workspace.workspace_source is not None:
            env["MAILBOX_WORKSPACE_SOURCE"] = resolved_workspace.workspace_source
        if resolved_workspace.requested_workspace_dir is not None:
            env["MAILBOX_REQUESTED_WORKSPACE_DIR"] = resolved_workspace.requested_workspace_dir
        env.pop("MAILBOX_FROM_ADDRESS", None)
        env.pop("MAILBOX_INBOX_ADDRESS", None)

        sandbox_runtime_dir = (resolved_workspace.workspace_dir / ".tmp_dogfood_live" / f"{self.role}-oncall").resolve()
        sandbox_runtime_dir.mkdir(parents=True, exist_ok=True)
        resolved_workspace.codex_home_dir.mkdir(parents=True, exist_ok=True)
        sandbox_config_path = sandbox_runtime_dir / self.runtime_config_path.name
        sandbox_delivery_path = sandbox_runtime_dir / self.runtime_delivery_path.name
        shutil.copy2(self.runtime_config_path, sandbox_config_path)
        _write_json_file(self.runtime_delivery_path, delivery)
        _write_json_file(sandbox_delivery_path, delivery)
        env["MAILBOX_CONFIG"] = str(sandbox_config_path)
        env["MAILBOX_DELIVERY_FILE"] = str(sandbox_delivery_path)
        _copy_codex_auth_files(resolved_workspace.codex_home_dir)

        session_token = _run_mailbox_login(
            tooling_dir=resolved_workspace.workspace_root_dir,
            mailbox_client_path=resolved_workspace.mailbox_client_path,
            role_config=role_config,
            env=env,
        )
        env["MAILBOX_SESSION_TOKEN"] = session_token
        env.pop("MAILBOX_TOKEN", None)
        recovery_context = _build_recovery_context(
            existing_thread_state=context.existing_thread_state,
            thread_assignment=context.thread_assignment,
        )
        if recovery_context["used"]:
            recovery_reason = recovery_context["recovery_reason"]
            recovered_last_processed_message_id = recovery_context["last_processed_message_id"]
            recovered_previous_worker_id = recovery_context["previous_worker_id"]
            if recovery_reason is not None:
                env["MAILBOX_RECOVERY_REASON"] = recovery_reason
            if recovered_last_processed_message_id is not None:
                env["MAILBOX_RECOVERED_LAST_MESSAGE_ID"] = recovered_last_processed_message_id
            if recovered_previous_worker_id is not None:
                env["MAILBOX_RECOVERED_PREVIOUS_WORKER_ID"] = recovered_previous_worker_id
        prompt_text = _build_prompt_text(
            prompt_path=resolved_workspace.prompt_path,
            workspace_dir=resolved_workspace.workspace_dir,
            mailbox_client_path=resolved_workspace.mailbox_client_path,
            delivery=delivery,
            sandbox_delivery_path=sandbox_delivery_path,
            recovery_context=recovery_context,
        )
        recovery_metadata = {
            "recovered_handoff_summary_used": bool(recovery_context["used"]),
        }
        if recovery_context["used"]:
            if recovery_context["recovery_reason"] is not None:
                recovery_metadata["recovered_recovery_reason"] = recovery_context["recovery_reason"]
            if recovery_context["last_processed_message_id"] is not None:
                recovery_metadata["recovered_last_processed_message_id"] = recovery_context["last_processed_message_id"]
            if recovery_context["previous_worker_id"] is not None:
                recovery_metadata["recovered_previous_worker_id"] = recovery_context["previous_worker_id"]
        return env, prompt_text, recovery_metadata


@dataclass
class _AppServerWorker:
    worker_id: str
    session: "_AppServerSession"
    cwd: Path
    started_at: float
    last_used_at: float
    active_turns: int = 0
    state_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def start(
        cls,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
    ) -> "_AppServerWorker":
        started_at = _now()
        session = _AppServerSession.start(
            command=command,
            cwd=cwd,
            env=env,
        )
        session.initialize()
        thread_result = session.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "model": "gpt-5.4",
                "ephemeral": True,
                "serviceName": "mail4agent-oncall",
            },
        )
        thread = _require_object(thread_result, "thread/start result")
        thread_payload = _require_object(thread.get("thread"), "thread/start thread")
        thread_id = _require_str(thread_payload.get("id"), "thread/start thread.id")
        return cls(
            worker_id=thread_id,
            session=session,
            cwd=cwd,
            started_at=started_at,
            last_used_at=started_at,
        )

    def is_alive(self) -> bool:
        return self.session.is_alive()

    def close(self) -> None:
        self.session.close()

    def run_turn(
        self,
        *,
        prompt_text: str,
        reasoning_effort: str,
    ) -> tuple[str, dict[str, Any]]:
        with self.state_lock:
            self.active_turns += 1
        try:
            turn_result = self.session.request(
                "turn/start",
                {
                    "threadId": self.worker_id,
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    "effort": reasoning_effort,
                    "input": [
                        {
                            "type": "text",
                            "text": prompt_text,
                        }
                    ],
                },
            )
            turn = _require_object(turn_result, "turn/start result").get("turn")
            turn_payload = _require_object(turn, "turn/start turn")
            turn_id = _require_str(turn_payload.get("id"), "turn/start turn.id")
            completion = self.session.wait_for_turn_completion(turn_id=turn_id)
            return turn_id, completion
        finally:
            with self.state_lock:
                self.active_turns = max(0, self.active_turns - 1)
                self.last_used_at = _now()

    def eviction_reason(
        self,
        *,
        now: float,
        worker_idle_timeout_seconds: float | None,
        worker_max_age_seconds: float | None,
    ) -> str | None:
        with self.state_lock:
            if self.active_turns > 0:
                return None
            last_used_at = self.last_used_at
            started_at = self.started_at
        if not self.is_alive():
            return "process_exited"
        if worker_max_age_seconds is not None and now - started_at >= worker_max_age_seconds:
            return "worker_max_age_exceeded"
        if worker_idle_timeout_seconds is not None and now - last_used_at >= worker_idle_timeout_seconds:
            return "worker_idle_timeout_exceeded"
        return None


@dataclass
class _AppServerSession:
    process: subprocess.Popen[str]
    stdout_queue: queue.Queue[str | None]
    stderr_tail: deque[str]
    pending_messages: deque[dict[str, Any]]
    stdout_thread: threading.Thread
    stderr_thread: threading.Thread
    _next_id: int = 1

    @classmethod
    def start(
        cls,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
    ) -> _AppServerSession:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("failed to start app-server stdio pipes")
        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=20)
        stdout_thread = threading.Thread(
            target=_pump_stream_to_queue,
            args=(process.stdout, stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_stream_to_tail,
            args=(process.stderr, stderr_tail),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        return cls(
            process=process,
            stdout_queue=stdout_queue,
            stderr_tail=stderr_tail,
            pending_messages=deque(),
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
        )

    def initialize(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "mail4agent-oncall",
                    "version": "0.1.0",
                }
            },
        )

    def request(self, method: str, params: dict[str, Any], *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = _now() + timeout_seconds
        while _now() < deadline:
            try:
                message = self._read_process_message(timeout_seconds=min(1.0, deadline - _now()))
            except TimeoutError:
                continue
            if message.get("id") == request_id:
                if "result" in message and isinstance(message["result"], dict):
                    return dict(message["result"])
                if "error" in message:
                    raise RuntimeError(f"app-server {method} failed: {message['error']}")
                raise RuntimeError(f"app-server {method} returned an invalid response: {message}")
            self.pending_messages.append(message)
        raise TimeoutError(f"timed out waiting for app-server response: {method}")

    def wait_for_turn_completion(self, *, turn_id: str, timeout_seconds: float = 900.0) -> dict[str, Any]:
        deadline = _now() + timeout_seconds
        agent_messages: list[dict[str, Any]] = []
        agent_message_deltas: dict[str, str] = {}
        last_error: str | None = None
        while _now() < deadline:
            try:
                message = self._read_message(timeout_seconds=min(1.0, deadline - _now()))
            except TimeoutError:
                continue
            method = message.get("method")
            if not isinstance(method, str) or not method.strip():
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    agent_messages.append(dict(item))
            elif method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "").strip()
                if item_id:
                    agent_message_deltas[item_id] = agent_message_deltas.get(item_id, "") + str(params.get("delta") or "")
            elif method == "error":
                error = params.get("error")
                if isinstance(error, dict):
                    message_text = str(error.get("message") or "").strip()
                    if message_text:
                        last_error = message_text
            elif method == "turn/completed":
                turn = _require_object(params.get("turn"), "turn/completed turn")
                status = _require_str(turn.get("status"), "turn/completed turn.status")
                turn_error = turn.get("error")
                if isinstance(turn_error, dict):
                    message_text = str(turn_error.get("message") or "").strip()
                    if message_text:
                        last_error = message_text
                if not agent_messages and agent_message_deltas:
                    agent_messages.extend(
                        {
                            "type": "agentMessage",
                            "id": item_id,
                            "text": text,
                            "phase": None,
                        }
                        for item_id, text in agent_message_deltas.items()
                        if text
                    )
                return {
                    "status": status,
                    "error": last_error,
                    "agent_messages": agent_messages,
                    "stderr_tail": list(self.stderr_tail),
                }
        raise TimeoutError(f"timed out waiting for app-server turn completion: {turn_id}")

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stdout_thread.join(timeout=1.0)
        self.stderr_thread.join(timeout=1.0)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def _read_message(self, *, timeout_seconds: float) -> dict[str, Any]:
        if self.pending_messages:
            return self.pending_messages.popleft()
        return self._read_process_message(timeout_seconds=timeout_seconds)

    def _read_process_message(self, *, timeout_seconds: float) -> dict[str, Any]:
        deadline = _now() + max(timeout_seconds, 0.01)
        while _now() < deadline:
            try:
                line = self.stdout_queue.get(timeout=max(min(deadline - _now(), 1.0), 0.01))
            except queue.Empty as exc:
                raise TimeoutError("timed out waiting for app-server stdout") from exc
            if line is None:
                raise RuntimeError("app-server exited before sending a response")
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise RuntimeError(f"app-server emitted a non-object message: {payload!r}")
            return payload
        raise TimeoutError("timed out waiting for app-server stdout")


def _build_app_server_command() -> tuple[str, ...]:
    prefix = _resolve_app_server_command_prefix()
    return (*prefix, "app-server", "--listen", "stdio://")


def _resolve_app_server_command_prefix() -> tuple[str, ...]:
    if os.name == "nt":
        codex_cmd = shutil.which("codex.cmd")
        if codex_cmd:
            return ("cmd.exe", "/c", codex_cmd)
    codex_bin = shutil.which("codex")
    if codex_bin:
        return (codex_bin,)
    raise FileNotFoundError("unable to find codex or codex.cmd on PATH for app-server backend")


def _copy_codex_auth_files(target_codex_home: Path) -> None:
    global_codex_home = Path.home() / ".codex"
    for name in ("auth.json", "config.toml", "cap_sid", "version.json"):
        source_path = global_codex_home / name
        if source_path.exists():
            shutil.copy2(source_path, target_codex_home / name)


def _run_mailbox_login(
    *,
    tooling_dir: Path,
    mailbox_client_path: Path,
    role_config: dict[str, Any],
    env: dict[str, str],
) -> str:
    login_args = [
        sys.executable,
        str(mailbox_client_path),
        "login",
        "--output",
        "token",
        "--project-id",
        _require_str(role_config.get("project_id"), "runtime config project_id"),
    ]
    roles = role_config.get("roles")
    role = role_config.get("role")
    if isinstance(roles, list):
        joined_roles = ",".join(str(item).strip() for item in roles if str(item).strip())
        if joined_roles:
            login_args.extend(("--roles", joined_roles))
    elif isinstance(roles, str) and roles.strip():
        login_args.extend(("--roles", roles.strip()))
    elif isinstance(role, str) and role.strip():
        login_args.extend(("--role", role.strip()))
    for key, flag in (
        ("session", "--session"),
        ("agent_name", "--agent-name"),
        ("local_part", "--local-part"),
        ("mailbox_type", "--mailbox-type"),
    ):
        value = role_config.get(key)
        if isinstance(value, str) and value.strip():
            login_args.extend((flag, value.strip()))
    completed = subprocess.run(
        login_args,
        cwd=str(tooling_dir),
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"mailbox login failed for app-server backend: {detail}")
    session_token = completed.stdout.strip()
    if not session_token:
        raise RuntimeError("mailbox login returned an empty session token for app-server backend")
    return session_token


def _build_prompt_text(
    *,
    prompt_path: Path,
    workspace_dir: Path,
    mailbox_client_path: Path,
    delivery: dict[str, Any],
    sandbox_delivery_path: Path,
    recovery_context: dict[str, Any] | None = None,
) -> str:
    base_prompt = prompt_path.read_text(encoding="utf-8")
    delivery_id = str(delivery.get("delivery_id") or "").strip()
    message_id = str(delivery.get("message_id") or "").strip()
    thread_id = str(delivery.get("thread_id") or "").strip()
    from_address = str(delivery.get("from") or "").strip()
    to_address = str(delivery.get("to") or "").strip()
    workspace_context = (
        "Workspace root:\n"
        f"- use {workspace_dir} as the repo root for all reads, edits, and validation commands\n"
        "- do not switch to a different checkout unless the mailbox task explicitly requires it\n"
        f"- use the mailbox CLI at {mailbox_client_path} when you need to inspect or reply on the claimed thread\n"
    )
    oncall_context = (
        "\n\nClaimed delivery context:\n"
        f"- delivery_id: {delivery_id}\n"
        f"- message_id: {message_id}\n"
        f"- thread_id: {thread_id}\n"
        f"- from_address: {from_address}\n"
        f"- to_address: {to_address}\n"
        f"- delivery_file: {sandbox_delivery_path}\n"
        "\nOncall rules:\n"
        f"1. The supervisor already claimed this delivery. Do not run python \"{mailbox_client_path}\" claim.\n"
        f"2. Read the thread with python \"{mailbox_client_path}\" --format text thread --message-id {message_id}.\n"
        "3. Keep the task bounded to one mailbox-native operator update in this repo.\n"
        "4. Run focused validation for the exact surface you changed.\n"
        "5. Reply exactly once with:\n"
        f"   python \"{mailbox_client_path}\" reply --delivery-file \"{sandbox_delivery_path}\" --idempotency-key \"oncall-{delivery_id}-reply\" --payload-json '{{...}}'\n"
        "6. Do not run ack, nack, or reply --ack-after. The supervisor will ack on exit code 0 and nack on non-zero.\n"
        "7. If the task is too broad but you can explain why safely, send a deferred reply and still exit 0.\n"
        "8. Exit non-zero only for transient failure where retry is desired.\n"
        "9. Use the explicit claimed delivery context in this prompt as the source of truth for message_id and delivery_file.\n"
    )
    recovered_context_text = _build_recovery_context_text(recovery_context or {})
    return workspace_context + "\n" + base_prompt + oncall_context + recovered_context_text


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _select_final_agent_message(agent_messages: list[dict[str, Any]]) -> str | None:
    final_answer: str | None = None
    fallback: str | None = None
    for item in agent_messages:
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        fallback = text.strip()
        if item.get("phase") == "final_answer":
            final_answer = text.strip()
    return final_answer or fallback


def _capture_stderr_tail(session: "_AppServerSession", *, settle_seconds: float = 0.02) -> list[str]:
    stderr_tail = list(session.stderr_tail)
    if stderr_tail or settle_seconds <= 0:
        return stderr_tail
    time.sleep(settle_seconds)
    return list(session.stderr_tail)


def _build_recovery_context(
    *,
    existing_thread_state: dict[str, Any] | None,
    thread_assignment: dict[str, Any],
) -> dict[str, Any]:
    handoff_summary = _state_text(existing_thread_state, "handoff_summary")
    reused_worker = bool(thread_assignment.get("reused_worker") is True)
    if reused_worker or handoff_summary is None:
        return {
            "used": False,
            "handoff_summary": None,
            "last_processed_message_id": None,
            "previous_worker_id": None,
            "recovery_reason": None,
        }
    return {
        "used": True,
        "handoff_summary": handoff_summary,
        "last_processed_message_id": _state_text(existing_thread_state, "last_processed_message_id"),
        "previous_worker_id": _state_text(existing_thread_state, "worker_id")
        or _assignment_text(thread_assignment, "previous_worker_id"),
        "recovery_reason": _assignment_text(thread_assignment, "recovery_reason"),
    }


def _build_recovery_context_text(recovery_context: dict[str, Any]) -> str:
    if not recovery_context.get("used"):
        return ""
    lines = [
        "\nRecovered thread context from the previous oncall run:\n",
    ]
    recovery_reason = recovery_context.get("recovery_reason")
    if isinstance(recovery_reason, str) and recovery_reason.strip():
        lines.append(f"- recovery_reason: {recovery_reason.strip()}\n")
    previous_worker_id = recovery_context.get("previous_worker_id")
    if isinstance(previous_worker_id, str) and previous_worker_id.strip():
        lines.append(f"- previous_worker_id: {previous_worker_id.strip()}\n")
    last_processed_message_id = recovery_context.get("last_processed_message_id")
    if isinstance(last_processed_message_id, str) and last_processed_message_id.strip():
        lines.append(f"- last_processed_message_id: {last_processed_message_id.strip()}\n")
    lines.append("- bounded_handoff_summary:\n")
    handoff_summary = str(recovery_context.get("handoff_summary") or "").strip()
    for line in handoff_summary.splitlines():
        stripped = line.rstrip()
        if stripped:
            lines.append(f"  {stripped}\n")
    lines.append(
        "Use this recovered summary as continuity help for the new worker, but still verify details against the live mailbox thread before replying.\n"
    )
    return "".join(lines)


def _resolve_workspace_context(
    *,
    delivery: dict[str, Any],
    existing_thread_state: dict[str, Any] | None,
    default_workspace_dir: Path,
    workspace_root_dir: Path,
    prompt_path: Path,
    mailbox_client_path: Path,
    codex_home_dir: Path,
) -> _ResolvedWorkspaceContext:
    workspace_hint = _workspace_hint(delivery)
    existing_workspace_dir = _state_text(existing_thread_state, "workspace_dir")
    requested_workspace_dir = workspace_hint[0] if workspace_hint is not None else None
    workspace_source = workspace_hint[1] if workspace_hint is not None else None
    resolution_error: str | None = None
    if requested_workspace_dir is not None:
        try:
            workspace_dir = _resolve_workspace_dir(
                raw_workspace_dir=requested_workspace_dir,
                workspace_root_dir=workspace_root_dir,
            )
        except ValueError as exc:
            workspace_dir = default_workspace_dir
            resolution_error = str(exc)
    elif existing_workspace_dir is not None:
        try:
            workspace_dir = _resolve_workspace_dir(
                raw_workspace_dir=existing_workspace_dir,
                workspace_root_dir=workspace_root_dir,
            )
            workspace_source = "thread_registry.workspace_dir"
        except ValueError:
            workspace_dir = default_workspace_dir
            workspace_source = "default_workspace_dir"
    else:
        workspace_dir = default_workspace_dir
        workspace_source = "default_workspace_dir"
    return _ResolvedWorkspaceContext(
        workspace_dir=workspace_dir,
        workspace_root_dir=workspace_root_dir,
        prompt_path=prompt_path,
        mailbox_client_path=mailbox_client_path,
        codex_home_dir=codex_home_dir,
        workspace_source=workspace_source,
        requested_workspace_dir=requested_workspace_dir,
        resolution_error=resolution_error,
        worker_binding_key=None,
    )


def _workspace_context_from_assignment(
    *,
    thread_assignment: dict[str, Any],
    default_workspace_dir: Path,
    workspace_root_dir: Path,
    prompt_path: Path,
    mailbox_client_path: Path,
    codex_home_dir: Path,
) -> _ResolvedWorkspaceContext:
    workspace_dir = Path(_assignment_text(thread_assignment, "workspace_dir") or str(default_workspace_dir)).resolve()
    workspace_root = Path(
        _assignment_text(thread_assignment, "workspace_root_dir") or str(workspace_root_dir)
    ).resolve()
    return _ResolvedWorkspaceContext(
        workspace_dir=workspace_dir,
        workspace_root_dir=workspace_root,
        prompt_path=prompt_path,
        mailbox_client_path=mailbox_client_path,
        codex_home_dir=codex_home_dir,
        workspace_source=_assignment_text(thread_assignment, "workspace_source"),
        requested_workspace_dir=_assignment_text(thread_assignment, "requested_workspace_dir"),
        resolution_error=_assignment_text(thread_assignment, "workspace_resolution_error"),
        worker_binding_key=_assignment_text(thread_assignment, "worker_binding_key"),
    )


def _workspace_context_metadata(resolved_workspace: _ResolvedWorkspaceContext) -> dict[str, Any]:
    metadata = {
        "handler_cwd": str(resolved_workspace.workspace_dir),
        "workspace_dir": str(resolved_workspace.workspace_dir),
        "workspace_root_dir": str(resolved_workspace.workspace_root_dir),
        "codex_home_dir": str(resolved_workspace.codex_home_dir),
        "prompt_path": str(resolved_workspace.prompt_path),
        "mailbox_client_path": str(resolved_workspace.mailbox_client_path),
    }
    if resolved_workspace.workspace_source is not None:
        metadata["workspace_source"] = resolved_workspace.workspace_source
    if resolved_workspace.requested_workspace_dir is not None:
        metadata["requested_workspace_dir"] = resolved_workspace.requested_workspace_dir
    if resolved_workspace.resolution_error is not None:
        metadata["workspace_resolution_error"] = resolved_workspace.resolution_error
    if resolved_workspace.worker_binding_key is not None:
        metadata["worker_binding_key"] = resolved_workspace.worker_binding_key
    return metadata


def _workspace_hint(payload: dict[str, Any]) -> tuple[str, str] | None:
    hint = _find_workspace_hint(payload, path="delivery")
    if hint is not None:
        return hint
    delivery_payload = payload.get("payload")
    if isinstance(delivery_payload, dict):
        hint = _find_workspace_hint(delivery_payload, path="delivery.payload")
        if hint is not None:
            return hint
    return None


def _find_workspace_hint(value: Any, *, path: str, depth: int = 0) -> tuple[str, str] | None:
    if depth > 5 or not isinstance(value, dict):
        return None
    direct_keys = ("workspace_dir", "workspace_root", "workspace_root_dir", "repo_root")
    for key in direct_keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip(), f"{path}.{key}"
    workspace_value = value.get("workspace")
    if isinstance(workspace_value, dict):
        hint = _find_workspace_hint(workspace_value, path=f"{path}.workspace", depth=depth + 1)
        if hint is not None:
            return hint
    for key in ("payload", "source", "headers", "context", "meta", "metadata"):
        nested = value.get(key)
        if isinstance(nested, dict):
            hint = _find_workspace_hint(nested, path=f"{path}.{key}", depth=depth + 1)
            if hint is not None:
                return hint
    return None


def _resolve_workspace_dir(*, raw_workspace_dir: str, workspace_root_dir: Path) -> Path:
    candidate = Path(raw_workspace_dir)
    if not candidate.is_absolute():
        candidate = (workspace_root_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not _is_within_workspace_root(candidate, workspace_root_dir):
        raise ValueError(
            f"requested workspace {candidate} is outside the allowed workspace root {workspace_root_dir}"
        )
    if not candidate.exists():
        raise ValueError(f"requested workspace {candidate} does not exist")
    if not candidate.is_dir():
        raise ValueError(f"requested workspace {candidate} is not a directory")
    return candidate


def _is_within_workspace_root(candidate: Path, workspace_root_dir: Path) -> bool:
    candidate_text = str(candidate.resolve()).replace("\\", "/").lower().rstrip("/")
    workspace_root_text = str(workspace_root_dir.resolve()).replace("\\", "/").lower().rstrip("/")
    return candidate_text == workspace_root_text or candidate_text.startswith(workspace_root_text + "/")


def _thread_assignment_worker_id(thread_assignment: dict[str, Any]) -> str | None:
    value = thread_assignment.get("worker_id")
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _assignment_text(thread_assignment: dict[str, Any], key: str) -> str | None:
    value = thread_assignment.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _state_text(state: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _pump_stream_to_queue(stream: Any, target_queue: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            target_queue.put(line.rstrip("\r\n"))
    finally:
        target_queue.put(None)


def _pump_stream_to_tail(stream: Any, target_tail: deque[str]) -> None:
    for line in stream:
        target_tail.append(line.rstrip("\r\n"))


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} must be a non-empty string")
    return value.strip()


def _now() -> float:
    return time.time()
