# 产品定义：Codex 外部 Worker Plugin

状态：v0.1.9 本地控制面、应用级 runtime 收敛与空闲 daemon 的受控更新已完成本机验收；Git-backed Marketplace 的干净 CLI 安装与两次更新也已实测。相同 NVIDIA 免费模型完成了隔离的真实 Codex CLI 工具写入，真实 dashboard child 也成功温和停止。修复后的 managed write 最近被 provider `429` 限流中断，因此仍不能把“同一 managed run 的成功写入和看板闭环”标为已验收；Ox Alpha 也仍不能标为 verified。

## 一句话

这是给个人开发者的 Codex skill 升级：Codex 继续负责判断，按需把明确、可验证的工作派给通过 OpenRouter 接入的外部模型，并在本地看板上看清这些外部 worker 是否可用、正在做什么、是否还占着进程，以及账户可用额度与 run 费用归因各自处于什么状态。

它不是一个新的 agent 平台，也不重建任何模型的 agent loop。

## 产品形态

正式发行单元是 **AIworker Relay** Codex Plugin，而不是独立 Web 应用或裸命令行工具。Plugin 的核心能力是 `aiworker-relay` Skill；Skill 是 Codex 内的调用入口，本地 Web 看板则是它打开的配套控制面。

alpha 包通过 `aiworker-relay` marketplace 分发，入口位于仓库的 `plugins/aiworker-relay/`。用户应从 Codex 的插件市场安装它；仓库目录和终端命令只承担开发、打包与验收责任。

目标使用路径如下：

```text
在 Codex 安装 AIworker Relay Plugin
  → Codex 识别 aiworker-relay Skill
  → 在一个新 task 中调用 $aiworker-relay setup
  → 打开本机 Web 控制面，在其中配置 OpenRouter API Key 与 worker profile
  → 像平时一样向 Codex 提任务
```

`aiworker-relay` 可以被用户显式调用，也可以在任务与其能力匹配时被 Codex 采用；这只使 Codex 获得受控派发能力。任何可能产生外部用量的派发，仍遵从“用户明确指定优先；未指定时 Codex 建议、用户确认”的策略，不能成为看不见的全局拦截。

Plugin 安装不接管 Codex：不得静默改写主 Codex 的默认模型、provider、原生 worker profile、系统提示、hooks 或项目 `AGENTS.md`。外部 run 才使用隔离配置；原生 Luna worker 继续遵从 Codex 的原生控制边界。

`$aiworker-relay setup` 打开本机 Web 控制面并呈现启动结果；若本机运行时尚未存在，这个明确的 setup 请求会在同一次操作中创建专用环境并完成安装，不再要求第二次对话确认。API Key 与 worker 模型 Profile 不在 Codex 对话、Skill 参数或普通 CLI 配置中录入。

本机控制面由一个按需 `external-workersd` 提供。它在浏览器客户端或外部 run 存在时运行，二者都不存在 60 秒后退出；页面使用事件推送观察运行态，不做空闲轮询。它不是常驻服务，也不是独立桌面应用。

“按需启动”不要求用户双击或常驻运行任何软件。首次调用 `$aiworker-relay setup` 时，Skill 启动或复用它并打开浏览器；之后每次由 Skill 进行受控外部派发时，若它尚未运行，Skill 同样自动启动它，若正在运行则直接复用。开发者只使用 Codex 和浏览器中的看板。

## 用户要解决的问题

个人开发者并非每个任务都需要最强、最慢或最贵的 Codex 子代理。某些探索、检索、实现或复核任务可以交给速度更快、上下文更大、价格更低的外部模型；但这不能牺牲主控权、可见性和可停止性。

用户需要同时得到两件事：

1. Codex 保持任务边界、选择建议和最终验收，不让外部模型自行扩大范围。
2. 外部 worker 像一个可管理的本地资源，而不是一条看不见、可能卡住的终端命令。

## 产品边界

### 要做

