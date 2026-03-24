# Collaboration Notes

## File Roles

- `AGENTS.md`: keep paths, workflow, and repo-specific commands short and current
- `.where-agent-progress.md`: keep the current test-readiness and integration status
- `docs/`: keep future benchmark notes, integration plans, and design decisions
- `README.md`: keep end-user setup and usage examples
- `sqlite_mailbox_http.py`: primary local HTTP server entry point
- `client.py`: terminal-oriented CLI for login/send/claim/thread/reply flows

## Key Paths

- Workspace: `E:\agent_misc\mail4agent`
- Docs: `E:\agent_misc\mail4agent\docs`
- Progress: `E:\agent_misc\mail4agent\.where-agent-progress.md`
- Server: `E:\agent_misc\mail4agent\sqlite_mailbox_http.py`
- CLI: `E:\agent_misc\mail4agent\client.py`
- Storage Layer: `E:\agent_misc\mail4agent\sqlite_mailbox.py`

## Workflow

1. Read `.where-agent-progress.md` first.
2. Keep detailed mainline plans in `docs/`.
3. Prefer the standard-library server, CLI, and adapter modules that already exist before adding one-off helpers.
4. Keep tests and experiments dependency-free unless there is a strong reason to add packages.
5. Keep `AGENTS.md` short and update the progress file when the mainline changes.

## Common Commands

- `python E:\agent_misc\mail4agent\sqlite_mailbox_http.py --db E:\agent_misc\mail4agent\mailbox.sqlite --host 127.0.0.1 --port 8787`
- `python E:\agent_misc\mail4agent\client.py claim`
- `python E:\agent_misc\mail4agent\client.py --format text thread --message-id <MESSAGE_ID>`
- `python E:\agent_misc\mail4agent\codex_mailbox_demo_agent.py`
- `python E:\agent_misc\mail4agent\codex_mailbox_demo_send.py --task-type upper_text --text "hello codex" --wait-for-reply`
- `powershell -ExecutionPolicy Bypass -File E:\agent_misc\mail4agent\launch_dogfood_agent.ps1 planner`
- `powershell -ExecutionPolicy Bypass -File E:\agent_misc\mail4agent\launch_dogfood_agent.ps1 operator`

## Notes

- The repo is intended to stay lightweight and Python-standard-library-only.
- `mailbox.sqlite` and other runtime SQLite files should stay out of version control.
- Favor repo-local documentation in `README.md` plus `docs/` over adding workflow notes to code comments.
