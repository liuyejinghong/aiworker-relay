# 验证记录：OpenRouter 外部 Worker 可行性

日期：2026-08-25
范围：保留早期产品假设 probe，并记录 v0.1 本地 supervisor / 看板的实现验收。

说明：本记录中的 `external-workers@aiworker-local` 是当时的 alpha Plugin identity；当前公开身份为 `aiworker-relay` / AIworker Relay。历史证据保留原始名称，不能据此声称新安装面已经完成验收。

## 环境与保护措施

- Codex CLI：`0.149.0`
- 测试模型：`stealth/ox-alpha`，经 OpenRouter
- API key 只从本机 `.env` 读取；未打印、未写入仓库，`.env` 已被忽略。
- 每次外部 probe 使用临时、隔离的 `CODEX_HOME` 与临时 `config.toml`，没有改动主 Codex 配置。
- 测试 task 均为只读；没有修改项目文件。

## 结果

| 验证点 | 结果 | 证据与含义 |
| --- | --- | --- |
| Native Luna Medium 路由 | 通过 | 收到精确回执 `NATIVE_LUNA_MEDIUM_PROBE=PASS; 7*8=56`。 |
| Native Luna Max 路由 | 通过 | 收到精确回执 `NATIVE_LUNA_MAX_PROBE=PASS; 9*7=63`。 |
| Codex CLI JSON lifecycle | 通过 | 本机 `codex exec --json` 产生 `thread.started`、`turn.started`、`item.completed`、`turn.completed` 和 token usage。 |
| Codex CLI → OpenRouter → Ox Alpha 单轮 | 通过 | 返回精确回执 `OX_ROUTE_PROBE=PASS`，进程退出码为 0。 |
| `--output-schema` 结构化收敛 | 未通过 | Ox Alpha 返回 `{"error":"No schema was supplied."}`。当前组合不能把 Codex 的 schema 选项当作可靠合同。 |
| 外部模型的只读 shell tool | 部分通过 | 模型实际执行了 `/bin/zsh -lc pwd` 并返回工作目录；工具后的后续生成被 OpenRouter `429 Too Many Requests` 中断，task 没有最终收敛。 |
| 429 受控重试 | 受限 | 一次重试在首轮即再次得到 429。结合该模型的免费 / 预览属性，当前证据更符合 provider / model 的限流；未继续消耗免费模型额度。 |
| OpenRouter 原生 usage / cost | 通过 | 直接调用官方 API 得到 `prompt_tokens=97`、`completion_tokens=16`、`total_tokens=113`、`cost=0`。这证明 provider 层可给真实费用字段。 |
| `codex exec --json` 每 run 费用归因 | 未建立 | CLI JSON 输出有 token，但没有 OpenRouter generation ID 或实际 `cost`，暂不能可靠映射到一次 run。 |
| Ox Alpha 的公共 benchmark 收录 | 未收录 | 2026-08-25 查询的 Artificial Analysis catalog（140 个模型）与 OpenRouter catalog（124 个模型）都没有 `stealth/ox-alpha`。详情页必须显示“暂无公开参考分数”，不能填入示例数字。 |
| Gemini 3.7 Flash 的公共 benchmark 收录 | 已收录 | 精确版本 `google/gemini-3.7-flash-20260813` 在 Artificial Analysis（截至 2026-08-25）记录为 Coding 76.1、Agentic 45.1；OpenRouter 评测（截至 2026-08-23）记录 GPQA Diamond 92.2559%（198 项）、TAU-Bench 航空 80.5556%（48 项）。这些是公开参考，不是本机 Worker 兼容性验证。 |
| Gemini 3.7 Flash 的模型发现 metadata | 通过 | 2026-08-25 查询当前 OpenRouter 模型目录，精确标识为 `google/gemini-3.7-flash`，上下文 1,048,576，支持 `low` / `medium` / `high` 推理档位，模型原始默认是 `medium`。这证明可发现及 metadata 可带入，不证明本机外部 Worker 已兼容。 |
| 温和停止 → 强制终止 | 通过 | 两个受控本机独立进程组中，TERM 使第一个正常退出；忽略 TERM 的第二个进程在 KILL 后以退出码 137 退出。 |

## v0.1 本机实现验收

