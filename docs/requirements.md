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
- run 进程退出后先把退出码、停止结果和不可用费用写入 `incomplete` 检查点，再收集证据；只有 `diff.patch`、`files.json`，以及自然成功所需的 `last-message.md` 都可读并持久化时，才显示 `succeeded`。证据、持久化、终端事件或广播失败不得留下活跃状态或伪造成功；若进程在清理后仍存活，必须保留进程句柄作为阻塞事实。
- daemon 重启不重新接管旧进程。对磁盘中的 `starting` / `running` / `stopping` 记录，只有正整数 PID、`psutil` 创建时间和 POSIX 进程组（Windows 进程树）完全匹配时才发送 TERM，超时再发送 KILL；身份缺失、PID 复用、进程不存在或 KILL 后仍存活均记录为用户可见的 `incomplete`，不得向不确定进程发信号。
- 外部 worker 需要单 run、日、月的 usage / cost 视图和预算告警。
- OpenRouter 账户余额与当前 API Key 的限额是独立的 provider 事实：只在用户从 Web 主动刷新时读取，明确区分管理 Key 可读的账户总额、普通 Key 可读的自身限额，以及尚未建立的 per-run 实际费用归因。
- provider privacy / data-retention 差异需要作为 profile 可见信息，而非隐藏配置。
- 每个外部 worker 需要独立详情卡：模型参数、上下文窗口、当前价格、数据保留提示、公共 benchmark 参考与本机兼容性验证。公共跑分必须带来源和更新时间；未收录时明确显示无数据，不得虚构分数。
- 公开 benchmark 只在用户从详情页主动刷新时读取；只展示与 Profile 精确模型标识匹配的 OpenRouter 记录，不把它当成调度资格或本机兼容性证明。
- OpenRouter API key 的生产配置入口在本地 Web UI；仓库与运行日志不得保存 key。
- 添加外部 worker 是模型发现流程：用户粘贴 OpenRouter 模型标识或模型页链接，系统查询当前模型目录、让用户处理歧义匹配，并自动带入当前 metadata；它不是 API key 设置入口。
- 模型目录收录只证明模型当前可发现，不等于外部 harness 已兼容。新 profile 必须把“已识别”与“本机尚未验证”分开；添加时不自动发起会消耗额度的任务。
- 每个外部 worker profile 有用户确认的默认推理策略，只能从该模型实际支持的档位中选择。v0.1 的 CLI/UI 不提供 per-run reasoning 选择；run payload 若包含 `reasoning_effort` key（包括 `null` 或空字符串）必须稳定返回 `reasoning_override_not_supported`，并只使用 Profile 默认值。Profile 值为 `auto` 时记录为 `profile_auto`，固定值记录为 `profile_default`。
- 产品保持本地、轻量、CLI first；Web 看板是运行控制面，不是远程 SaaS。
- 首版跨平台目标是 macOS、Windows 和具备系统密钥服务的桌面 Linux。
- 本地控制面是单个持久 `external-workersd`：Python 3.12+、`asyncio` 与 `aiohttp`；同一进程服务本机 Web、SSE、状态 API 与外部 run 监管。它固定绑定 loopback `127.0.0.1:49178`；macOS 的 setup 将该同一进程注册为用户级 LaunchAgent。一个活跃 daemon 绑定一个项目根目录；第二个项目不得静默复用它。
- 每次 `external-workersd` 启动生成一个随机 capability，写入用户专属且 owner-only 的 `daemon.json`；health/overview、错误、日志、URL 与静态 JavaScript 不返回该值。浏览器只使用 host-only、HttpOnly、SameSite=Strict cookie；CLI/launcher 只使用 `X-AIworker-Capability`，两种模式不混用。CLI/launcher 必须用 capability 和 health 的 PID、端口、项目根、runtime 根、版本及 persistent 状态确认 daemon 身份；旧记录没有 capability 且 PID 仍存活时视为 unknown，不复用、停止、杀进程或覆盖。
- v0.1 信任能够发起任意原始 loopback HTTP 的本地进程。浏览器 cookie 不按端口隔离，因此它只作为 hostile browser origin / CSRF 防护，不声称隔离可读取其他 `127.0.0.1` 端口 cookie 的本地进程；若未来要防该主体，必须另行接受不同的 browser bootstrap 或 IPC 边界。携带 capability 的 CLI/launcher HTTP 请求不得跟随重定向。
- loopback API 严格接受 `127.0.0.1:<实际端口>` Host；浏览器 API/SSE 只接受同源 Origin、Fetch Metadata 的 same-origin/none、非 no-cors 且非 subresource 请求。静态首个顶层文档导航可以领取 cookie，同源静态资源可以加载；API JSON 写入必须声明 `application/json`（可带 charset），shutdown 同样解析 JSON 对象。
- 页面使用静态 HTML、CSS 和原生 JavaScript；状态使用 SSE 推送，用户操作使用 HTTP `POST`，不使用 React、WebSocket、Node 常驻进程或桌面壳。
- 外部 run 用 `asyncio` 管理生命周期，`psutil` 读取 RSS 与在停止时枚举进程树。只有活跃外部 run 每 2 秒采样一次；原生 worker 不采样。
- API Key 使用 `keyring` 写入操作系统密钥服务；密钥服务不可用时拒绝保存，不回退到明文文件。Provider HTTPS 使用操作系统证书库，不关闭 TLS 验证。
- 用户级 Profile 使用原子 JSON；项目级 run 证据使用 `.orch/runs/` 下的 JSONL。真实费用日/月汇总在可靠归因前不生成，不引入数据库。
- 持久 `external-workersd` 在空闲时只维持 loopback listener，不做状态采样、自动 provider 请求或自动派发；账户和公开跑分只接受用户主动刷新。只有活跃 external run 才有 RSS 采样。
- 目标是升级并融合进 `sol-worker-routing-codex`，不是长期维护平行路由项目。
- 源码开发可使用 Codex local marketplace；面向其他开发者的公开 pre-release 使用 Git-backed marketplace。Plugin 包含 `aiworker-relay` Skill，不含 MCP server；2026-08-26 已在干净隔离 `CODEX_HOME` 中实测 `0.1.6 → 0.1.7 → 0.1.8` 更新与 runtime setup 收敛，并在推送后全新安装 `0.1.9`。2026-08-27 又在新的隔离 `CODEX_HOME` 从公开 Git Marketplace 安装 `0.1.16`，其 launcher 的 `setup --no-open` 与同项目、空闲的本机控制面回读 bundle/runtime/daemon 一致。该事实不等同于已发布正式版本，也不替代每种 Codex Desktop UI 更新交互的验收。
- 公开 pre-release 源码仓库是 [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay)。仓库公开不等于任意新 bundle 已自动到达用户本机；用户获得新 bundle 后仍须显式运行 `$aiworker-relay setup`。
- `$aiworker-relay setup` 使用 Python 3.12+ 在用户应用数据目录创建并复用专用 `venv`；明确的 setup 请求授权这一次本机安装，缺失依赖时不再要求第二次对话确认，不依赖或改写全局 Python 包。泛泛的配置请求不得自动 bootstrap。用户应用数据目录分别为 macOS `~/Library/Application Support/Codex External Workers`、Windows `%LOCALAPPDATA%\\Codex External Workers`、Linux `$XDG_DATA_HOME/codex-external-workers`（默认 `~/.local/share/codex-external-workers`）。
- `external-workersd` 只绑定 loopback `127.0.0.1`；`orch setup` 与 `orch dispatch --profile <id> --packet <path>` 是由 Skill 调用的真实本机入口。
- 首发外部 harness 只有隔离的 `codex exec --json --ephemeral --approve-for-me --output-last-message`；自动审批保持在 Codex 的 workspace-write 模式，不使用危险的 approvals/sandbox bypass，也不建设备用 CLI 或模型 SDK 路径。
- 外部 write run 必须从项目 `HEAD` 创建 detached Git worktree，位于 `.orch/worktrees/<run-id>`；不自动 commit、merge 或同步主工作区的未提交改动。
- Task Packet 使用 v1 固定字段表达目标、范围、禁止修改项、已知事实、约束、验收、验证、Profile、推理档位、选择来源、数据边界与 workspace。模型最终文字与 daemon 采集的 lifecycle、diff、退出状态共同构成 Result Evidence，不依赖 JSON Schema enforcement。
- Profile 同时保存 `enabled` / `frozen` 和 `unverified` / `verified`。未验证 Profile 不得被 Codex 建议；只有用户显式选择并确认实验性运行时允许派发。
- 429、模型中断与启动失败只记录并允许用户手动重试；首版没有自动 fallback。
- 停止外部 run 时先 TERM 并观察最多 10 秒，仍存活才允许用户确认 KILL。
- 真实费用归因不阻止外部 run 控制路径。归因未建立时只显示 `pending` / `unavailable`；token 需先有不保存原始 transcript 的可靠来源，实际单 run、日/月费用和预算告警后置。
- LM Studio / MLX 等本地模型接入已明确后置；当前不为它建立执行路径、配置项或兼容层。

