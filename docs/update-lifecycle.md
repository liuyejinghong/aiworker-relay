# 更新与发布生命周期

状态：v0.1.19 的 reviewed Plugin source 为 `89d8d564c80cd4de59d7908a6a7114f4c2d03f54`；release-catalog 候选已用 exact `sha` 指向它，并通过隔离 Codex CLI 安装。v0.1.18 仍是真实用户环境中已安装并完成 NVIDIA write / TERM 验收的 pre-release。v0.1.19 把 source fingerprint 纳入 bundle/runtime/daemon 收敛，并为 macOS arm64/x86_64、标准 CPython 3.12/3.13/3.14 固定 hash-checked build/runtime set。GitHub ruleset `21734294` 已对 `main` active 生效；当前仍待 Codex Desktop 更新/回滚行为验收，尚未打 tag 或创建 GitHub Release。

## 为什么需要这一项

AIworker Relay 有两个独立但必须保持一致的交付物：

1. Codex Plugin bundle：manifest、Skill、bootstrap launcher、Python 源码与静态看板资源；
2. 用户应用数据目录中的专用 Python runtime：实际运行 `orch` 与 `external-workersd` 的 venv。

2026-08-26 的本机核验已经证明两者会分离：已安装 Plugin 为 `0.1.1`，而应用级 `orch version` 为 `0.1.0`。当时的 launcher 只在 runtime 缺失时执行安装；已有 venv 时会直接复用旧 `orch`。后续 version 收敛修复了正常 bump 路径，但不能识别同 version / 不同 source。v0.1.19 同时比较并报告 human version 与 source fingerprint，只在显式 setup 且 daemon 确认空闲时执行可恢复替换。

这不是单纯的版本显示问题：新 Skill、看板和 runtime 的行为可能不再匹配，用户也无法从当前界面看出这个状态。

## 目标与非目标

### 目标

- 用户先通过 Codex 的正常 Plugin 渠道获得新 bundle，再显式执行一次 `$aiworker-relay setup`；无需接触 pip、venv 或 API Key。
- setup 只在没有活跃 external run 时替换应用级 runtime；绝不为升级停止、重启或改变正在工作的 worker。
- 更新后 bundle、runtime 和 daemon 都能报告同一个 release version 与 source fingerprint；看板与 setup 输出都能说明实际结果。
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
| bundle source fingerprint | launcher 对唯一 Plugin source 的实际分发文件计算 | 检测同 version / 不同 source；与稳定 release 的 Git SHA 证据绑定 |
| runtime source fingerprint | venv 根目录的 `.aiworker-release.json` | 记录该 runtime 实际从哪个 bundle 内容安装 |
| runtime dependency identity | 同一 `.aiworker-release.json` 中的 lock 名称、lock SHA-256、Python 版本与 package set | 证明本次 setup 实际接受的完整构建与运行依赖集合 |
| daemon source fingerprint | `daemon.json`、`/api/health` 与 `/api/overview` | 确认当前进程与 installed runtime 身份一致 |
| profile schema version | `profiles.json` 内的版本字段，当前为 `1` | 仅在持久化 Profile 格式改变时决定是否迁移 |

实现使用 Python package 内的单一 `src/orchestrator/VERSION` 文件：

- Python package build metadata 从它派生；
- source-tree 与安装后的 `orch version` 都从该文件派生；
- `plugin.json` 的 `version` 是经测试校验的 release 镜像，不再由人手独立维护。

release 检查必须拒绝 `VERSION`、manifest 与已构建 package 三者不一致的包。source fingerprint 覆盖 manifest、Skill、launcher、runtime source 与静态 UI，排除 Python bytecode、build 目录和 egg metadata 等 setup 可再生文件。它直接参与更新判定，不单独生成仓库 checksum 文件。

## 正常用户流程

```text
Codex Marketplace / Plugin UI
        │ 安装或获得新 Plugin bundle
        ▼
用户在新 task 执行 $aiworker-relay setup
        │
        ├─ bundle version + fingerprint = runtime，且 daemon 匹配固定持久入口
        │      └─ 正常打开或复用看板
        │
        └─ version 或 fingerprint 不一致 / identity 缺失
               │
               ├─ 有活跃 external run 或状态无法可靠判断
               │      └─ 不更新、不杀进程；明确报告“更新等待当前任务结束”
               │
               └─ 没有活跃 external run
                      └─ 受控替换 runtime → 启动固定持久 daemon → 恢复旧 run 记录 → 健康检查通过 → 打开看板
```

