# 更新与发布生命周期

状态：v0.1.16 已实现并在用户应用数据中完成受控 runtime 收敛和持久本机入口验收。macOS LaunchAgent 为其自身提供解析 Codex CLI/Node 所需的最小 `PATH`；空闲 daemon 的受控停启已在同一固定地址完成真实回读。2026-08-27 的全新隔离 `CODEX_HOME` 已从公开 Git Marketplace 安装 `0.1.16`，其 launcher `setup --no-open` 与当前同项目的空闲本机控制面收敛。未单独观察的 Codex Desktop UI 更新交互继续按待验证处理。

## 为什么需要这一项

AIworker Relay 有两个独立但必须保持一致的交付物：

1. Codex Plugin bundle：manifest、Skill、bootstrap launcher、Python 源码与静态看板资源；
2. 用户应用数据目录中的专用 Python runtime：实际运行 `orch` 与 `external-workersd` 的 venv。

2026-08-26 的本机核验已经证明两者会分离：已安装 Plugin 为 `0.1.1`，而应用级 `orch version` 为 `0.1.0`。当时的 launcher 只在 runtime 缺失时执行安装；已有 venv 时会直接复用旧 `orch`。当前 launcher 已改为比较并报告三者版本，只在显式 setup 且 daemon 确认空闲时执行可恢复替换。

这不是单纯的版本显示问题：新 Skill、看板和 runtime 的行为可能不再匹配，用户也无法从当前界面看出这个状态。

## 目标与非目标

### 目标

- 用户先通过 Codex 的正常 Plugin 渠道获得新 bundle，再显式执行一次 `$aiworker-relay setup`；无需接触 pip、venv 或 API Key。
- setup 只在没有活跃 external run 时替换应用级 runtime；绝不为升级停止、重启或改变正在工作的 worker。
- 更新后 bundle、runtime 和 daemon 都能报告同一个 release version；看板与 setup 输出都能说明实际结果。
- Profile、系统钥匙串中的 OpenRouter Key 与项目 `.orch/` 证据在无 schema 变化的更新中原样保留。
- 安装失败或中断后，上一可用 runtime 能被恢复；失败必须可见，而不是悄悄留下半安装环境。

### 非目标

- 不实现独立的 Plugin 商店、后台检查更新器、定时轮询、远程发布服务或数据库。
- 不尝试更新 Codex 本体、OpenRouter 模型、用户的全局 Python 或原生 Codex worker。
- 不承诺 Codex Desktop 已提供某个“更新”按钮；其实际语义必须以一次真实 marketplace 验收为准。
- 不预建通用 migration framework、无限版本保留树或任意历史版本的一键回退。

## 版本事实与单一来源

| 名称 | 当前/未来事实来源 | 用途 |
| --- | --- | --- |
| bundle version | Plugin manifest 所声明的 release version | Codex 安装面识别的新 bundle |
| runtime version | 已安装 `orch version` | launcher 判断实际 venv 是否需要更新 |
| daemon version | `/api/health` 与 `/api/overview` | 确认正在服务看板的代码版本 |
| profile schema version | `profiles.json` 内的版本字段，当前为 `1` | 仅在持久化 Profile 格式改变时决定是否迁移 |

实现使用 Python package 内的单一 `src/orchestrator/VERSION` 文件：

- Python package build metadata 从它派生；
- source-tree 与安装后的 `orch version` 都从该文件派生；
- `plugin.json` 的 `version` 是经测试校验的 release 镜像，不再由人手独立维护。

release 检查必须拒绝 `VERSION`、manifest 与已构建 package 三者不一致的包。此项只消除当前已有的三个手工版本点，不引入通用版本管理框架。

## 正常用户流程

```text
Codex Marketplace / Plugin UI
        │ 安装或获得新 Plugin bundle
        ▼
用户在新 task 执行 $aiworker-relay setup
        │
        ├─ bundle version = runtime version，且 daemon 匹配固定持久入口
        │      └─ 正常打开或复用看板
        │
        └─ bundle version ≠ runtime version
               │
               ├─ 有活跃 external run 或状态无法可靠判断
               │      └─ 不更新、不杀进程；明确报告“更新等待当前任务结束”
               │
               └─ 没有活跃 external run
                      └─ 受控替换 runtime → 启动固定持久 daemon → 健康检查通过 → 打开看板
```

`setup` 是唯一自动执行 runtime 更新的入口，因为它已是用户明确授权的本机安装动作。`dispatch` 不触发隐式升级：若发现 bundle/runtime 不一致，或 daemon 不是固定持久入口，它应提示用户先运行 setup。这样一次可能下载依赖、替换 venv 的操作不会隐藏在一次任务派发中。

如果更新因活跃 run 被延后，用户在任务结束后再次执行 setup。系统不新增后台更新 watcher 来等待或自动重试；持久控制面只提供本机入口，不触发 provider 读取或外部任务。

