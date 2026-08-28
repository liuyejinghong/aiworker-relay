# AIworker Relay

> OpenRouter coding workers, directed by Codex.

AIworker Relay is a local Codex Plugin for using selected OpenRouter models as bounded coding workers. It is for a developer who wants to use a fast or economical model where it fits, while keeping Codex responsible for the task boundary, judgment, and final review.

It is not another agent platform, model router, or custom agent loop.

## The idea

Codex decides whether a task should be delegated, which worker is appropriate, and whether the result is acceptable. AIworker Relay supplies a controlled local execution lane for an explicitly selected external model.

```text
Developer
  → Codex
  → bounded task packet
  → AIworker Relay (local control plane)
  → isolated Codex CLI run
  → OpenRouter
  → selected coding model
  → diff, tests, and evidence
  → Codex review
```

The developer's explicit Profile choice always wins. In v0.1, each run uses that Profile's configured reasoning policy; run-level reasoning overrides are rejected. A Codex recommendation is never dispatch authority by itself.

## What is implemented today

- A Codex Plugin and `$aiworker-relay` Skill for setup and explicitly authorized dispatch.
- A fixed, persistent local Web control plane for OpenRouter connection settings, worker profiles, and active external runs.
- OS-keyring storage for the OpenRouter API key; it is not entered through the Codex chat or written to the repository.
- Model-name or model-link discovery against the OpenRouter catalog, with a profile default for supported reasoning effort and a separate enable/freeze state.
- A worker dashboard showing current model metadata, prices, context window, and exact-model public benchmark records when the user requests a refresh.
- A fixed isolated execution path: detached Git worktree, separate `CODEX_HOME`, `codex exec`, JSONL evidence, diff, and a TERM-then-KILL stop flow.
- Honest local run observation: process state and RSS come from the local supervisor; native Codex workers are shown as Codex-managed rather than synthetic local telemetry.

## Product boundaries

- **Codex owns judgment. Workers provide labor and evidence.**
- OpenRouter is the only external-model gateway in scope. There are no direct Gemini, Claude, Muse, or other provider adapters.
- Dispatch is consent-gated. AIworker Relay does not silently intercept work, substitute a model, lower reasoning effort, or auto-fallback after a failure.
- A frozen worker refuses new dispatches and tells the user why; it does not silently select another model.
- The dashboard is a local control surface, not a hosted service or a replacement for Codex.
- The v0.1.x preview supports macOS and one project-bound daemon. Windows, Linux, multi-project control, actual-cost budgets, and independently verifiable worker test evidence are not release claims.

## Current release status

**Pre-release.** v0.1.17 is the current source candidate. It adds the explicit run-scoped permission profile and local run-data controls reviewed on current `main`; its installed real-Provider acceptance is still pending. The earlier v0.1.16 NVIDIA run remains historical evidence for a dashboard-managed detached write, file/diff readback, and TERM stop, not proof for the new runner boundary. Current macOS Codex runtimes still expose host temp directories to ordinary sandboxed shell processes, so this candidate does not claim complete host isolation. No tag or GitHub Release has been created.

Source repository: [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay). The documented Git marketplace CLI flow has been observed end to end; that does not by itself establish every Codex Desktop update interaction or a public release.

The repository deliberately does not claim features that are not implemented: automatic routing, provider fallbacks, run-level actual-cost attribution, budgets, native-worker process telemetry, direct provider integrations, a database, or a custom workflow engine.

## Security and data boundary

OpenRouter API keys are stored only through the operating system's keyring service. An approved external run sends its bounded task context to the OpenRouter model selected by the user; model-specific privacy and retention terms remain the developer's responsibility to review. Runtime data lives locally under `.orch/` or the application-data directory and is excluded from version control. Project run data is retained until the user explicitly deletes eligible terminal runs; uninstall and runtime updates preserve `.orch/`. Text evidence replaces the exact configured OpenRouter Key, but raw worktrees and isolated `CODEX_HOME` are not claimed to be sanitized.

## Documentation

The detailed product and technical documents are currently maintained in Chinese:

- [Product definition](docs/product.md)
- [Architecture](docs/architecture.md)
- [New-user flow](docs/user-flow.md)
- [Requirements and open decisions](docs/requirements.md)
- [Update lifecycle proposal](docs/update-lifecycle.md)
- [Verification record](docs/verification.md)
- [Architecture decisions](docs/decisions.md)

## Development

AIworker Relay requires Python 3.12+ for its local runtime. From a source checkout with its development environment installed:

```bash
PYTHONPATH=plugins/aiworker-relay/src .venv/bin/python -m unittest discover -s tests -v
node --check plugins/aiworker-relay/src/orchestrator/web/app.js
```

## License

[MIT](LICENSE)

AIworker Relay is an independent project and is not affiliated with or endorsed by OpenAI or OpenRouter.
