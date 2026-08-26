# Requirements

这是后续需求对齐的唯一 agenda。列为待决的问题不是已接受的设计，不得由代码、原型或文档默认替用户决定。

## 产品目标

帮助个人开发者在 Codex 的主控下使用外部 OpenRouter 模型作为派生 coding worker：在适合的任务上节省速度或成本，同时保留 Codex 的范围控制、证据要求与最终验收。

## 已确定

- Codex / Sol 是主控和最终 authority；worker 只提供劳动与证据。
- 正式产品名是 **AIworker Relay**；Codex Plugin、Plugin folder 与核心入口统一为 `aiworker-relay`，本地 Web 看板是 setup 后打开的配套控制面，而非独立产品入口。
- 首次配置由用户在一个新 Codex task 中调用 `$aiworker-relay setup` 发起。
- 安装后，OpenRouter API Key 与外部 worker Profile 只在本机 Web 控制面中配置；`$aiworker-relay setup` 只负责打开该页面，不在 Codex 对话、Skill 参数或普通 CLI 中接收配置值。
- Plugin 安装不得静默改写主 Codex 的默认模型、provider、原生 worker profile、系统提示、hooks 或项目 `AGENTS.md`；外部 run 才使用隔离配置。
- Skill 可以被用户显式调用或在任务匹配时被 Codex 采用，但可能产生外部用量的派发不能成为不可见的全局自动拦截。
- hooks 不作为首版外部 worker 路由或生命周期控制的核心机制；若未来采用，只能在实际能力已验证后承担明确、受限的作用。
- 用户显式指定模型优先于 Codex 的建议。
- 外部模型统一经过 OpenRouter；不维护 Gemini、Claude、Muse 等的直接 provider 集成。
- 首个优先验证的外部执行方式是隔离配置下的 Codex CLI，而非自研 agent loop。
- worker task 使用已有 Sol worker routing 的 bounded packet、scope 和验收思路。
- 原生 worker 与外部 worker 在一个产品里展示，但运行权属不同。
- 原生 worker 当前只需展示已配置的 Luna Medium / Luna Max 及“由 Codex 管理”；拿不到实时状态不伪造。
- 外部 worker profile 需要独立的启用 / 冻结状态。
- 对冻结 profile 的新派发必须被拒绝，并明确告知用户它处于冻结状态。
- 外部 run 需要任务隔离、可观测的本地进程、RSS 监控、结构化收敛和停止结果。
- 停止外部 run 的顺序是温和停止、确认仍存活后再强制终止，并报告实际结果。
- 外部 worker 需要单 run、日、月的 usage / cost 视图和预算告警。
- OpenRouter 账户余额与当前 API Key 的限额是独立的 provider 事实：只在用户从 Web 主动刷新时读取，明确区分管理 Key 可读的账户总额、普通 Key 可读的自身限额，以及尚未建立的 per-run 实际费用归因。
- provider privacy / data-retention 差异需要作为 profile 可见信息，而非隐藏配置。
- 每个外部 worker 需要独立详情卡：模型参数、上下文窗口、当前价格、数据保留提示、公共 benchmark 参考与本机兼容性验证。公共跑分必须带来源和更新时间；未收录时明确显示无数据，不得虚构分数。
- 公开 benchmark 只在用户从详情页主动刷新时读取；只展示与 Profile 精确模型标识匹配的 OpenRouter 记录，不把它当成调度资格或本机兼容性证明。
- OpenRouter API key 的生产配置入口在本地 Web UI；仓库与运行日志不得保存 key。
- 添加外部 worker 是模型发现流程：用户粘贴 OpenRouter 模型标识或模型页链接，系统查询当前模型目录、让用户处理歧义匹配，并自动带入当前 metadata；它不是 API key 设置入口。
- 模型目录收录只证明模型当前可发现，不等于外部 harness 已兼容。新 profile 必须把“已识别”与“本机尚未验证”分开；添加时不自动发起会消耗额度的任务。
- 每个外部 worker profile 有用户确认的默认推理策略，只能从该模型实际支持的档位中选择。任务未指定档位时，Codex 必须按固定默认值派发；用户明确指定档位优先。只有 profile 被用户设为“自动”时，Codex 才可选择档位。
- 产品保持本地、轻量、CLI first；Web 看板是运行控制面，不是远程 SaaS。
- 首版跨平台目标是 macOS、Windows 和具备系统密钥服务的桌面 Linux。
- 本地控制面是单个按需 `external-workersd`：Python 3.12+、`asyncio` 与 `aiohttp`；同一进程服务本机 Web、SSE、状态 API 与外部 run 监管。一个活跃 daemon 绑定一个项目根目录；第二个项目不得静默复用它。
- 页面使用静态 HTML、CSS 和原生 JavaScript；状态使用 SSE 推送，用户操作使用 HTTP `POST`，不使用 React、WebSocket、Node 常驻进程或桌面壳。
- 外部 run 用 `asyncio` 管理生命周期，`psutil` 读取 RSS 与在停止时枚举进程树。只有活跃外部 run 每 2 秒采样一次；原生 worker 不采样。
- API Key 使用 `keyring` 写入操作系统密钥服务；密钥服务不可用时拒绝保存，不回退到明文文件。Provider HTTPS 使用操作系统证书库，不关闭 TLS 验证。
- 用户级 Profile 使用原子 JSON；项目级 run 证据使用 `.orch/runs/` 下的 JSONL。真实费用日/月汇总在可靠归因前不生成，不引入数据库。
- 没有浏览器客户端且没有活跃外部 run 时，`external-workersd` 在 60 秒后退出；看板打开但没有活跃 run 时不做状态采样或自动 provider 请求，账户和公开跑分只接受用户主动刷新。
- 目标是升级并融合进 `sol-worker-routing-codex`，不是长期维护平行路由项目。
- 源码开发可使用 Codex local marketplace；面向其他开发者的公开 pre-release 使用 Git-backed marketplace。Plugin 包含 `aiworker-relay` Skill，不含 MCP server；该公开安装与更新路径仍需完成端到端验收。
- 公开 pre-release 源码仓库是 [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay)。仓库公开不等于 Git-backed marketplace 安装或 runtime 更新已经验收。
- `$aiworker-relay setup` 使用 Python 3.12+ 在用户应用数据目录创建并复用专用 `venv`；明确的 setup 请求授权这一次本机安装，缺失依赖时不再要求第二次对话确认，不依赖或改写全局 Python 包。泛泛的配置请求不得自动 bootstrap。用户应用数据目录分别为 macOS `~/Library/Application Support/Codex External Workers`、Windows `%LOCALAPPDATA%\\Codex External Workers`、Linux `$XDG_DATA_HOME/codex-external-workers`（默认 `~/.local/share/codex-external-workers`）。
- `external-workersd` 只绑定 loopback `127.0.0.1`；`orch setup` 与 `orch dispatch --profile <id> --packet <path>` 是由 Skill 调用的真实本机入口。
- 首发外部 harness 只有隔离的 `codex exec --json --ephemeral --output-last-message`；不建设备用 CLI 或模型 SDK 路径。
- 外部 write run 必须从项目 `HEAD` 创建 detached Git worktree，位于 `.orch/worktrees/<run-id>`；不自动 commit、merge 或同步主工作区的未提交改动。
- Task Packet 使用 v1 固定字段表达目标、范围、禁止修改项、已知事实、约束、验收、验证、Profile、推理档位、选择来源、数据边界与 workspace。模型最终文字与 daemon 采集的 lifecycle、diff、退出状态共同构成 Result Evidence，不依赖 JSON Schema enforcement。
- Profile 同时保存 `enabled` / `frozen` 和 `unverified` / `verified`。未验证 Profile 不得被 Codex 建议；只有用户显式选择并确认实验性运行时允许派发。
- 429、模型中断与启动失败只记录并允许用户手动重试；首版没有自动 fallback。
- 停止外部 run 时先 TERM 并观察最多 10 秒，仍存活才允许用户确认 KILL。
- 真实费用归因不阻止外部 run 控制路径。归因未建立时只显示 `pending` / `unavailable`；token 需先有不保存原始 transcript 的可靠来源，实际单 run、日/月费用和预算告警后置。
- LM Studio / MLX 等本地模型接入已明确后置；当前不为它建立执行路径、配置项或兼容层。

