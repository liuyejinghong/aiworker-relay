# Agent Instructions

## Scope and design

- Implement only the requested scope. Report important out-of-scope issues, but do not solve them without authorization.
- Prefer the smallest complete change. Do not add speculative features, compatibility layers, flags, generic frameworks, or placeholder behavior.
- Do not introduce a new abstraction without a real current requirement. Do not create generic utils, helpers, common, manager, service, engine, handler, processor, repository, factory, or adapter modules.
- Keep dependencies one-way and visible. Do not introduce global singletons, service locators, or cross-layer callbacks.
- Prefer the Python standard library. Explain any new third-party runtime dependency in the relevant decision or implementation discussion.

## Documentation and architecture

- Before changing architecture, read docs/architecture.md, docs/requirements.md, and docs/decisions.md.
- Treat docs/requirements.md as the source of pending design questions. Do not silently decide an item that remains open there.
- If documentation and implementation conflict, identify the discrepancy and ask for direction or update both under explicit scope; never guess which is authoritative.
- Keep architecture and decision records concise. Record only decisions that are actually accepted.

## Runtime and verification

- Never commit runtime state, logs, task artifacts, or worktrees. Runtime data belongs under .orch/ and is ignored.
- Do not add empty CLI commands for unimplemented behavior.
- Test only behavior that exists or changes. Before adding a check, identify the concrete uncertainty it resolves and the decision it could change.
- Do not commit, push, merge, tag, release, deploy, or make other external changes unless explicitly authorized.
