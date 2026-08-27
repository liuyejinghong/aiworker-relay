# Architecture Decisions

Only accepted decisions belong here. Open questions remain in docs/requirements.md.

## D001 — Python 3.12+ as implementation language

Status: Accepted

Reason:

The agreed initial stack is Python 3.12+ with a CLI-first operating model. It directly supports the expected standard-library use of subprocess, asyncio, JSON, filesystem access, and Git CLI invocation.

Alternatives considered:

No alternative implementation language has been accepted for this project.

Consequences:

The repository uses a src-layout Python package. v0.1 keeps its small runtime dependency set documented in D015 and D024.

## D002 — Codex owns orchestration judgment

Status: Accepted

Reason:

Codex is the designated primary controller and final authority. External coding agents supply execution and evidence rather than independent final decisions.

Alternatives considered:

Delegating planning, task acceptance, or final authority to workers was not accepted.

Consequences:

Future worker interfaces must make scope and evidence explicit for Codex review.

## D003 — Use mature external coding-agent CLIs rather than a custom agent loop

Status: Accepted

Reason:

The project should build on provider CLI or compatible coding-agent capabilities, not recreate their agent loops or model SDKs.

Alternatives considered:

A self-built agent loop and direct multi-provider model SDK integration are out of scope.

Consequences:

Any future provider integration starts by defining a concrete CLI contract.

## D004 — Keep runtime state outside version control

Status: Accepted

Reason:

Task runs, artifacts, and worktrees are runtime data rather than source.

Alternatives considered:

Committing runtime state to the project repository was not accepted.

Consequences:

Future runtime data belongs under .orch/, which is ignored by Git.

## D005 — OpenRouter is the sole external-model gateway

Status: Accepted

Reason:

The product needs access to different external models without maintaining a direct integration for every provider. OpenRouter is the only external gateway in scope; individual model support is a profile-level decision, not a provider adapter project.

Alternatives considered:

Direct Gemini, Claude, Muse, DeepSeek, Grok, or other provider SDK / CLI integrations are not part of the first product boundary.

Consequences:

The external execution path is `Codex CLI → OpenRouter → selected model`. A new model becomes eligible only after its concrete Codex CLI contract is verified.

## D006 — Profile availability is separate from run lifecycle

Status: Accepted

Reason:

A developer may temporarily stop using a model because a free window ended, pricing changed, or privacy policy is unsuitable. That is a policy decision about future dispatch, not a statement about a currently running task.

Alternatives considered:

Using one overloaded state for both model availability and task execution was not accepted.

Consequences:

External profiles have `enabled` or `frozen` state. Frozen profiles reject new dispatch with an explicit reason. Existing runs use their own lifecycle and stopping flow.

## D007 — External worker termination is two-stage and evidence-based

Status: Accepted

Reason:

An external worker can finish work yet remain alive, consuming memory. The user requires a gentle stop before force killing, plus a factual outcome.

Alternatives considered:

Only offering force kill, or recording a button click without checking the process, was not accepted.

Consequences:

The local supervisor will own an external run's process group, request TERM first, verify exit, then allow KILL when necessary. The run record must report the observed outcome.

## D008 — Local Web UI owns production OpenRouter configuration

Status: Accepted

Reason:

The final product must allow the user to configure OpenRouter API access inside the local dashboard rather than depend on a shell `.env` file.

Alternatives considered:

Using `.env` as the persistent product configuration was not accepted. It remains acceptable only for bounded local probes.

Consequences:

The implementation uses `keyring` and requires an available operating-system secret service: macOS Keychain, Windows Credential Locker, or Linux Secret Service. If none is available, settings fail closed rather than storing a plaintext fallback. Keys are never committed, embedded in profile exports, or displayed in full.

## D009 — Native worker visibility is declarative, not synthetic telemetry

Status: Accepted

Reason:

Native Codex subagents are controlled by Codex, while local external workers are controlled by the local supervisor. The dashboard must not invent process or cost data it cannot observe.