## 更新状态与判定

| 状态 | 判定 | 可执行动作 |
| --- | --- | --- |
| `up_to_date` | bundle、runtime、健康 daemon（若存在）版本一致，且 daemon 位于固定持久入口 | 正常 setup / dispatch |
| `runtime_missing` | 没有可用专用 venv | setup 创建首次 runtime |
| `update_required` | runtime 缺失、无法执行 version，或版本不同 | setup 进入更新判定 |
| `update_deferred_active_run` | 受健康检查确认的 daemon 有 `starting`、`running` 或 `stopping` external run | 保留原 runtime 与进程；报告需在 run 结束后重新 setup |
| `update_blocked_unknown_daemon` | daemon PID 存在，但 health / overview 与记录不一致或无法确认活跃状态 | 不替换 runtime；报告恢复阻塞，避免误杀未知进程 |
| 更新失败后已恢复 | 新 runtime 未通过安装、version 或 health 检查，旧 runtime 已恢复 | setup 明确报告恢复结果与失败原因 |

“活跃”只指由 `external-workersd` 实际拥有的 external run。原生 Codex worker 不属于本机进程监管边界，因此不会被本更新流程停止或作为 update blocker。

## 最小且可恢复的 runtime 替换

Python venv 内的入口脚本通常包含绝对路径，不能把一个已验证的临时 venv 直接改名为 `venv`。因此不采用看似原子的“创建 `venv.next` 后重命名”方案。

当前版本使用一条有界的恢复路径：

1. launcher 从新 bundle 的 package `VERSION` 与现有 `orch version` 读取目标和现状；不依赖全局 Python 依赖。
2. 若有 daemon，先以 `daemon.json`、`/api/health` 和 `/api/overview` 交叉确认它就是本产品的 daemon，且没有活跃 external run。
3. 只在该判定成立时，请求 idle daemon 正常退出；首个兼容旧 runtime 的升级桥接仅可终止 health 返回 PID 与记录一致的 idle daemon。macOS 的 LaunchAgent 将这视为一次正常退出，直到新 runtime 就绪后才重新启动同一 daemon。
4. 将旧 `venv` 暂存为唯一的 `venv.previous`，在原路径创建新的 `venv` 并从当前 Plugin bundle 安装 runtime。
5. 依次检查新 `orch version`、固定 `127.0.0.1:49178` daemon health、持久状态和版本一致性。全部通过才删除 `venv.previous`。
6. 任一步失败时，清理未完成的新 venv，并将 `venv.previous` 放回原路径；Profile、Key 与项目运行证据不写入、不迁移。

如果进程在第 4 至第 5 步之间被中断，下一次 setup 发现 `venv` 缺失且 `venv.previous` 存在时先恢复旧 runtime，再报告或重新尝试更新。该临时备份只解决一次更新事务的失败恢复，不形成长期多版本管理系统。

为在 macOS、Windows 与 Linux 上一致地替换 idle daemon，当前 runtime 提供一个仅供 launcher 使用的窄本地“正常退出”控制动作。它不是通用管理 API，也不作用于活跃 run。首个从旧 runtime 升级的 bridge 无法假设旧 daemon 已支持该动作，因而只能在前述三项事实均匹配、且 overview 明确无活跃 run 时处理其精确 PID。

## 持久数据与迁移

本次更新包不改变 Profile schema：`profiles.json` 保持版本 `1`，Keyring 条目不移动，`daemon.json` 只作为可重建的运行记录。

未来只有某个真实需求改变 Profile 持久格式时才增加一段从已知旧版本到新版本的显式迁移。那一段迁移必须：

1. 在写入前确认输入 schema version；未知或未来版本失败并保留原文件；
2. 先保存一份同目录、可恢复的原始 JSON；
3. 使用现有原子 JSON 写入；
4. 在 release 验收中覆盖升级与失败恢复。

不为尚未出现的 schema 建立 migration registry、数据库或通用转换层。

## 分发策略

### 源码开发

local marketplace 只用于源码开发、私有测试，不应被描述为普通开发者的安装或更新渠道。OpenAI 官方文档明确说明：`marketplace upgrade` 的文档语义是刷新 Git marketplace snapshot，而不是保证已安装 Plugin 自动升级。[OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)

Git marketplace 的安装/更新验收必须真实操作一次 Codex Desktop：确认新 manifest 是否出现、已安装 Plugin 如何切换到新版本、随后 setup 是否把 runtime 收敛到相同版本。没有观察到的 UI 能力不写入用户说明。

### 面向正常开发者的渠道（推荐）

在 local alpha 验收后，使用同一仓库或专门 marketplace 仓库的 Git-backed marketplace。官方格式可将 Plugin 子目录作为 `git-subdir` source，并以受控的 Git ref 提供更新来源。[OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)

