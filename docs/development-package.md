# AIworker Relay v0.1 开发包

状态：首个实现切片已完成本机代码、页面路径、Git-backed Marketplace CLI 安装/更新、受控 runtime 收敛，以及修复后 NVIDIA dashboard-managed 最小 write / TERM 停止验收。本文同时记录已交付边界与仍待明确的 Profile 能力晋级规则。

## 首个闭环

首个闭环让开发者从 Codex 安装本地 Plugin，完成一次 Web 配置，再把一个明确同意的任务交给外部 worker。`external-workersd` 自动启动、监管本机进程并在看板上留下可核验的证据；Codex 根据这些证据而不是看板按钮或模型聊天记录完成验收。

这个切片支持真实的受隔离写入任务。它不包含自动路由、自动 fallback、公开 Plugin 发布、实际费用归因、benchmark 实验平台或原生 worker 遥测。

## 已冻结的工程合同

| 关注点 | v0.1 合同 | 不做什么 |
| --- | --- | --- |
| 开发分发 | 使用 Codex local marketplace 安装 Plugin；仓库 `.agents/plugins/marketplace.json` 指向 `plugins/aiworker-relay/`，该唯一 Plugin source 同时包含 `aiworker-relay` Skill 与启动本地运行时所需资源。公开目录发布不是本切片工作。 | 不安装 MCP server，不改写 Codex 的默认模型、provider 或原生 worker 配置。 |
| 运行时 bootstrap | 明确的 `$aiworker-relay setup` 通过 Plugin 随包 launcher 检查 Python 3.12+，在用户应用数据目录创建并复用专用 `venv`，再从该环境调用 `orch setup` 启动 `external-workersd`。缺失依赖时，这一次明确 setup 直接授权安装，不再追加对话确认。 | 不依赖或改写用户全局 Python site-packages。首次应用级安装已完成一次真实 marketplace 验收；改版 bootstrap 待重新安装复核。 |
| 首发 harness | 只使用 `codex exec`，以 `--json`、`--ephemeral`、`--strict-config`、`--approve-for-me` 和 `--output-last-message` 运行。每个 run 使用独立 `CODEX_HOME`、隔离 HOME 与 run-scoped permission profile；安全配置和用户已选 reasoning 由 CLI 固定。 | 不同时传旧 `--sandbox`，不引入第二条 CLI、SDK、自研 agent loop 或危险的全局 sandbox bypass。 |
| 上下文 | Task Packet 提供目标、范围、禁止修改项、已知事实、验收与验证。外部 run 不继承主 Codex 对话、主 `CODEX_HOME` 或 hooks；工作区内的项目规则仍是该项目的事实。 | 不复制主 Codex 的完整上下文。 |
| 写入隔离 | 写入任务只在从当前 `HEAD` 创建的 Git worktree 中执行，worktree 位于 `.orch/worktrees/<run-id>`。主工作区保持不变，run 不创建 commit，也不自动 merge。 | 不直接让外部 worker 写主工作区，不复制未提交改动。 |
| dirty 工作区 | 若主工作区有未提交改动，run 仍以 `HEAD` 为源；看板和 Task Packet 必须明确标记“未提交改动未包含”。用户可先整理版本库或改用原生 worker。 | 不在首版实现 patch 复制、stash 恢复或自动冲突处理。 |
| Result 收敛 | `last-message.md` 保存模型最终文字；daemon 独立保存 JSONL 生命周期、退出码、diff、文件清单和停止结果。Codex 将这些 artifact 一起验收。 | 不依赖 `--output-schema` 或要求模型产出可靠 JSON Schema。 |
| Profile 可用性 | Profile 有 `enabled` / `frozen` 长期状态，以及 `unverified` / `verified` harness 状态。已验证 profile 可被 Codex 建议；未验证 profile 只能由用户显式选择并确认“实验性运行”。 | 不让 Codex 自动挑选未验证模型，也不因冻结静默换模型。 |
| 失败处理 | 429、启动失败和模型中断写入 run 记录并允许用户手动重试。 | 不实现自动 fallback 或自动重派。 |
| 停止 | 停止请求先向外部 run 的进程组发送 TERM，最多观察 10 秒；仍存活时由用户再次确认 KILL。最终状态必须来自 OS 观察。 | 不把一次 HTTP 点击当成停止成功。 |
| 账户与公开跑分 | 用量页和详情页只在用户点击时读取 OpenRouter。账户总余额需要 management Key；普通 Key 只展示 provider 返回的自身限额。跑分只保留精确模型标识的来源与时间。 | 不轮询 provider，不把账户 / Key 限额当作 run 实际费用，不写死或合成 benchmark 分数。 |
| 费用 | 首个切片显示 `pending` / `unavailable` 归因状态。原始 CLI transcript 不持久化；token 展示需在建立有界解析来源后再加入。实际 per-run、日/月费用和预算告警在建立可靠 run-to-cost 关联后单独实现。 | 不用模型标价、`$0` 或近似值伪造实际费用。 |
| 秘密与本地数据 | API key 只在 Web 设置页录入，由 `keyring` 保存。每个 run 的本地证据落在 `.orch/runs/<run-id>/`，不含 API key；OpenRouter HTTPS 使用系统证书库。 | 不落 `.env`、数据库、明文密钥回退或跳过 TLS 验证。 |

