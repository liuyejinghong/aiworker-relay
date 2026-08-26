# Worker Contract v1

这是首个外部 run 的最小合同，不是 JSON Schema，也不是通用 workflow protocol。Task Packet 以固定 Markdown 标题传给 `codex exec`；daemon 生成机器可读的 `run.json` 和 JSONL evidence。模型最终文字会被保留，但不被当成唯一或权威的结构化状态。

它延续 `sol-worker-routing-codex` 的核心前提：Codex 负责目标、授权、选择和验收；worker 只在明确边界内执行并交回证据。

## Task-side information

- **run_id**：控制面生成的不可重复标识。
- **Task**：有限、可完成的目标。
- **Scope**：允许的文件、系统与操作。
- **Do Not Touch**：显式排除项。
- **Existing Behavior**：已观察到的事实、源码或复现。
- **Expected Behavior**：请求达成的结果。
- **Constraints**：技术、安全、依赖、授权与时间限制。
- **Acceptance Criteria**：Codex 将据此验收的条件。
- **Verification**：worker 应收集的聚焦证据。
- **Deliverables**：预期变更、报告或 artifact。

外部 worker 在派发前还必须明确以下选择信息：

- **Worker Kind**：native 或 external。
- **Requested Profile**：用户显式指定时的 profile / model slug。
- **Selection Source**：用户指定或 Codex 建议。
- **Data Boundary**：可否发送给该 external profile 的敏感性约束。
- **Workspace**：worktree 路径、源 `HEAD` 和未包含主工作区 dirty 变更的提示。

## Result-side information

- **status**
- **summary**
- **files_changed**
- **tests_run**
- **test_results**
- **unresolved_issues**
- **assumptions**
- **artifacts**

模型以自由文本最终消息补充上述内容；首版不要求它生成可强制验证的 JSON。

## Runtime evidence beside the result

外部 run 还需要关联下列本地监督证据，但这些不是让模型自行编造的 result 字段：

- profile state at dispatch (`enabled` / `frozen`)
- start / finish time
- PID / process group and RSS samples
- JSONL lifecycle event index
- stop request and observed outcome
- token usage and cost-attribution state

原生 worker 不强行填充上述本地进程字段；它只返回 Codex 可取得的 task evidence。

## Contract rules

- worker 不得推断 task packet 之外的授权。
- 用户显式选择被冻结 profile 时，不得静默换模型或继续派发。
- raw transcript 不是系统状态；最终结果必须收敛为可供 Codex 判断的证据。
- 首版不使用 JSON Schema enforcement。模型或 harness 不支持 schema 时，daemon 仍保存可观测 lifecycle、最后消息和 diff；Codex 根据这些 artifact 判断。
- Codex 决定接受、退回、合并或停止；worker 不拥有最终决定权。