Alternatives considered:

Pretending that native workers have the same PID, RSS, kill, or per-run cost controls as local external processes was not accepted.

Consequences:

The dashboard may show configured native Luna lanes and verified routing facts, but labels them as Codex-managed. Rich operational controls are limited to external workers.

## D010 — OpenRouter catalog discovery creates external worker profiles

Status: Accepted

Reason:

Adding a worker means discovering a model and forming a usable local profile, not configuring credentials. The dashboard must validate a pasted OpenRouter model identifier or model link against the current catalog, resolve ambiguity explicitly, and prefill current model metadata.

Alternatives considered:

Using API-key settings as the add-worker flow, accepting an ambiguous display name silently, or treating catalog presence as a successful worker execution test was not accepted.

Consequences:

API-key configuration remains in Settings. A discovered model is marked separately from local harness compatibility, and model discovery never launches a paid task automatically.

## D011 — A profile owns its default reasoning strategy

Status: Accepted

Reason:

For a cost-effective external model, its useful operating point can be its highest supported reasoning effort rather than its cheapest one. A generic global low-effort default would discard that value.

Alternatives considered:

Always defaulting every model to maximum effort, allowing Codex to silently lower a profile's configured effort, or presenting a universal effort enum regardless of a model's real capabilities was not accepted.

Consequences:

The user selects a profile default only from the discovered model's supported efforts, or explicitly selects an automatic policy. In v0.1, fixed defaults are passed through unchanged and tasks cannot override them; an automatic Profile leaves the effort unset for Codex to choose. Each run records the configured effort and whether it came from a fixed or automatic Profile. D032 supersedes the earlier per-run override idea.

## D012 — Distribute the capability as a Codex Plugin with a core Skill

Status: Accepted

Reason:

The product is useful only when it appears naturally inside Codex, while its local dashboard remains a companion control surface rather than a separate application. A Plugin is the distribution boundary; `aiworker-relay` is the Codex-facing Skill.

Alternatives considered:

A bare standalone CLI, an independent Web application, or a package that silently rewrites Codex's global configuration were not accepted.

Consequences:

The Plugin bundle contains the `aiworker-relay` Skill and the minimum local resources it needs. It must not replace Codex's default model, provider, native worker profiles, system prompt, hooks, or project `AGENTS.md`. Exact distribution channel and local dependency handling remain open requirements.

## D013 — First-time configuration starts from a Codex task

Status: Accepted

Reason:

The user should not need to discover or operate a separate dashboard before the capability is available. Codex is the natural entry point for a Codex extension.

Alternatives considered:

Opening a standalone Web site first, or requiring a general-purpose CLI command as the main onboarding path, was not accepted.

Consequences:

The intended first-run interaction is a new Codex task invoking `$aiworker-relay setup`, which only opens the local Web control plane. OpenRouter API Key and external worker Profile configuration happen in that Web control plane, not in the Codex conversation, Skill parameters, or ordinary CLI configuration. The exact local launcher and distribution mechanism remain open requirements.

## D014 — External dispatch is consent-gated, not global interception

Status: Accepted

Reason:

An external run can incur cost and send task context to a provider. The capability may be available through explicit Skill invocation or task matching, but its availability is not authorization to dispatch invisibly.

Alternatives considered:

Always-on automatic dispatch, hook-based global interception, and silently substituting a worker or reasoning effort were not accepted.

Consequences:

An explicit user selection takes priority. When the user has not selected a profile, Codex may recommend one but needs the agreed confirmation path before a paid external dispatch. Hooks, if ever introduced, are not the source of dispatch authority.

## D015 — Use one on-demand cross-platform local control process

Status: Superseded by D031

Reason:

The product needs a locally managed Web control plane and process supervisor, but its normal state is a personal developer with no active external run. A single event-driven process minimizes idle work without sacrificing reliable ownership of a run's lifecycle.

Alternatives considered:

FastAPI/Uvicorn plus a separate frontend server, React or Electron, a permanent background service, WebSocket or periodic polling as the default update path, a database, Redis, and a separate process manager were not accepted.

Consequences:

`external-workersd` is a Python 3.12+ process using `asyncio` and `aiohttp`; it serves static Web assets, HTTP operations, SSE status events, and external run supervision. It starts on demand, remains alive while a browser client or external run exists, then exits after 60 seconds of inactivity. The Web UI uses static HTML, CSS, and JavaScript; status is pushed through SSE and mutations use HTTP `POST`.

`psutil` is used only for external run RSS observation and process-tree termination. Active external runs are sampled every two seconds; native Codex workers are never locally sampled. On POSIX, runs use independent process groups; on Windows, they use a new process group and recursive process-tree handling.

Persistent configuration uses atomic JSON and run evidence uses JSONL under `.orch/runs/`. Actual-cost aggregates wait for a reliable provider correlation rather than being reconstructed from logs. The runtime dependencies are `aiohttp`, `psutil`, `keyring`, and `truststore`; no database or plaintext secret fallback is introduced.

## D016 — Develop and alpha-test through a local marketplace

Status: Superseded by D027

Reason:

The first users need a Plugin-shaped experience inside Codex, but public directory submission would add distribution and review work before the local workflow is proven. Codex supports local marketplace testing for a Plugin containing skills.

Alternatives considered:

A standalone dashboard, a bare general-purpose CLI, an MCP server, and public Plugin publication as the first milestone were not accepted.

Consequences:

The development bundle has a `.codex-plugin/plugin.json` manifest and the Codex-facing Skill. Local marketplace installation remains the first acceptance path. D027 later accepts the public AIworker Relay source identity and repository while keeping formal Git-backed install/update acceptance open.

## D017 — Use one isolated Codex CLI harness and evidence artifacts

Status: Accepted

Reason:

The verified route already uses Codex CLI through OpenRouter. `codex exec --json`, `--ephemeral`, and `--output-last-message` provide lifecycle events and a final message without relying on provider-specific JSON Schema support.

Alternatives considered:

A fallback CLI, a direct model SDK, a custom agent loop, and a schema-enforced model result contract were not accepted.

Consequences:

Each run gets isolated `CODEX_HOME`, provider configuration, process group, task packet and `last-message.md`. The daemon owns JSONL lifecycle, exit, diff and process evidence; Codex evaluates their combination rather than treating a model's result JSON as authoritative.

## D018 — Isolate write runs in a fixed Git worktree path

Status: Accepted

Reason:

External coding work must not silently alter the main working directory. A detached worktree from `HEAD` gives one stable source snapshot and a diff that Codex can evaluate, without creating a generic workspace framework.

Alternatives considered:

Writing directly into the main worktree, automatic commit or merge, and synchronizing dirty source changes into the worktree were not accepted for the first cut.

Consequences:

Write runs require a Git repository with a resolvable `HEAD` and use `.orch/worktrees/<run-id>`. Main-worktree changes are excluded and disclosed. A run leaves changes for Codex review but does not commit or merge them.

## D019 — Separate experimental execution from verified routing eligibility

Status: Accepted

Reason:

Catalog discovery proves a model exists but does not prove the Codex harness works. Developers still need a controlled way to try inexpensive or newly available models.

Alternatives considered:

Treating discovery as compatibility, letting Codex recommend unverified Profiles, or prohibiting every unverified manual experiment were not accepted.

Consequences:

Profiles independently record availability (`enabled` / `frozen`) and harness status (`unverified` / `verified`). Codex recommends only verified enabled Profiles; an unverified enabled Profile requires explicit user selection and experimental-run confirmation.

## D020 — Defer actual cost attribution without concealing uncertainty

Status: Accepted

Reason:

OpenRouter can supply actual cost, but current Codex CLI JSON does not expose a generation identifier or cost that can be tied to one run. This does not prevent process control and evidence collection, but it prevents honest actual-cost reporting.