- 以 Codex / Sol 为唯一的判断与验收 authority。
- 以 AIworker Relay Plugin + `aiworker-relay` Skill 作为正式产品入口；首次配置从 Codex 中启动，而非要求用户先打开一个独立 Web 产品。
- 用户可以显式指定 worker profile；显式选择优先于 Codex 的建议。
- 外部模型统一走 OpenRouter，首选执行 harness 是隔离配置下的 Codex CLI。
- 用同一套 task packet、范围限制和验收证据，服务原生 Luna worker 与外部 worker。
- 本地 Web 看板管理 OpenRouter 连接与外部 worker profile。
- 用户可按需读取 OpenRouter 账户余额或当前 Key 的配置限额；二者都不被当作某次 run 的实际费用。
- 看板区分原生 worker 与外部 worker，且不虚构原生 worker 的进程数据。
- 对外部 worker 展示 profile 启用状态、run 状态、PID / 进程组、RSS、费用状态和停止结果；token 只在不依赖原始 transcript 的可靠来源建立后展示。
- 支持将外部 profile 冻结；冻结后拒绝新派发，并明确返回“该模型已冻结”。
- 对运行中的外部 worker 先做温和停止，再按用户动作强制终止，并报告实际结果。

### 明确不做

- 多家 provider 的直接适配层、模型 SDK 或自研 agent loop。
- 自动模型路由作为首个实现目标。
- Web UI 之外的远程控制面、账号体系、多人协作、数据库、Redis、队列、MCP server、A2A protocol 或 workflow engine。
- 对原生 Codex 子代理伪造 PID、RSS、费用或可杀进程能力。
- 通过“预估费用”伪装为 OpenRouter 实际扣费。
- 用 hooks 或安装器静默重写 Codex 全局配置，并把它当作外部 worker 的核心路由机制。

## 核心概念

| 概念 | 含义 | 责任边界 |
| --- | --- | --- |
| Codex / Sol | 目标、任务拆分、worker 建议与最终验收 | 始终由 Codex 持有 |
| 原生 worker | Codex 官方机制派生的 Luna Medium / Luna Max 等子代理 | Codex 管理运行态；本地看板仅展示已配置事实 |
| 外部 worker profile | OpenRouter 模型及其用户确认的默认推理策略，例如 `stealth/ox-alpha` + `max` | 本地控制面负责启用、冻结与派发前检查 |
| 外部 run | 一次具体任务的受监管进程 | 本地控制面负责 PID、RSS、日志索引与停止 |
| 冻结 | 禁止某个 profile 接收新的 task，不等于取消已经运行的 task | 独立于 run 生命周期 |
| 费用状态 | 对一次 run 的实际花费是否已归因 | 必须区分 `已确认`、`待归因` 和 `不可得` |

## 关键交互

### 派发

1. Codex 先形成有范围、验收条件和证据要求的 task packet。
2. 用户若明确选模型或推理档位，系统只检查 profile 是否可用并遵从该选择；不静默替换模型或降低用户指定档位。
3. profile 为冻结时，拒绝派发并报告冻结原因。
4. 未指定推理档位时，固定策略 profile 使用其用户确认的默认档位；只有用户选择“自动”策略的 profile 才允许 Codex 选择档位。
5. 未明确选模型时，Codex 可建议一个 profile，但建议不等于自动授权。
6. 外部 run 必须有独立配置与独立进程组，避免污染主 Codex 上下文和运行环境。

### 添加外部 Worker

1. 用户粘贴 OpenRouter 模型标识或模型页链接。
2. 看板查询当前 OpenRouter 模型目录；唯一匹配时带入模型资料，歧义匹配时要求用户选择具体模型。
3. 用户查看上下文、价格、能力、数据处理提示与该模型实际支持的推理档位，再选择 profile 的默认推理策略。
4. 用户显式选择“添加并启用”或“添加为已冻结”。模型目录发现不触发付费任务，也不等于本机 harness 已验证。