`setup` 是唯一自动执行 runtime 更新的入口，因为它已是用户明确授权的本机安装动作。`dispatch` 不触发隐式升级：若发现 bundle/runtime 不一致，或 daemon 不是固定持久入口，它应提示用户先运行 setup。这样一次可能下载依赖、替换 venv 的操作不会隐藏在一次任务派发中。

如果更新因活跃 run 被延后，用户在任务结束后再次执行 setup。系统不新增后台更新 watcher 来等待或自动重试；持久控制面只提供本机入口，不触发 provider 读取或外部任务。

## 更新状态与判定

| 状态 | 判定 | 可执行动作 |
| --- | --- | --- |
| `up_to_date` | bundle、runtime、健康 daemon（若存在）的 version 与 fingerprint 一致，且 daemon 位于固定持久入口 | 正常 setup / dispatch |
| `runtime_missing` | 没有可用专用 venv | setup 创建首次 runtime |
| `update_required` | runtime 缺失、无法读取 version/fingerprint、任一身份不同，或存在待完成的 `venv.previous` 事务 marker | setup 进入更新判定 |
| `update_deferred_active_run` | 受健康检查确认的 daemon 有 `starting`、`running` 或 `stopping` external run | 保留原 runtime 与进程；报告需在 run 结束后重新 setup |
| `update_blocked_unknown_daemon` | daemon PID 存在，但 health / overview 与记录不一致或无法确认活跃状态 | 不替换 runtime；报告恢复阻塞，避免误杀未知进程 |
| 更新失败后已恢复 | 新 runtime 未通过安装、version 或 health 检查，旧 runtime 已恢复 | setup 明确报告恢复结果与失败原因 |

启动恢复只处理证据记录，不重新接管旧的 `ManagedProcess`。`starting`、`running` 或 `stopping` 记录只有在 PID、`psutil` 创建时间和 POSIX 进程组（Windows 进程树）都精确匹配时才允许 TERM；超时后再次确认身份才允许 KILL。身份缺失、PID 复用、进程已退出或仍存活都留下 `incomplete` 与恢复原因；若本次 daemon 关闭时 survivor 仍在运行，则保留 daemon 身份和阻塞记录，避免把旧记录永久当作已安全退出。

“活跃”只指由 `external-workersd` 实际拥有的 external run。原生 Codex worker 不属于本机进程监管边界，因此不会被本更新流程停止或作为 update blocker。

## 最小且可恢复的 runtime 替换

Python venv 内的入口脚本通常包含绝对路径，不能把一个已验证的临时 venv 直接改名为 `venv`。因此不采用看似原子的“创建 `venv.next` 后重命名”方案。

当前版本使用一条有界的恢复路径：

1. launcher 从新 bundle 读取 package `VERSION` 并计算 source fingerprint，从现有 `orch version` 与 venv release identity 读取现状；不依赖全局 Python 依赖。
2. 若有 daemon，先以 `daemon.json`、`/api/health` 和 `/api/overview` 交叉确认它就是本产品的 daemon，且没有活跃 external run。
3. 只在该判定成立时，使用记录中的 capability 请求 idle daemon 正常退出；不再为缺少该控制动作的旧 daemon 直接发送信号。macOS 的 LaunchAgent 将受控退出视为一次正常退出，直到新 runtime 就绪后才重新启动同一 daemon；旧记录没有 capability 时保持 unknown 并阻塞更新。
4. 将旧 `venv` 暂存为唯一的 `venv.previous`，在原路径创建新的 `venv`。launcher 按当前 Python minor 选择随 bundle 分发的 lock，只从显式官方 PyPI index 安装带 SHA-256 的 accepted wheels；venv seed pip 只负责把 pip、setuptools、直接与传递依赖收敛到精确集合。随后以固定 setuptools 和 `--no-build-isolation --no-deps` 安装当前 Plugin source；`pip check`、完整 package-set readback 和版本检查通过后，写入 version-bound source/dependency identity。任一步失败都立即恢复旧 runtime，成功后仍保留 marker。若此前已停止 verified-idle daemon，也要用恢复后的旧 runtime 重启并校验持久控制面。
5. 由持久 entry、daemon 启动和权威最终校验共同接受 candidate。最终校验必须确认预期 bundle version、source fingerprint、固定 `127.0.0.1:49178` endpoint、`persistent`、以及当前项目 `project_root`；全部通过后才删除 `venv.previous`。
6. 任一 post-install 步骤失败时，只有已验证 idle 的 daemon 才能通过 capability-gated shutdown 停止；随后恢复旧 runtime、用旧 runtime 重启持久控制面并校验旧 version、endpoint、persistent 和 `project_root`。若 daemon active 或 unknown，则不停止、不删除任一目录，明确报告 deferred / blocked。