本地 marketplace 适合作为个人开发与 alpha 测试的分发路径；官方 Plugin 文档也明确将其作为提交公共目录前的测试方式。[Build plugins](https://developers.openai.com/codex/build-plugins)

用户应用数据目录固定为 macOS 的 `~/Library/Application Support/Codex External Workers`、Windows 的 `%LOCALAPPDATA%\\Codex External Workers`，以及 Linux 的 `$XDG_DATA_HOME/codex-external-workers`（未设置时为 `~/.local/share/codex-external-workers`）。Profile、运行时 `venv` 和小型汇总位于这里；项目特定的运行证据继续位于项目 `.orch/`。

## 外部 run 的固定行为

每次外部派发先验证用户显式选择或接受 Codex 建议，随后检查 Profile 是否冻结。通过检查后，控制面创建 run 目录、worktree 和最小 `CODEX_HOME`，再启动一个独立进程组中的 `codex exec`。浏览器是否打开不会改变 run 生命周期；`external-workersd` 保持本机 loopback 控制面可用，但空闲时不采样 run、不读取 provider，也不派发任何 worker。

写入任务要求 Git 仓库存在可解析的 `HEAD`。这是一项刻意透明的 alpha 限制：它给 worker 一个稳定、可比较的源快照，也使 diff 能够成为 Codex 的验收证据。主工作区未提交改动不被复制，避免为尚未证实的脏树同步需求构建复杂的 patch / merge 层。

每个 run 使用下面的最小命令语义，其中路径和配置均由控制面生成，而不是由用户手写：

```text
codex exec --json --ephemeral \
  --strict-config \
  --approve-for-me \
  --output-last-message <run>/last-message.md \
  --cd <worktree> \
  <task-packet-prompt>
```

隔离 `CODEX_HOME` 只包含此次 OpenRouter provider 与 `aiworker` permission profile 的配置。控制面从系统密钥服务短暂读取 key，并仅向 provider 子进程提供认证环境；worker 工具使用 `inherit=none` 的隔离 HOME / TMP / XDG，无法继承该 Key。项目 `.codex/config.toml` 按 untrusted 跳过，但 worktree 中的 `AGENTS.md` 仍作为项目事实加载。

## Task Packet 与 Run Evidence

Task Packet 是面向 worker 的最小 Markdown 文档，不是模型强制 schema。它以稳定字段表达 Codex 已经决定的事实，避免将主对话历史塞进派发输入。

| Packet 字段 | 含义 |
| --- | --- |
| `run_id` | 控制面生成的不可重复标识。 |
| `goal` | 可完成的工作目标。 |
| `scope` 与 `do_not_touch` | 允许和禁止修改的文件或操作。 |
| `existing_behavior` 与 `expected_behavior` | 已知事实与预期结果；纯探索任务可明确写为不适用。 |
| `constraints` | 工具、依赖、外部访问、数据边界和时间限制。 |
| `acceptance_criteria` 与 `verification` | Codex 最终判断所需证据。 |
| `profile`、`reasoning_effort` 与 `selection_source` | 本次模型和推理策略的实际来源。 |
| `workspace` | worktree 路径、`HEAD` 与 dirty 源工作区提示。 |

Run Evidence 由 `external-workersd` 产生或采集，而不是相信模型自行填报。其最小内容是 `events.jsonl`、`last-message.md`、`run.json`、`diff.patch`、变更文件清单、退出状态、RSS 样本、停止请求和停止结果。模型最后消息可以补充摘要、测试和未决问题，但它不是唯一事实来源。

## 本地状态与 Profile 状态

用户级状态只保存 Profile、启用状态和已发现模型 metadata；项目级状态只保存该项目的 run evidence。Profile 的目录发现与运行资格不是同一个概念，状态含义如下。

| 状态 | 可否由 Codex 建议 | 可否派发 |
| --- | --- | --- |
| `frozen` | 否 | 否，返回冻结原因。 |
| `enabled + verified` | 是 | 是，仍需用户选择或接受建议。 |
| `enabled + unverified` | 否 | 仅用户显式选择并确认实验性运行时允许。 |

成功完成一个具体 harness 能力检查后才可把 Profile 标记为 `verified`。验证记录必须写明能力范围，例如“单轮成功”不能扩展成“多轮工具调用已验证”。

## 模块边界与实施顺序

依赖关系保持单向：Plugin / Skill 只触发本地控制面；控制面拥有 Profile、run 和进程；Web 页面只调用控制面 API；Codex 从 Task Packet 和 Evidence 获得判断材料。不存在全局 service locator、provider adapter 或第二套 agent loop。

| 顺序 | 所有权 | 交付物 | 依赖 |
| --- | --- | --- | --- |
| 1 | Sol | Plugin manifest、`aiworker-relay` Skill 与 local marketplace 开发安装说明。 | 已冻结的 Plugin 形态。 |
| 2 | 一个后端 Worker | Profile 存储、keyring 接口、OpenRouter 目录查询和最小 `external-workersd` 启动路径。 | Python 运行时与 Web 设置页契约。 |
| 3 | 同一后端 Worker | Task Packet、worktree、`codex exec` 生命周期、JSONL evidence、RSS 和 TERM / KILL。 | 步骤 2 的 Profile 与 key 获取。 |
| 4 | 一个前端 Worker | 现有 HTML 原型演进为本机静态页面，连接 Profile、run、SSE 与停止 API。 | 步骤 2、3 固定 API。 |
| 5 | Sol | 在真实低成本模型上完成一次 setup、一次外部 run、一次停止和 Codex 证据验收。 | 所有前述步骤。 |

后端步骤 2 与 3 由同一个写入 Worker 连续完成，避免 Profile、run 和 API 合同被两个人并行改写。前端只在 API 被固定后开始；Sol 保留跨模块集成、实际 provider 验收和所有架构决定。

## 本机控制 API 与 Skill 入口

`external-workersd` 只绑定 `127.0.0.1`，不监听局域网地址。它在用户应用数据目录保存原子 `daemon.json`，其中包含本机 PID、随机端口、启动时间和绑定的项目根目录；启动者先调用 `/api/health`，确认存活且项目一致才复用，无法连接时才将其视为过期记录。不同项目当前明确拒绝复用，直到多项目体验被单独决定。这个文件不是通用 service registry。

| 入口 | 调用者 | 行为 |
| --- | --- | --- |
| `orch setup` | Plugin launcher（由 `aiworker-relay` Skill 调用） | 启动或复用 daemon，打开本机 Web 控制面。 |
| `orch dispatch --profile <id> --packet <path>` | Plugin launcher（由 `aiworker-relay` Skill 调用） | 启动或复用 daemon，再把固定 v1 Task Packet 交给控制面。它不是给用户学习的常用派发 CLI。 |
| `GET /api/health` | launcher | 返回 daemon 存活与版本，不返回 key。 |
| `GET /api/overview` | Web | 返回 Profile 摘要、外部 run 摘要与 native worker 的声明性配置。 |
| `GET /api/events` | Web | SSE 推送 Profile 与 run 状态变化；连接断开不影响 run。 |
| `GET` / `PUT /api/openrouter-key` | Web | 读取是否已配置，或保存并验证新的 key；读取永不回显 key。 |
| `GET /api/openrouter-account` | Web 用量页 | 只在用户点击时读取账户总余额，或当前 Key 的 provider 限额；不会返回 key，也不代表 run 实际费用。 |
| `GET /api/models?query=` | Web | 查询 OpenRouter 模型目录，供粘贴模型 slug / 链接时消歧。 |
| `POST /api/profiles` 与 Profile 状态操作 | Web | 创建 Profile，启用、冻结或恢复 Profile。 |
| `GET /api/profiles/<id>/benchmarks` | Worker 详情页 | 只在用户点击时读取精确模型标识的公开 benchmark 记录。 |
| `POST /api/runs` | `orch dispatch` | 校验 consent、Profile 与 Task Packet，创建 worktree 并启动外部 run。 |
| `POST /api/runs/<id>/stop` | Web | `{ "force": false }` 发起 TERM；仍存活且用户再次确认时 `{ "force": true }` 发起 KILL。 |

API 的 error body 固定为 `{ "code": "…", "message": "…" }`。`frozen_profile`、`unverified_profile_requires_confirmation`、`missing_key`、`run_not_stoppable` 和 `run_not_found` 是当前必须可区分的错误；不为未来 provider 设计泛化错误层。

## 已完成的本机验收与剩余闭环

已完成的本机验收不是仅看页面截图：聚焦测试覆盖 Task Packet、worktree、TLS、Profile 与进程两阶段停止；隔离 daemon 真实服务了多页 Web；通过页面查询 OpenRouter、发现 Ox Alpha、带入可用推理档位、创建 Profile、冻结 Profile，并由真实 API 拒绝冻结派发。

Git-backed marketplace 的干净 CLI 安装 / 更新与首次 Plugin runtime bootstrap 已经通过。NVIDIA 模型已在修复后的 dashboard-managed detached worktree 中实际完成限定 marker 写入；文件清单只含该 marker、`git diff --check` 通过，真实外部 CLI child 随后由 dashboard 温和停止并记录 `term_exited`。历史 `429` 和本机 Node 启动失败仍保留为失败证据，不覆盖这项通过的窄验收。Codex 最后读取 Task Packet、可得的模型文字、diff、文件清单、进程结果与 run record；有意 TERM 停止的 run 不要求模型再产生最终文字。

实际费用没有可信归因时，验收只检查它被标记为 `pending` 或 `unavailable`。任何显示为实际金额的费用都必须有可追溯的 provider 关联证据。

## 明确后置

实际 OpenRouter cost correlation、日/月预算、dirty 工作区同步、公共发布、自动模型建议、自动 reasoning、retry fallback、benchmark / A-B 平台和 reviewer worker 都在本切片之外。它们保留在 [requirements.md](requirements.md)，不会以预留接口或空壳命令进入实现。