| 验证点 | 结果 | 证据与边界 |
| --- | --- | --- |
| 聚焦 Python 合同测试 | 通过 | `unittest` 覆盖 package、CLI、loopback API、Key 不回显、Profile 推理规则、冻结拒绝、Task Packet、worktree、TLS context 和 TERM → KILL。 |
| 真实 loopback daemon 与页面资源 | 通过 | 隔离临时数据目录中的 `orch setup --no-open` 返回 `127.0.0.1` endpoint；`daemon.json` 记录 PID、端口和项目根目录。浏览器成功加载多页 Web、SSE、CSS、JavaScript，且无新的 console error。 |
| SSE 初始事件 | 修复后通过 | 真实浏览器发现初始 `overview` 事件触发刷新循环；页面现只处理实际 Profile / run 变更事件，重新加载后循环消失。 |
| daemon 带 SSE 连接的关闭 | 修复后通过 | 真实临时 daemon 在浏览器 SSE 连接存在时收到 TERM 后结束并清除 `daemon.json`；SSE 循环现在观察 shutdown event。 |
| OpenRouter TLS 与目录发现 | 通过 | 当前 Python 默认 `urllib` 因本机 CA 配置失败；改用系统证书库后，公开 `/api/v1/models` 返回 200。隔离页面成功发现 `stealth/ox-alpha`，带入 `max` / `high` / `low`。 |
| 添加 / 冻结 Profile | 通过 | 隔离页面创建 Ox Alpha 的 `unverified` Profile，详情页显示目录快照；冻结后 `POST /api/runs` 返回 `frozen_profile`，未触发 provider 或 run。 |
| 账户 / benchmark 的按需 Web 路径 | 通过 | 聚焦测试覆盖账户总额、普通 Key 限额 fallback、精确模型匹配和两个 loopback endpoint；真实页面在无 Key 状态下仅显示配置提示，确认点击前没有 provider 请求。随后以用户提供的 management Key 通过隔离页面显式刷新，成功显示账户总额；未发起模型调用。 |
| local marketplace 发现 | 通过 | 采用标准 `./plugins/external-workers` source path 后，`codex plugin list` 显示 `external-workers@aiworker-local` 为可安装状态。 |
| 首次 Plugin 安装与应用级 runtime | 通过 | 用户已在 Codex UI 从 `aiworker-local` 安装 Plugin，并完成应用级 venv 与运行依赖安装；本地控制面返回 loopback 地址，随后 status 为 `runtime_ready=true`。当时采用旧版二次确认流程；v0.1.1 的一次 setup 自动 bootstrap 待重新安装复核。 |
| v0.1.1 setup 入口 | 通过（已存在 runtime） | 新 launcher 直接接受 `setup --no-open` 并复用现有控制面，返回 loopback endpoint；首次缺失 runtime 的分支由聚焦单元测试覆盖，仍需干净重装后完成体验验收。 |
| v0.1.6 runtime 版本收敛 | 通过 | 应用数据中的历史 `0.1.0` runtime 经显式 setup 收敛至 `0.1.6`；bundle、`orch version` 与健康 daemon 均报告同一版本。空闲 daemon 在替换前正常退出。 |
| Ox Alpha 真实 write probe | 未通过，保留隔离证据 | run 创建了 detached worktree、隔离 `CODEX_HOME` 与受监管的 Codex CLI 进程，但 provider 在工具调用阶段返回 `400 Server tool request failed`；没有文件变更，因此 Profile 仍为 `unverified`。 |
| 真实 external child 的 dashboard stop | 历史待验收 | 当时 Ox Alpha 在约一秒内失败，无法对仍在运行的真实外部 Codex 子进程完成 TERM / KILL 联动；后续 2026-08-26 NVIDIA 复核已补上真实 dashboard 温和停止证据。 |

## 2026-08-26 发布前复核

以下记录与上方历史 alpha probe 分开：使用当前公开身份 `aiworker-relay`、当前 Codex CLI `0.149.0` 和用户明确指定的 `nvidia/nemotron-3-ultra-550b-a55b:free`。Key 只在本机钥匙串和短生命周期子进程环境中使用，未打印或写入仓库。