## 当前阶段边界

本次已完成：符合 Codex marketplace source 约定的 AIworker Relay Plugin package、`aiworker-relay` Skill、应用级 bootstrap launcher、loopback `external-workersd`、Profile / Key / run API、账户与公开跑分的按需读取、静态多页 Web、隔离 worktree / `codex exec` 路径、JSONL 证据与两阶段停止代码；canonical package `VERSION`、setup-only runtime 收敛、活跃 run 更新延后、idle daemon 受控退出、更新失败恢复与 dispatch 版本不一致拒绝。

本机真实路径已验证：daemon 启动、页面资源、无 Key 的 Ox Alpha 模型发现、Profile 创建、推理档位带入、冻结与拒绝派发；management Key 的账户总额读取；历史 alpha 身份下 `aiworker-local` marketplace 对 `external-workers` 的发现、用户首次 Plugin 安装和应用级 runtime bootstrap；以及真实用户应用数据从 `0.1.0` 至 `0.1.6` 的空闲 runtime 收敛。聚焦测试覆盖 package、daemon、TLS、Profile、Task Packet、worktree、TERM → KILL、runtime 失败恢复、active update defer 与错误摘要脱敏。

尚未验收：Git-backed marketplace 的干净新装/已安装 bundle 更新、工具调用成功的真实外部 write run 与真实看板停止联动。Ox Alpha 的实验性 write probe 当前在 provider 侧以 `400 Server tool request failed` 结束，故它不能被标为 verified。实际费用、日/月汇总、预算、自动路由与 fallback 均未实现。

