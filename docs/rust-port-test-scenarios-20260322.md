# Rust Port Test Scenarios

## Goal

Use `mail4agent` as a future benchmark repo for Python-to-Rust migration tasks that exercise:

- local HTTP protocol fidelity
- SQLite-backed state transitions
- agent worker loops with ack, nack, and heartbeat behavior
- operator-facing setup and recovery flows on Windows

## Working Assumptions

- Treat `E:\agent_misc\mail4agent` as the canonical source repo and keep future benchmark runs on seeded copies, not in-place edits.
- Use the current Python implementation as the protocol oracle during early migration rounds.
- Prefer Windows-first validation because that is the environment already exercised here.
- Keep early migration tasks dependency-light even on the Rust side when practical.

## Shared Fixture

Every scenario becomes easier to compare if it reuses the same seeded mailbox fixture:

- Start the Python server from `sqlite_mailbox_http.py` with a temporary SQLite database and a fixed `MAILBOX_ADMIN_TOKEN`.
- Seed one harness, one project, and at least three mailboxes:
  - `planner@mail4agent.codex`
  - `reviewer@mail4agent.codex`
  - `operator@mail4agent.codex`
- Allow same-project delivery and create one harness token for agent login.
- Export a stable env bundle with:
  - `MAILBOX_BASE_URL`
  - `MAILBOX_TOKEN`
  - `MAILBOX_FROM_ADDRESS`
  - `MAILBOX_INBOX_ADDRESS`

That shared fixture lets us vary only the migration target while keeping the mailbox topology fixed.

## Scenario Matrix

### `small_rust_readonly_cli_surface`

- Shape: small
- Primary target: port the read-oriented client surface to Rust
- Suggested scope:
  - `healthz`
  - `whoami`
  - `resolve`
  - `message`
  - `thread`
- Why it is useful:
  - validates Rust HTTP and JSON plumbing without immediately pulling in worker concurrency
  - gives us a first protocol-parity task with low setup cost
- Acceptance:
  - a Rust CLI can talk to the Python server using the same env model as `client.py`
  - `resolve`, `message`, and `thread` return the expected fields on seeded data
  - output is machine-readable JSON and stable enough for scripted diffing
- Best at measuring:
  - auth/header correctness
  - JSON normalization discipline
  - CLI ergonomics and argument handling

### `small_rust_demo_send_roundtrip`

- Shape: small
- Primary target: port `codex_mailbox_demo_send.py` to Rust
- Suggested scope:
  - `echo`
  - `upper_text`
  - `sum_numbers`
  - `sleep_echo`
  - `retry_demo`
  - `--wait-for-reply`
- Why it is useful:
  - exercises send, claim, ack, nack, and thread matching without requiring a full Rust server
  - gives a clean “Rust client talks to Python oracle” scenario
- Acceptance:
  - the Rust sender can send tasks to the Python demo agent and receive matching replies
  - reply matching works by `in_reply_to_message_id` or `thread_id`
  - unmatched replies are nacked and do not break the wait loop
- Best at measuring:
  - end-to-end roundtrip correctness
  - delivery matching logic
  - timeout and retry handling

### `medium_rust_worker_once_and_heartbeat`

- Shape: medium
- Primary target: port `codex_mailbox_worker.py`
- Suggested scope:
  - `claim_once`
  - `process_once`
  - heartbeat loop
  - ack and nack application
  - `print` and `echo-reply` demo modes
- Why it is useful:
  - this is the first migration step that really tests concurrency and lease semantics
  - it is compact enough to keep the task bounded, but rich enough to expose real bugs
- Acceptance:
  - the Rust worker can claim one delivery from the Python server and ack it
  - a long-running handler extends the lease with heartbeat before expiry
  - handler exceptions produce a nack with a truncated error string
  - `--once` and continuous loop behavior both work
- Best at measuring:
  - background thread/task correctness
  - lease and heartbeat fidelity
  - failure-path discipline

### `medium_rust_adapter_demo_agent`

- Shape: medium
- Primary target: port `codex_mailbox_adapter.py` and `codex_mailbox_demo_agent.py`
- Suggested scope:
  - context wrapper
  - `ReplyAction`
  - `NackAction`
  - retryable errors
  - demo task dispatch
- Why it is useful:
  - it turns the mailbox from a transport exercise into a true agent-facing programming model
  - it directly mirrors how we would want Codex-style agents to consume the service later