| 验证点 | 结果 | 证据与边界 |
| --- | --- | --- |
| Git-backed marketplace 安装与更新 | 通过 | 在干净隔离 `CODEX_HOME` 中添加 `liuyejinghong/aiworker-relay` marketplace、安装 `0.1.6`，并连续升级至 `0.1.7`、`0.1.8`；每次 setup 后 bundle/runtime/daemon 版本一致。该证据不声称未观察到的 Desktop UI 更新行为。 |
| NVIDIA 模型目录与基础 API | 通过 | 精确匹配到 `nvidia/nemotron-3-ultra-550b-a55b:free`，目录快照显示 1,000,000 context、`high` / `medium` reasoning。对同一模型的 OpenRouter Responses、流式 Responses 和函数调用 Responses 均返回 200。 |
| 非交互式 Codex CLI 工具写入 | 通过 | 隔离 worktree 中的同一模型使用 `codex exec --approve-for-me`，返回码 0，产生 `thread.started`、`turn.started`、工具完成和 `turn.completed` 事件，只创建了 `manual-probe.txt`，内容精确为 `PING\n`。这确认 root cause 是非交互式审批路径，而非模型、OpenRouter 或 Responses 流。 |
| 修复前真实 dashboard child 的温和停止 | 通过 | 真实 NVIDIA child 在看板中展示 PID、44 个 RSS 样本和运行态；用户操作温和停止后记录为 `term_exited`、退出码 0，无需 KILL，主仓库无变更。该 run 未产生文件变更。 |
| v0.1.9 runtime 收敛 | 通过 | 显式 setup 后 bundle/runtime/daemon 同为 `0.1.9`，状态为 `up_to_date`。 |
| 修复后 dashboard-managed NVIDIA write | 未通过，provider 限流 | 受控 run 已启动并进入真实 Codex CLI，但约两秒后以 OpenRouter `429 Too Many Requests` 失败；记录保留人类可读错误、退出码 1、空 diff 与 `unavailable` cost，不自动重试或替换模型。 |

因此，`--approve-for-me` 是已验明的最小 runner 修复，429 是当前免费模型的 provider 结果而非成功。NVIDIA Profile 继续保持 `unverified`；在该模型有可用额度时，仍需一次修复后 dashboard-managed 成功 write 才能完成同-run 验收。

## 已验证结论

1. 使用独立 `CODEX_HOME` 的 Codex CLI 可以真实地走 OpenRouter 调用 Ox Alpha，不需要自己实现模型 SDK 或 agent loop。
2. OpenRouter 能直接返回 token 与 `cost`，因此看板的“实际费用”目标有 provider 原始数据来源。
3. 外部进程需要独立进程组，才可以把“温和停止、确认、强制终止”做成可核验的产品行为。
4. 原生 Luna Medium / Luna Max 已在当前环境完成路由 probe；本地看板仍不因此获得其 PID / RSS 或 kill 权限。

## 不能越过的限制

- Ox Alpha 当前页面标明支持 tool calling 和 `response_format`，但不提供 JSON Schema enforcement；本地实测也没有得到可用 schema 约束。因此 result contract 不能依赖该能力。
- Ox Alpha 的免费 / 预览路径在一次工具调用后的后续生成与一次受控重试中都触发 429。现有证据只能说明该 provider / model 当时受到限流，不能据此断言 Codex CLI 多轮 harness 普遍不兼容；单轮路由成功仍不足以证明其适合长任务。
- Codex CLI 的 JSON 输出没有传递 OpenRouter 的实际 cost 或 generation ID。只有在费用归因方案明确后，才可以承诺单 run、日、月的真实费用面板。
- 进程停止已具有看板与 daemon 代码路径，且 OS 进程组语义已测；Ox Alpha 的真实 write probe 启动后即在 provider 工具调用阶段失败，故尚未用可持续运行的真实 Codex CLI 子进程完成完整的看板取消联动验收。

## 对下一轮验收与演进的直接影响

- 将“模型兼容性”拆成单轮、工具调用、多轮收敛、结构化结果、取消五项，而不是只做一次 hello-world。
- first MVP 不应承诺 Ox Alpha 等免费模型可以可靠完成多轮工作；Profile 的验证状态必须继续区分发现与实际 harness 验证。
- 实现费用面板前，先解决 `codex exec` 与 OpenRouter generation 的相关性；在此之前只显示 token 和费用归因状态。
- 进程 supervisor 的最小不变量是：自己创建且持有一个外部 run 的独立进程组，只信任自己采集的退出结果。

## 参考

- [OpenRouter 的 Codex CLI 配置说明](https://openrouter.ai/docs/cookbook/coding-agents/codex-cli)
- [OpenRouter Usage Accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
- [Ox Alpha 当前模型页](https://openrouter.ai/stealth/ox-alpha)
- [truststore 系统证书库说明](https://truststore.readthedocs.io/en/stable/)