## 待确认

### 1. 外部 worker 的实际执行合同

- 哪些模型必须通过“单轮 / 工具调用 / 多轮收敛 / 取消”兼容性矩阵后才可启用？
- 哪些已验证能力足以将一个 Profile 从 `unverified` 升为 `verified`；不同模型是否需要分别记录单轮、工具调用、多轮收敛与取消结果？

### 1b. Plugin、runtime 与发布更新

- major breaking change 是否要求额外用户确认；patch / minor 是否可沿用 setup 的既有本机安装授权？
- 看板是否只呈现当前 bundle/runtime/daemon 版本与可操作的更新结果，还是确有用户价值需要维护 release history？
- Codex Desktop 对 Git marketplace 刷新后“已安装 Plugin 更新”的实际行为是什么，应该如何准确写入新用户指引？

### 2. Task 和 Result Contract

- `verified` Profile 的能力记录与 Task Packet / Run Evidence 如何建立最小、稳定的关联，而不演变成通用 workflow schema？

### 3. 费用、配额与预算

- 怎样可靠把 OpenRouter 的 generation / `cost` 对应到某个 `codex exec` run？
- 若未来引入估算，如何与实际费用清晰区分？
- 实际费用可用后，日 / 月范围按本地时区还是 UTC 计算？
- 实际费用可用后，预算是告警、冻结 profile，还是只阻止新派发？
- 免费模型的限流、可用性和价格变化如何进入 profile 的可用性判断？

### 4. 本地数据与秘密

- 任务 prompt、stdout、diff 和 provider metadata 各自保留多久？
- 哪些数据应默认红脱敏，哪些需要用户明确允许才可发送给外部 provider？
- 用户级应用数据未来升级时的迁移规则是什么？
- 模型 metadata（价格、隐私标签、跑分）的用户主动刷新和本地快照更新策略是什么？

### 5. 看板与操作权限

- 温和停止的等待时长、强制终止的确认方式和进程树处理规则是什么？
- profile 冻结是否允许等待当前 run 完成，还是需要额外提供“冻结并停止”？
- 本地 UI 与 Codex CLI 分别负责哪些操作入口；Codex Desktop 能否提供原生入口仍待验证。
- macOS、Windows 和 Linux 的最低支持版本、密钥服务前置条件与进程终止验收矩阵是什么？
- 多项目同时需要外部 worker 时，应复用一个真正的多项目控制面，还是要求显式切换？当前实现选择安全拒绝，尚未决定产品体验。
- Codex 中“明确指定 Worker”的最终引导文案是什么，是否需要提供可复制示例？
- 普通 Key 与 management Key 是否继续使用同一个设置字段，还是需要第二个可选入口？
- 移动端是完整支持目标，还是只保证桌面浏览器的可用窄屏布局？

### 6. 路由与质量

- Codex 的建议应基于哪些简明输入：用户目标、模型标签、价格、上下文、已验证能力，还是人工标签？
- “自动”推理策略在首版应根据哪些明确输入选择档位；何时只给出建议而不自动改变 profile 的固定默认值？
- 是否需要 reviewer worker；若需要，它什么时候创造真实价值？
- 是否需要 benchmark / A-B 模式；它要支持哪一个实际选择？
- 哪些 provider privacy 标签构成硬性禁止，哪些只是路由提醒？
