---
name: aiworker-relay
description: Configure or dispatch an OpenRouter-backed coding worker through AIworker Relay while Codex retains task judgment and final acceptance. Use for local setup, external worker selection, or an explicitly authorized external run; do not use it merely to route native Codex workers.
---

# AIworker Relay

Use this capability only for external OpenRouter-backed coding work. Codex owns the task, scope, worker recommendation, user-consent check, and final acceptance. An external worker supplies labor and evidence.

## Setup and configuration

- Resolve this Skill's Plugin root by walking up to the directory containing `.codex-plugin/plugin.json`. Its launcher is `scripts/launch_external_workers.py`.
- For first use or when the user asks to configure workers, run `python3 <plugin-root>/scripts/launch_external_workers.py status`.
- If the runtime is ready, run `python3 <plugin-root>/scripts/launch_external_workers.py setup`. It starts or reuses the fixed loopback control plane and opens the local Web page. On macOS, setup also installs the user-level entry for that same daemon; afterward the user can directly open `http://127.0.0.1:49178` without asking Codex to reopen it.
- An explicit `$aiworker-relay setup` request authorizes this one-time local bootstrap. If the runtime is absent, briefly state that setup will create the dedicated application-data venv and install this Plugin source plus its four direct runtime dependencies, then run `python3 <plugin-root>/scripts/launch_external_workers.py setup` in the same turn. Do not ask for a second confirmation.
- For a generic configuration request that does not explicitly ask to run setup, do not bootstrap. Explain the one-time setup action and wait for the user to request it.
- OpenRouter API keys and worker Profiles are configured only in that page. Never request, print, add, or pass an API key through the Codex conversation, a Task Packet, or a regular command argument.
- If the launcher cannot start the runtime, report that concrete failure. Do not replace it with a direct provider call or a second CLI path.

## External dispatch

Before dispatching, keep the work bounded and write a Task Packet Markdown file with these headings:

```text
# Task
# Scope
# Do Not Touch
# Existing Behavior
# Expected Behavior
# Constraints
# Acceptance Criteria
# Verification
# Deliverables
```

- The user must explicitly select the Profile, or explicitly accept Codex's recommendation, before any external run that may send context or incur use.
- Use the selected Profile's configured reasoning policy. v0.1 has no per-run reasoning override; a fixed Profile passes its default unchanged, while only an automatic Profile leaves the effort for Codex to choose.
- Invoke `python3 <plugin-root>/scripts/launch_external_workers.py dispatch --profile <profile-id> --packet <packet-path>` only after those conditions are true.
- For an unverified Profile, require the user's explicit experimental-run confirmation and add `--confirm-experimental`.
- A frozen Profile is a refusal, not a reason to silently choose another model. Report the frozen state and wait for the user to activate a Profile or choose another one.
- Do not use this dispatch path for Luna Medium or Luna Max. Their lifecycle remains under Codex native worker control.

## During and after a run

- The external run writes only in its detached Git worktree. Main-worktree changes are excluded; do not imply that they were sent to the worker.
- Observe status in the local Web control plane. A request to stop sends TERM first; only after the process remains alive may the user confirm forced termination.
- Treat the Task Packet, `last-message.md`, diff, test evidence, process outcome, and run record as the evidence set. Do not accept a result solely because a worker says it succeeded.
- A 429, start failure, or model interruption is a recorded failed run. Offer a user-directed retry; do not auto-fallback or silently reroute.
- When actual provider cost is not reliably attributable to the run, present it as pending or unavailable. Never present model list price, `US$0`, or an estimate as actual spend.