Alternatives considered:

Displaying model list prices, `$0`, or an unlabeled estimate as actual run cost was not accepted.

Consequences:

The first cut shows `pending` or `unavailable` cost attribution. It does not persist raw CLI transcripts just to extract token usage; token display and per-run actual costs wait for separately verified bounded sources and a correlation mechanism.

## D021 — Bootstrap an application-local Python runtime

Status: Accepted

Reason:

Cross-platform setup cannot assume that the user's global Python has `aiohttp`, `psutil`, `keyring`, and `truststore`, and it must not mutate global site-packages as an installation side effect.

Alternatives considered:

Requiring a manually prepared global environment, silently installing globally, bundling an Electron runtime, or adding a separate Node service were not accepted.

Consequences:

`$aiworker-relay setup` verifies Python 3.12+ and creates or reuses a dedicated `venv` in user application data. An explicit setup request is the authorization for that one-time local installation, so it does not ask for a second conversational confirmation before installing missing dependencies. A generic configuration request still does not bootstrap automatically. macOS uses `~/Library/Application Support/Codex External Workers`; Windows uses `%LOCALAPPDATA%\\Codex External Workers`; Linux uses `$XDG_DATA_HOME/codex-external-workers` or `~/.local/share/codex-external-workers`.

## D022 — Use a loopback control API between Skill, daemon, and Web

Status: Accepted

Reason:

The Skill needs a stable way to launch or reuse the local control process, and the static Web page needs live state and mutations. One loopback HTTP/SSE API keeps that boundary visible without an MCP server or a second desktop process.

Alternatives considered:

Binding to LAN interfaces, putting secrets in Skill parameters, using a database-backed IPC layer, or making the Web page own process control were not accepted.

Consequences:

`external-workersd` binds only to `127.0.0.1`. `orch setup` and `orch dispatch --profile <id> --packet <path>` are real Skill-facing commands; a small atomic daemon record permits reuse. The API returns stable error codes and never returns an API key.

## D023 — Keep the alpha Plugin and Python runtime in one repository source tree

Status: Superseded by D026

Reason:

The Plugin must not become a thin Skill that works only when a separately and manually installed runtime happens to exist. Keeping the manifest, Skill and Python runtime in the repository root preserves one source of truth for local marketplace development and avoids copying the runtime into a second plugin folder.

Alternatives considered:

A nested plugin folder with a duplicate runtime, a Skill that assumes a global `orch` installation, and a separate application installer were not accepted for the alpha path.

Consequences:

The repository-root source placement did keep all runtime code together, but it did not follow Codex marketplace's standard `./plugins/<plugin-name>` source convention and was not surfaced as an installable Plugin. D026 preserves the one-package requirement at the compatible location.

## D024 — Use the operating-system trust store for provider HTTPS

Status: Accepted

Reason:

The actual Python runtime on the development machine could reach OpenRouter with the system client but failed TLS verification through its default `urllib` certificate configuration. Model discovery and Key validation must retain HTTPS verification while working with the operating system's trusted roots.

Alternatives considered:

Disabling certificate verification, requiring a user to repair a global Python certificate bundle, or shipping a static CA bundle as the only source of trust were not accepted.

Consequences:

`truststore` is the fourth runtime dependency. Provider requests create an explicit system-backed TLS context; Plugin setup installs it in the application-local venv alongside the other runtime dependencies.

## D025 — Read account and benchmark facts only on explicit request

Status: Accepted

Reason:

The dashboard should help a developer compare workers and understand available OpenRouter credit without becoming a background polling client or inventing a local cost-accounting system. OpenRouter exposes account credits only to management Keys, while its benchmark endpoint is authenticated and rate-limited.

Alternatives considered:

Polling provider data while the dashboard is idle, treating a regular API Key's configured limit as account balance, writing static benchmark scores into Profiles, or showing account data as a run's actual cost were not accepted.

Consequences:

The Web page makes one authenticated provider request only after the user presses refresh. A management Key can return account credits; otherwise a provider-reported current-Key limit is labeled separately when available. Worker detail keeps only exact `model_permaslug` benchmark records with their source and time. These facts remain separate from D020's run-to-cost attribution.

## D026 — Package the alpha bundle at Codex's standard marketplace source path

Status: Accepted

Reason:

The local marketplace did not surface the Plugin while its entry pointed outside the conventional Plugin subtree. In the historical alpha identity, using the standard `./plugins/external-workers` source path caused Codex to list `external-workers@aiworker-local` as installable. The same discovery constraint applies to the current `./plugins/aiworker-relay` source.

Alternatives considered:

Keeping the repository root as a non-standard Plugin source, creating a thin Skill that assumes a separately installed runtime, or copying the Python runtime into a second package were not accepted.

Consequences:

`plugins/aiworker-relay/` is the only installable Plugin source. It contains `.codex-plugin/plugin.json`, `skills/`, `scripts/`, `pyproject.toml`, and `src/`; its launcher installs that same directory into the application-local venv. The repository root retains the marketplace manifest, product documentation, diagrams, test suite, and development-only materials. No global Python runtime or duplicate execution package is introduced.

## D027 — Use AIworker Relay as the public product identity

Status: Accepted

Reason:

The product needs one memorable name that describes its job without pretending to be a second orchestrator. The user selected **AIworker Relay** for the public product, Plugin, and Skill identity before creating the public pre-release repository.

Alternatives considered:

Keeping `external-workers` as the public name, maintaining a permanent synonym Skill, or renaming internal persisted identifiers at the same time were not accepted.

Consequences:

The public Plugin ID, Plugin folder, marketplace entry, manifest display name, and Codex-facing Skill are all `aiworker-relay` / AIworker Relay. Existing alpha users remove the old `external-workers` Plugin and install the new identity; no long-term compatibility alias is maintained.

The application-data directory, keyring service, Python distribution, and `external-workersd` process name remain internal stable identifiers. Changing those would require a separate, accepted data-migration contract and is not part of a product-name change.

## D028 — Reconcile the local runtime only during explicit setup

Status: Accepted

Reason:

An installed Plugin bundle and its application-local runtime can drift. Replacing a runtime while a worker is active risks terminating work, while doing it implicitly during dispatch hides a material local mutation inside a task request.

Alternatives considered:

Updating on every dispatch, adding a background update watcher, leaving runtime drift undetected, or requiring the user to run pip and manage venvs were not accepted.

Consequences:

`$aiworker-relay setup` compares the canonical package version with runtime and daemon versions. It replaces only a verified idle runtime, keeps one short-lived `venv.previous` recovery copy, and refuses dispatch while versions differ. An active run produces an explicit deferred result; an unknown daemon state blocks setup rather than risking an unrelated process.

## D029 — Use a Git-backed marketplace for public pre-release distribution

Status: Accepted

Reason:

The public product needs a normal Codex installation path rather than a local filesystem checkout. The official marketplace contract supports a Git catalog and a `git-subdir` Plugin source in the same repository.

Alternatives considered:

Presenting the local marketplace as the public channel, building a separate custom installer, or publishing a duplicate runtime package were not accepted.

Consequences:

The public catalog at `.agents/plugins/marketplace.json` refers to `https://github.com/liuyejinghong/aiworker-relay.git`, `./plugins/aiworker-relay`, and `main`. Source-checkout development may still use a local marketplace, but public installation and update instructions use `codex plugin marketplace add` and `codex plugin add`. A clean isolated CLI install plus `0.1.6 → 0.1.7 → 0.1.8` marketplace upgrades, and a fresh current `0.1.9` install, have been observed; untested Codex Desktop update interactions are not silently claimed as equivalent.

## D030 — Use workspace-write approval for non-interactive external runs

Status: Accepted

Reason:

An isolated `codex exec` that needed to write a bounded file made no progress when it could not answer a terminal approval prompt. Re-running the exact NVIDIA model with Codex's `--approve-for-me` mode produced the expected tool event stream and the only permitted file change.

