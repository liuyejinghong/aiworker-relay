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

The developer's explicit model and reasoning choice always wins. A Codex recommendation is never dispatch authority by itself.

## What is implemented today

- A Codex Plugin and `$aiworker-relay` Skill for setup and explicitly authorized dispatch.
- A local, on-demand Web control plane for OpenRouter connection settings, worker profiles, and active external runs.
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

## Current release status

**Pre-release.** The Git-backed Marketplace CLI path has completed a clean current-v0.1.9 install and prior bundle updates, and explicit setup has converged the bundle, local runtime, and daemon versions. v0.1.9 also fixes the non-interactive Codex approval path: the exact NVIDIA model completed a real tool-capable CLI write in an isolated worktree. The first dashboard-managed v0.1.9 write then received the free model's OpenRouter `429 Too Many Requests`, which was recorded as a failure rather than hidden or retried. A successful post-fix managed write is therefore still required before a release claim; no tag or public release has been created.

Source repository: [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay). The documented Git marketplace CLI flow has been observed end to end; that does not by itself establish every Codex Desktop update interaction or a public release.

The repository deliberately does not claim features that are not implemented: automatic routing, provider fallbacks, run-level actual-cost attribution, budgets, native-worker process telemetry, direct provider integrations, a database, or a custom workflow engine.

## Security and data boundary

OpenRouter API keys are stored only through the operating system's keyring service. An approved external run sends its bounded task context to the OpenRouter model selected by the user; model-specific privacy and retention terms remain the developer's responsibility to review. Runtime data lives locally under `.orch/` or the application-data directory and is excluded from version control.

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