## 当前阶段边界

本次已完成：符合 Codex marketplace source 约定的 AIworker Relay Plugin package、`aiworker-relay` Skill、应用级 bootstrap launcher、固定 loopback `external-workersd`、Profile / Key / run API、账户与公开跑分的按需读取、静态多页 Web、隔离 worktree / `codex exec` 路径、JSONL 证据与两阶段停止代码；canonical package `VERSION`、setup-only runtime 收敛、活跃 run 更新延后、持久控制面迁移、更新失败恢复与 dispatch 版本不一致拒绝。

本机真实路径已验证：daemon 启动、页面资源、无 Key 的 Ox Alpha 模型发现、Profile 创建、推理档位带入、冻结与拒绝派发；management Key 的账户总额读取；Git-backed marketplace 的干净安装和两次更新；以及真实用户应用数据的空闲 runtime 收敛。2026-08-26 对 `nvidia/nemotron-3-ultra-550b-a55b:free` 的直接 Responses、流式 Responses 和函数调用均返回 200；2026-08-27 同一模型的 dashboard-managed detached run 实际创建了限定 marker，文件清单仅该路径、`git diff --check` 通过，随后在 PID / PGID 可观测和 RSS 采样期间由控制面 TERM 收敛为 `term_exited`、退出码 0。聚焦测试覆盖 package、daemon、TLS、Profile、Task Packet、worktree、TERM → KILL、runtime 失败恢复、active update defer、错误摘要脱敏及非交互式审批参数。

尚未验收：Profile 从 `unverified` 升为 `verified` 所需的能力矩阵和用户可见晋级操作尚未接受；因此 NVIDIA 的最小 write / stop 证据通过后仍不静默改写其标签。Ox Alpha 的实验性 write probe 仍以 provider `400 Server tool request failed` 结束。实际费用、日/月汇总、预算、自动路由与 fallback 均未实现。

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