如果进程在第 4 至第 5 步之间被中断，下一次 setup 按 `venv.previous` marker 做确定性恢复：只有 `venv.previous` 时直接恢复；两目录中 candidate 已完整接受且 idle 时提交删除 marker，active 时延后清理；未接受的 candidate 在 active 时延后、unknown 时阻塞、idle 时受控停止后恢复旧 runtime，missing/stale 时直接恢复。该临时备份只解决一次更新事务的失败恢复，不形成长期多版本管理系统。

为在 macOS、Windows 与 Linux 上一致地替换 idle daemon，当前 runtime 提供一个仅供 launcher 使用的窄本地“正常退出”控制动作。它不是通用管理 API，也不作用于活跃 run。launcher 只对 capability 与 health/overview 身份均匹配的 idle daemon 调用该动作；控制动作缺失、认证失败或身份不完整都会保持更新阻塞，不会退回到直接处理 PID。macOS 仅在同一次 setup 已完成该受控 idle shutdown 时，才允许卸载随后处于 loaded 但无 daemon record 的 owned LaunchAgent；其他 missing/stale record 与 loaded entry 组合一律视为未知并阻塞，不猜测 active run 状态。

## 依赖更新与安全修复

依赖不会在用户 setup 时自动漂移，也不由后台任务静默升级。上游安全公告、兼容性缺陷或明确的维护需要只会触发一个普通受审查源码变更：同一 PR 必须同步更新 `pyproject.toml` 的直接/build pin、三个 Python-minor lock 的完整传递集合与官方 PyPI wheel hashes，并说明被替换版本的原因。不能只改范围或只补一个平台文件。

该 PR 先通过受支持的六个 Python/架构组合 fresh install、package-set readback 与完整测试，再接受独立 exact-head review。通过后仍只是新的 source candidate；更新 stable SHA、tag 或 Release 是另外的发布动作。若新依赖集合在 setup 或后续权威校验中失败，既有 `venv.previous` 事务恢复上一 runtime，用户 Profile、Keychain 与 `.orch/` 不参与依赖回滚。这里不增加自动漏洞扫描服务、远程 installer 或第二个 package manager；发现具体公告时再以其实际受影响范围决定优先级和版本变更。

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

Git marketplace 的安装/更新验收必须真实操作一次 Codex Desktop：确认新 manifest 是否出现、已安装 Plugin 如何切换到新版本、随后 setup 是否把 runtime 收敛到相同 version 与 fingerprint。没有观察到的 UI 能力不写入用户说明。

### 面向正常开发者的渠道（推荐）

在 local alpha 验收后，使用同一仓库或专门 marketplace 仓库的 Git-backed marketplace。官方格式可将 Plugin 子目录作为 `git-subdir` source，并使用 `ref` 或 `sha` selector。[OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)

`main` 作为唯一 release-producing catalog branch，但不直接充当 Plugin source selector。Plugin 源码先由普通 PR 合并成一个不可变 commit；只有在该 commit 的受支持矩阵、安装证据与独立审查通过后，单独的 catalog PR 才把 `sha` 前移到它。这样 catalog 可以正常演进，而任一已发布 Plugin 的 bytes 不会因后续 `main` 推送漂移。发布证据同时记录 Git SHA、安装后 fingerprint 与依赖锁身份；Git provenance 与本机 bytes 收敛各自承担一个必要事实。

在 catalog PR 合并前，`main` 必须启用 active ruleset：所有变更经 PR；六个 `tests (macos-14|macos-15-intel, 3.12|3.13|3.14)` check 严格通过且 branch 与 base 同步；禁止 branch deletion 与 force-push。个人仓库不增加无法由作者自身满足的一人审批要求，也不配置可直接绕过这些条件的例外。tag 与 GitHub Release 仍是另外的发布动作，不是 source immutability 的替代品。

推荐产品动作保持两步：

1. 用户在 Codex 中刷新 marketplace catalog，并在安装面接受该 catalog 暴露的 exact-SHA Plugin；
2. 用户下次需要本地控制面时执行 `$aiworker-relay setup`，它完成应用级 runtime 收敛。