### 观测与费用

- 外部 run 的真实来源是 `external-workersd`：JSONL 生命周期、PID / 进程组、RSS 和退出结果。
- 原生 worker 的真实来源是 Codex：看板只显示已配置的 worker 类型和“由 Codex 控制”。
- OpenRouter API 的响应可返回 token 与 `cost`。但当前 Codex CLI 的 JSON 输出没有把 OpenRouter 的 generation/cost 直接带出来，因此“每 run 实际费用如何可靠归因”是独立的 accounting 里程碑，不阻止先实现外部 run 控制路径。
- 在归因建立前，看板显示 `pending` / `unavailable` 费用状态，不显示 `$0`、模型标价或伪造估算。token 也需建立不依赖原始 transcript 的有界来源；单 run、当日、当月实际费用与预算告警只在有可信归因后启用。
- 用量页的账户总余额只在用户主动刷新时读取，且需要 OpenRouter management Key；普通 Key 若 provider 返回其配置限额，则单独标为“当前 Key 限额”。
- Worker 详情页的公开跑分也只在用户主动刷新时读取，并只保留与 Profile 精确模型标识匹配的记录、来源与时间；未收录时显示无公开参考。

### 停止

外部 run 的停止顺序固定为：

1. 发送温和停止信号给该 run 的整个进程组。
2. 观察是否已经退出，并把结果写入 run 记录。
3. 仍存活时，允许用户显式发起强制终止。
4. 报告 `已温和退出`、`已强制终止` 或 `未能确认`，不只显示一个按钮点击成功。

## 隐私与配置

API key 的最终录入和更新入口在本机 Web 设置页。页面不得回显完整 key；仓库不得保存 key。`.env` 只用于本次连通性验证，并已被 `.gitignore` 忽略。

每个外部 profile 应展示 provider 当前可返回的数据保留标签。目录没有可验证字段时，看板明确显示“目录未提供”，而不是沿用历史探测或自行补全；因此“免费”不能成为把敏感代码默认交给某个模型的理由。该类标签应随用户主动查询目录时的 metadata 更新，而不是写死在代码里。

## v0.1 实现与验收状态

下列本地能力已进入实现，并以聚焦测试或隔离 loopback 路径验证：

- loopback Web、模型目录发现、Profile 创建 / 冻结、冻结拒绝派发、可选推理档位、原生 Luna 声明卡、账户信息和公开跑分的按需读取。
- Task Packet、隔离 `CODEX_HOME`、detached worktree、`codex exec --approve-for-me` harness、JSONL 证据和 TERM → 确认 KILL 的代码路径。
- Keyring 保存与验证的实现入口，但不会在测试中读取、回显或代填用户 Key。

仍未完成的产品验收是：以用户明确允许且当前有可用额度的模型，经修复后的控制面成功派发一个真实 write run，并在同一个 run 中从看板观察结果与停止生命周期。Git-backed Marketplace CLI 安装与更新已完成实测；实际费用归因继续后置，本地模型接入也不在当前实现范围。

## 与 `sol-worker-routing-codex` 的关系

建议作为其升级，而不是新建一套平行路由项目：保留已有的 Sol 主控、bounded packet、route receipt 与 native Luna 语义；将 `aiworker-relay` Skill 纳入同一 Plugin，并新增“外部 worker profile + `external-workersd` + Web control plane”这一条外部执行支路。

当前仓库已经承载这个升级方向的 v0.1 实现；未对 `sol-worker-routing-codex` 原项目执行 Git 合并或迁移。它仍是下一阶段需要显式选择的分发 / 演进工作。

## 首次使用体验

本地控制面不应要求新用户先理解 Profile、Key 或外部 run 的工程状态。已完成的全新用户走查及下一次改进顺序见 [首次使用体验优化](product-optimization.md)。该文档不会改变“添加模型是发现流程”“Codex 保留派发与验收权”的既定产品边界。