推荐产品动作保持两步：

1. 用户在 Codex 中刷新 marketplace 并接受可用的 Plugin 更新；
2. 用户下次需要本地控制面时执行 `$aiworker-relay setup`，它完成应用级 runtime 收敛。

公开 pre-release 源码已位于 [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay)。2026-08-26 的干净隔离验收已实际执行上述安装路径，并从 `0.1.6` 连续升级至 `0.1.7`、`0.1.8`；推送后又从同一 Git marketplace 全新安装 `0.1.9`。2026-08-27 在新的隔离 `CODEX_HOME` 安装 `0.1.16` 后，其 launcher 的 `setup --no-open` 回读 bundle/runtime/daemon 版本一致、固定持久 endpoint 为空闲。此处复用了同项目的现有用户级控制面；因为 macOS 的 LaunchAgent 标签唯一，不宣称已在同一登录用户下创建第二个隔离 daemon。它验证的是 CLI marketplace 路径，不把未单独观察的 Codex Desktop 更新交互写成既成事实。本提案不添加自研 updater 来绕开 Codex 的安装面。

## 实施包与文件责任

### P0 — 已在本地实现，待随公开 bundle 发布

| 所有权 | 最小变更 |
| --- | --- |
| `plugins/aiworker-relay/src/orchestrator/VERSION`、`pyproject.toml`、`src/orchestrator/__init__.py`、`.codex-plugin/plugin.json` | 建立一份 canonical release version，并在构建/测试时验证 manifest 与 runtime 一致。 |
| `scripts/launch_external_workers.py` | 读取 bundle/runtime 版本；setup 执行更新判定、活跃 run 保护和有界恢复。dispatch 在 mismatch 时明确拒绝并引导 setup。 |
| `src/orchestrator/cli.py`、`daemon.py` | 提供仅用于替换 idle daemon 的窄内部控制路径，并报告 daemon version。 |
| `src/orchestrator/config.py` | 只增加本次 venv 恢复所需的路径与原子记录；不改变 Profile 格式。 |
| `tests/` | 覆盖版本不一致、active defer、idle replace、失败恢复与数据不变。 |
| `docs/` | 更新用户流程、发布说明与故障处理口径。 |

### P1 — 真实分发验收

已完成的窄证据：干净 `CODEX_HOME` 已成功添加 marketplace、安装 `0.1.6` bundle、两次执行 marketplace upgrade，并经 setup 把应用级 runtime 收敛至对应 bundle。该验收没有借助自定义安装器，也没有读取或复制用户 Key。

1. 在干净 Codex 用户状态安装旧 bundle，保存测试 Profile 与 Key 状态；
2. 通过真实 marketplace 路径获得新 bundle；
3. 执行一次 `$aiworker-relay setup`；
4. 验证 bundle/runtime/daemon 三个版本一致，Profile 与 Key 仍存在；
5. 启动一个受控 external run 后重复更新动作，确认不会停止它且显示 deferred；run 结束后再次 setup，确认更新完成；
6. 注入一次安装失败，确认旧 runtime 可继续启动且没有改写数据。

若 Codex Desktop 的 Git marketplace 行为与预期不同，先记录实际行为并调整用户流程；不以自定义安装器掩盖该差异。

### 以后才处理的事项

- Profile schema 真正变化时的显式迁移；
- 发布渠道的公开目录提交；
- 具有业务价值的 release notes / 兼容性提示；
- 多项目控制面的产品决策；
- 本地模型或第二 provider 的运行时。

## 最小验收标准

下一开发包完成的判定不是“增加了 update 命令”，而是以下事实同时成立：

1. 同一 bundle 的 manifest、`orch version` 与健康 daemon 版本一致；
2. 当前已观察到的 `0.1.1` bundle / `0.1.0` runtime 漂移可通过一次 setup 收敛；
3. 一个活跃 external run 不会被更新流程停止、重启或迁移；
4. 更新失败后上一 runtime 可重新 setup，Profile 与 Key 不丢失；
5. 用户只需使用 Codex 的 Plugin 安装面和已有 `$aiworker-relay setup`，不需要 pip、手动 venv 操作或复制 Key；
6. 对实际 Codex marketplace 更新行为有一次端到端观察记录，而非依据 CLI 名称推断；当前已具备 CLI 观察记录，Desktop 特有交互另行记录。

## 仍需用户确认的产品选择

已确认 runtime 收敛绑定到显式 setup，Git-backed marketplace 是面向其他开发者的渠道。仍待后续产品选择：

- major breaking change 是否要求用户显式确认后才更新，而 patch/minor 可沿用 setup 的既有本机安装授权；
- 看板是否只显示当前版本与失败原因，还是确有需要保留面向用户的 release history。