Alternatives considered:

Using `--dangerously-bypass-approvals-and-sandbox`, building a second model harness, or assuming a free-model timeout were not accepted.

Consequences:

Every external run includes `--approve-for-me`. Its tools remain inside Codex's supported workspace-write approval mode and the detached worktree; the product does not claim a global sandbox bypass. A dashboard-managed successful write is necessary evidence for any future Profile promotion, but the capability matrix and user-visible promotion operation remain explicit product decisions rather than automatic side effects of one run.

## D031 — Keep one stable local control-plane entry

Status: Accepted

Reason:

The prior 60-second idle exit made the dashboard URL disappear after ordinary use. A browser bookmark cannot restart a stopped HTTP process, so normal use otherwise required returning to Codex solely to reopen the page. The user needs a stable local entry without changing external-worker consent or making idle provider calls.

Alternatives considered:

Adding a Plugin MCP server solely for a custom Codex UI, a second proxy or watcher process, a separate desktop application, repeated idle timers, and an always-open browser client were not accepted.

Consequences:

The existing `external-workersd` gains a persistent mode and serves the fixed loopback address `127.0.0.1:49178`. On macOS, explicit setup writes one user-owned `com.aiworker.relay` LaunchAgent that starts this same daemon at login; it contains only local runtime, project, port, persistence, the setup-resolved absolute Codex CLI path, and a process-local minimal `PATH` for that CLI and its resolved Node runtime. It never stores an OpenRouter Key or Profile value, and does not change the user's global shell environment. The daemon has no idle provider polling, account refresh, external dispatch, or RSS sampling unless a run is active.

The daemon remains bound to one project root. Setup may replace a verified idle temporary daemon to establish the fixed entry, but it defers while a run is active and blocks on unknown state; after a verified idle stop it uses the same reusable-address semantics as the `aiohttp` listener before rebinding the fixed loopback port. It never silently switches the persisted entry to another project. Windows and Linux use the same fixed persistent daemon for the current login session, while a platform-native login entry is not claimed until it is implemented and accepted.

## D032 — Use the Profile default as the only v0.1 reasoning value

Status:

Accepted

Reason:

The v0.1 CLI and UI do not expose per-run reasoning selection. Accepting an override in the API would create an unsupported path and make the persisted run value ambiguous.

Consequences:

`POST /api/runs` and `DaemonState.create_run` reject any payload containing the `reasoning_effort` key, including `null` and an empty string, with `reasoning_override_not_supported` before packet loading or worktree creation. Normal runs use `Profile.default_reasoning`; `TaskPacket` and `RunRecord` persist the separate source as `profile_default` or `profile_auto`. Dispatch does not refresh the catalog in v0.1: it uses the Profile value validated and stored when the Profile was created, so provider catalog availability cannot silently change a run setting. Existing records without the source field remain readable.

## D033 — Use one startup capability with separate browser and CLI modes

Status:

Accepted

Reason:

Loopback binding alone does not distinguish the AIworker daemon from another local service, and a browser page can send cross-site requests to loopback. The control plane needs one short-lived per-process capability while keeping the existing local-user trust boundary and without introducing accounts or a general authentication framework.

Consequences:

Each daemon startup generates a random capability and stores it only in owner-only `daemon.json`. A top-level document served from the exact `127.0.0.1` listener can receive it as a host-only HttpOnly SameSite=Strict cookie. CLI and launcher requests read the same file and send `X-AIworker-Capability`; a request cannot use both modes. API requests validate exact Host, Origin and Fetch Metadata, and JSON writes require `application/json`. Health exposes only non-secret identity fields (PID, port, project root, project `.orch` runtime root, version and persistent state). A live daemon record without a capability is unknown and cannot be reused, stopped, killed or overwritten; a dead PID is stale and may be replaced. The launcher has no direct signal fallback for an unavailable shutdown endpoint.