- Acceptance:
  - the Rust demo agent processes the same five demo task types as the Python version
  - `retry_demo` maps to nack semantics instead of an incorrect reply
  - successful tasks send replies with the expected shape and message type
  - the agent can run once or continuously against the Python server
- Best at measuring:
  - API design migration quality
  - handler ergonomics
  - semantic parity under mixed success and retry outcomes

### `large_rust_server_vertical_slice`

- Shape: large
- Primary target: port the core mailbox server slice to Rust
- Suggested scope:
  - SQLite-backed storage for messages and deliveries
  - `POST /login`
  - `POST /resolve`
  - `POST /send`
  - `POST /claim`
  - `POST /ack`
  - `GET /thread`
- Why it is useful:
  - this is the cleanest “real server migration” benchmark without dragging in every admin/operator feature
  - it maps well to the prior large-task benchmark framing of a vertical slice
- Acceptance:
  - the existing Python client can talk to the Rust server for the supported endpoints
  - send -> claim -> ack -> thread works end to end on a fresh SQLite database
  - login issues usable session tokens and enforces claim/send permissions correctly
  - seeded thread history is returned with the expected ordering and structure
- Best at measuring:
  - protocol fidelity across language boundaries
  - schema and state-transition correctness
  - agent ability to choose a bounded but coherent migration cut

### `large_rust_protocol_replay_parity_workbench`

- Shape: large
- Primary target: build the parity harness around the migration
- Suggested scope:
  - capture normalized request and response transcripts from the Python server
  - replay the same flows against a Rust implementation
  - diff normalized JSON and produce a readable parity report
- Why it is useful:
  - parity tooling is often what unlocks confident larger migrations
  - it gives us a benchmark task that rewards judgment, tooling, and evidence quality
- Acceptance:
  - one command can run the Python oracle flow, the Rust candidate flow, and produce a parity report
  - the report clearly marks supported, mismatched, and missing fields
  - normalization removes obvious noise such as timestamps or generated ids when appropriate
- Best at measuring:
  - migration strategy quality
  - validation-tooling taste
  - evidence quality for agent outputs

### `large_rust_operator_polish`

- Shape: large
- Primary target: operator and setup surface for the Rust migration
- Suggested scope:
  - admin bootstrap flow
  - admin token or basic-auth handling
  - health checks
  - static admin UI serving
  - startup and shutdown behavior
  - Windows-friendly launch and smoke commands
- Why it is useful:
  - the protocol can be correct while the operator experience is still rough
  - this scenario measures the “last 20 percent” that often separates a demo from a usable service
- Acceptance:
  - a fresh operator can start the Rust service, bootstrap admin access, and open the admin UI locally
  - operator-facing errors are actionable
  - smoke checks cover startup, login, one send/claim/ack cycle, and clean shutdown
  - docs or scripts are good enough that another agent can reuse them in later tests
- Best at measuring:
  - developer-experience polish
  - Windows operational fit
  - completeness of finish work after the main port

## Recommended Benchmark Order

If we want a staged benchmark rollout instead of one giant jump, this order is the best fit:

1. `small_rust_demo_send_roundtrip`
2. `medium_rust_worker_once_and_heartbeat`
3. `medium_rust_adapter_demo_agent`
4. `large_rust_server_vertical_slice`
5. `large_rust_protocol_replay_parity_workbench`
6. `large_rust_operator_polish`

That sequence starts with “Rust client against Python oracle”, then moves to “Rust worker against Python oracle”, and only then asks the agent to migrate the server itself.

## Packaging Guidance

For future benchmark tasks, I would package this repo in two different ways:

- Supporting service mode:
  - keep the Python server as fixed infrastructure
  - benchmark only the Rust client, worker, or demo-agent migration
- Primary workload mode:
  - ask the agent to create or extend a Rust implementation of the server
  - validate behavior against the Python implementation or a parity harness

This split should help us separate “can the agent speak the protocol” from “can the agent migrate the system”.

## First Recommended Batch

If we only want the first three benchmark tasks from this repo, use these:

- `small_rust_demo_send_roundtrip`
- `medium_rust_adapter_demo_agent`
- `large_rust_server_vertical_slice`

Those three together would already tell us a lot about:

- protocol correctness
- worker and reply-loop semantics
- how well agents handle a bounded but meaningful server migration