公开 pre-release 源码已位于 [liuyejinghong/aiworker-relay](https://github.com/liuyejinghong/aiworker-relay)。2026-08-26 的干净隔离验收已实际执行上述安装路径，并从 `0.1.6` 连续升级至 `0.1.7`、`0.1.8`；推送后又从同一 Git marketplace 全新安装 `0.1.9`。2026-08-27 在新的隔离 `CODEX_HOME` 安装 `0.1.16` 后，其 launcher 的 `setup --no-open` 回读 bundle/runtime/daemon 版本一致、固定持久 endpoint 为空闲。此处复用了同项目的现有用户级控制面；因为 macOS 的 LaunchAgent 标签唯一，不宣称已在同一登录用户下创建第二个隔离 daemon。它验证的是 CLI marketplace 路径，不把未单独观察的 Codex Desktop 更新交互写成既成事实。本提案不添加自研 updater 来绕开 Codex 的安装面。

## 实施包与文件责任

### P0 — 已在本地实现，待随公开 bundle 发布

| 所有权 | 最小变更 |
| --- | --- |
| `plugins/aiworker-relay/src/orchestrator/VERSION`、`pyproject.toml`、`src/orchestrator/__init__.py`、`.codex-plugin/plugin.json` | 建立一份 canonical release version，并在构建/测试时验证 manifest 与 runtime 一致。 |
| `scripts/launch_external_workers.py`、`locks/` | 读取 bundle/runtime 版本；按受支持 target 安装 hash-locked build/runtime set；回读 package set；执行更新判定、活跃 run 保护和有界恢复。dispatch 在 mismatch 时明确拒绝并引导 setup。 |
| `src/orchestrator/cli.py`、`daemon.py` | 提供仅用于替换 idle daemon 的窄内部控制路径，并报告 daemon version、source fingerprint 与 dependency identity。 |
| `src/orchestrator/config.py` | 只增加本次 venv 恢复所需的路径与原子记录；不改变 Profile 格式。 |
| `tests/` | 覆盖版本不一致、active defer、idle replace、失败恢复与数据不变。 |
| `docs/` | 更新用户流程、发布说明与故障处理口径。 |

### P1 — 真实分发验收

已完成的历史窄证据：干净 `CODEX_HOME` 已成功添加 marketplace、安装 `0.1.6` bundle、两次执行 marketplace upgrade，并经 setup 把应用级 runtime 收敛至对应 bundle。当前 exact-SHA v0.1.19 candidate 也已在干净 `CODEX_HOME` 完成隔离 CLI 安装与版本/指纹回读；该验收未借助自定义安装器，也未读取或复制用户 Key。真实 Desktop 更新与回滚验收仍待后续明确授权。

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

1. 同一 bundle 的 manifest、`orch version` 与健康 daemon version + source fingerprint 一致；
2. 当前已观察到的 `0.1.1` bundle / `0.1.0` runtime 漂移可通过一次 setup 收敛；
3. 一个活跃 external run 不会被更新流程停止、重启或迁移；
4. 更新失败后上一 runtime 可重新 setup，Profile 与 Key 不丢失；
5. 用户只需使用 Codex 的 Plugin 安装面和已有 `$aiworker-relay setup`，不需要 pip、手动 venv 操作或复制 Key；
6. 对实际 Codex marketplace 更新行为有一次端到端观察记录，而非依据 CLI 名称推断；当前已具备 CLI 观察记录，Desktop 特有交互另行记录。
7. 同一受审查 bundle 在 macOS arm64/x86_64 的 Python 3.12、3.13、3.14 上只接受 lock 中的 wheel hashes；fresh install 的 identity 回读 lock、Python 与完整 package set，依赖更新必须形成新的受审查源码 diff。
8. catalog 只通过 exact `sha` 选择与当前 release version 相同、Plugin bytes 无差异的 reviewed commit；main ruleset 要求 PR、六路 CI、禁止删除和 force-push。

## 仍需用户确认的产品选择

已确认 runtime 收敛绑定到显式 setup，Git-backed marketplace 是面向其他开发者的渠道。仍待后续产品选择：

- major breaking change 是否要求用户显式确认后才更新，而 patch/minor 可沿用 setup 的既有本机安装授权；
- 看板是否只显示当前版本与失败原因，还是确有需要保留面向用户的 release history。
