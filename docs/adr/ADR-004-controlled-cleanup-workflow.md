# ADR-004：受控清理闭环与 AI 建议导入

- 状态：Accepted
- 日期：2026-07-16
- 决策所有者：DevClean 产品所有者
- 影响版本：下一可执行版本（不得沿用 inventory-only 的发布声明）
- 修订：2026-07-16 移除 Windows Shell 回收站桥接；所有不可恢复路径改为“先精确隔离、后确认清除”

## 背景

DevClean 的产品目标不是只生成磁盘占用报告，而是完成一条用户可实际使用、可审计的清理链路：扫描与分类、选择确定可清理项、把不确定项交给 AI 复核、导入建议、由用户最终确认、执行删除并验证结果。

此前的只读里程碑及时阻断了“扫描过程中自动永久删除”和“模型回复直接触发删除”等危险实现，但如果只读状态被当作产品终态，工具无法解决用户释放磁盘空间的核心需求。产品所有者现决定恢复删除能力，同时把扫描、AI 建议、用户授权和执行器严格分层。

本 ADR 是对产品范围的主动变更，不代表扫描结果或 AI 文本获得了文件系统权限。

## 与既有决策的关系

本 ADR 作出以下替代：

1. 替代《DevClean 可落地实施方案 v2》中“AI 完全退出 v0.x”“通用扫描结果永远不能进入任何删除流程”以及把 inventory-only 视为终态的产品范围决定。
2. 替代 ADR-002 中 AI 审查批次只能停留在临时解释层、不得进入持久复核流程的限制；也替代任何“模型返回 `DELETE` 后可直接触发本机删除”的旧语义。
3. 允许导入一种专门的、闭合的 AI 建议响应；普通扫描报告、Markdown、任意 JSON、路径列表和命令文本仍不可导入为执行计划。

以下边界继续有效，且不因本 ADR 放宽：

- 扫描、分类、导出和导入阶段对被扫描对象零副作用；
- AI 无执行权，不能提供路径、命令、通配符、action ID 或扩大候选集合；
- 所有动作默认未选择，模型建议也不自动选择；
- 用户必须在本机查看最终作用域并明确确认；
- 执行前必须复验候选快照、文件身份、能力范围和计划有效期；
- 执行意图必须先持久化到 SQLite，未知结果不得自动重放；
- 解析失败、身份变化、范围扩大、增量状态失效或权限异常时，允许的动作只会减少。

ADR-002 中关于最小披露、严格 JSON、候选 ID 闭包和模型不是安全证明的原则继续有效；其具体工作流由本 ADR 取代。

## 决策

### 1. 完整产品链路

DevClean 采用以下闭环：

```text
只读扫描
→ 生成并提交不可变 inventory generation
→ 分类与人工选择
→ 确定项进入本机计划草案
→ 不确定项导出 AI 复核包
→ 导入 AI 建议
→ 用户显式采用部分或全部建议
→ 展示最终计划并再次确认
→ preflight
→ 持久化执行意图
→ 可恢复私有隔离，或先隔离后确认清除
→ reconcile / verify
→ 发布新的 inventory generation
→ 导出非执行性审计报告
```

扫描未完成、已取消、覆盖不完整或结果已失效时，不得创建或批准执行计划。扫描期间不得删除、移动、回收、运行维护命令或根据中间结果改变任何候选文件。

### 2. 分类、文件 capability 与最终动作模式相互独立

展示分类不能兼作删除授权。每个候选必须分别具有分析队列和本地 capability；最终动作模式又必须由用户在确认页单独选择。

分析队列：

```text
DETERMINISTIC
AI_REVIEW
MANUAL_ONLY
PROTECTED
```

执行策略：

```text
PERMANENT_APPROVED_CACHE
EXACT_VENDOR
PREVIEWED_VENDOR
POLICY_VENDOR
RECYCLE_ONLY
NONE
```

含义如下：

| 分析队列 | 含义 |
|---|---|
| `DETERMINISTIC` | 版本化本机规则和证据足以形成清理建议；仍默认未选 |
| `AI_REVIEW` | 本机无法确定，允许导出脱敏元数据获得模型建议 |
| `MANUAL_ONLY` | 只展示给用户，不把模型判断当作有意义的依据 |
| `PROTECTED` | 硬保护对象，不产生执行动作 |

| 执行策略 | 含义 |
|---|---|
| `PERMANENT_APPROVED_CACHE` | 命中版本化批准缓存根与保留期的确定性低风险文件，可用于低风险批量选择；不允许绕过隔离直接永久删除 |
| `EXACT_VENDOR` | 通过厂商稳定对象 ID 和固定能力执行 |
| `PREVIEWED_VENDOR` | 执行前重跑预览，规范化 digest 一致后执行 |
| `POLICY_VENDOR` | 无精确预览的整体或策略 GC，使用专用高风险确认页 |
| `RECYCLE_ONLY` | Alpha 内部兼容名：普通精确文件具备私有隔离资格；不调用 Windows 回收站。稳定版应改名以消除误解 |
| `NONE` | 只报告，任何来源的建议都不能改变它 |

`AI_REVIEW` 不等于文件可执行或确认清除；AI 只影响建议列。最终可用 capability 由本机版本化规则决定，模型不能改变。

用户可见动作模式只有：

```text
QUARANTINE
CONFIRMED_PURGE
```

| 动作模式 | 含义 |
|---|---|
| `QUARANTINE` | 把精确对象按句柄移动到同卷私有隔离区并停留；可显式恢复，立即释放空间为 0 |
| `CONFIRMED_PURGE` | 在更强口令确认后，先执行同一精确隔离，再持久化不可逆意图并从隔离区按句柄处置 |

Alpha 内部把 `QUARANTINE` 记为 `CleanupMode.RECYCLE`，该名称只是迁移兼容，不连接 Windows Shell 回收站。`PERMANENT` 与 `CONFIRMED_PURGE` 均已固定为“先隔离、持久化 `PURGE_PENDING`、再处置”，不存在从原路径直接永久删除的产品分支。

### 3. 删除范围

第一版闭环至少支持：

1. 当前用户 TEMP 和 CrashDumps 中达到保留期的普通文件使用 `PERMANENT_APPROVED_CACHE` 作为确定性低风险/便捷选择证据；它不再代表直接永久删除。
2. 本地 capability 判定为可执行的普通扫描文件，经人工明确选择后默认使用 `QUARANTINE`；AI 复核是可选解释路径，不是人工选择的强制前置门。
3. 用户若确实需要释放空间，可对同一批已可执行候选独立选择 `CONFIRMED_PURGE`。AI 导入和“采用建议”都不能自动选择该模式。
4. 私有隔离中的字节不得计作已释放的主机物理空间；确认清除也只能报告逻辑释放上限，不能把它等同于物理 allocated bytes。
5. 厂商语义命令仍需显式 capability 逐步接入，不以任意命令代替当前精确文件链路。未知版本、未知输出或不受支持的环境降级为只报告。
6. 目录不作为 AI 或清除目标。目录选择必须在计划创建时展开成有上限的精确文件清单；计划后出现的新文件不属于动作范围，非空目录保留。
7. reparse point、Cloud Files 占位文件、身份不完整对象、未经证明的硬链接、系统/提权范围和硬保护资产不得进入文件执行链路。

扩大可清理范围必须通过版本化 capability catalog 新增批准根或厂商动作，不得通过把候选文件的父目录临时当作“批准根”实现。

### 4. 候选 ID、AI 包与本机快照绑定

界面序号和导出数组下标不能作为稳定候选 ID。用户选择候选时，本机私有状态库创建随机 `candidate_ref`，至少绑定：

```text
scope_id
inventory_generation_id
normalized_path
inventory_payload_sha256
volume_serial / file_id / file_id_kind
link_count / logical_size
creation_time_ns / last_write_time_ns
capability_id / approved_root_id
```

AI 复核包使用独立的随机 `packet_id` 和 `review_item_id`。当前 Alpha 在 GUI 内存中保存 `review_item_id → 当前扫描项` 映射；应用重启即失效，不能导入执行。未来若持久化，必须使用受保护状态库、过期时间和完整性绑定。导入响应只能引用当前包中已有的 `review_item_id`，不能携带或覆盖本机路径。

AI 响应采用严格、闭合、有界的 JSON 契约，只允许：

```text
packet_id
packet_digest
review_item_id
decision = RECOMMEND_RECYCLE | KEEP | UNSURE
explanation
```

未知、重复、遗漏或过期 ID，错误 digest，额外字段，路径、命令、通配符、action ID，非标准 JSON 常量或越界文本一律拒绝。导入只持久化建议，不修改选择集、不生成已批准计划，也不调用执行器。

导出默认使用缓存根别名、相对路径、扩展名、大小、年龄、分类与本机证据。披露完整路径必须由用户单独开启，并显示隐私提示。文件内容、凭据和 API Key 不进入复核包。

### 5. 用户确认与内部计划

导入完成后，界面展示 AI 建议与本机 capability。用户可以逐项选择，也可以显式点击“采用全部 AI 建议隔离项”；该点击是新的用户操作，不能由导入过程代替。采用建议只改变候选选择，不选择 `CONFIRMED_PURGE`；不可恢复模式必须在最终页由用户再次独立选择。

当前 Alpha 的执行计划只存在于本进程密封对象中；只有最终批准后的低层动作意图进入私有 SQLite。一次计划最多 256 个精确文件/1 TiB，并自动拆成每批最多 32 个文件/256 GiB 的持久意图批次。未来若持久化未执行计划，至少绑定：

```text
plan_id / selection_digest
engine_build_id / schema_version / OS boot identity
scope_id / inventory_generation_id
candidate_ref / inventory_payload_sha256
capability_id / capability_digest
vendor executable path / hash / version（如适用）
preview digest（如适用）
monitor session token / confirmed sequence（如可用）
approved_at / expires_at
```

所有动作默认未选。用户最终确认页展示完整计划路径、大小、摘要、执行方式、风险、恢复能力和不可逆说明。`QUARANTINE` 与 `CONFIRMED_PURGE` 使用不同确认；确认清除口令包含文件数、逻辑字节数和随机码。当前计划/口令 TTL 为 10 分钟；重启、引擎变化、能力变化、厂商工具变化、preview 变化或相关文件系统变化使计划失效。

### 6. 执行与持久意图日志

当前 GUI 的扫描与同会话增量状态只保存在内存中，不创建全盘 inventory
数据库。唯一的产品 SQLite 是小型执行安全日志；它使用单写者配置：

```text
journal_mode=DELETE
synchronous=FULL
BEGIN IMMEDIATE
```

新日志启用 `auto_vacuum=FULL` 并最多保留最近 128 个 `COMPLETED`
批次。裁剪只作用于已完成历史；`ACTIVE`、`NEEDS_REVIEW`、隔离待恢复和
任何不确定动作都不受容量规则影响。这样既不把全盘文件清单写入数据库，
也不会为了省空间丢失恢复所需状态。

计划状态机：

```text
DRAFT
→ REVIEWED
→ APPROVED
→ PREFLIGHTED
→ RUNNING
→ COMPLETED | PARTIAL | FAILED | STALE | CANCELLED
```

每个动作或目标遵循：

```text
PLANNED
→ PREFLIGHTED
→ INTENT_DURABLE
→ EXECUTING
→ QUARANTINED
→（仅确认清除）PURGE_PENDING → PURGED
→ VERIFIED | FAILED | INDETERMINATE | RESTORED
```

只有 `INTENT_DURABLE` 事务提交后才能产生首次移动副作用。`CONFIRMED_PURGE` 在对象已精确隔离并验证身份后，还必须先持久化 `PURGE_PENDING`，才能对隔离对象执行不可逆处置。`INTENT_DURABLE`、`EXECUTING`、`PURGE_PENDING` 或 `INDETERMINATE` 在恢复时不得自动重放。路径消失不能单独证明是 DevClean 清除；身份变化必须标为 stale 并跳过。

直接文件执行器必须在句柄上复验本地固定卷、批准根、最终路径、128-bit file ID、大小、时间戳、普通文件、非 reparse、非 Cloud placeholder 和硬链接约束，并在最后再次执行版本化硬保护 deny-list。目标文件句柄不共享 WRITE/DELETE；目录句柄为允许子项重命名而共享 WRITE，但不共享 DELETE，因此目录对象本身保持固定。隔离批次目录必须作为批准根的随机直属子目录以最终私有 DACL 原子创建，预存在路径不接管；隔离文件保留原 DACL。任何失败都只跳过目标，不得回退到 `os.remove`、`unlink`、`rmtree`、Windows Shell 回收站或宽范围命令。所有不可恢复处置的输入路径必须位于 DevClean 私有隔离区且仍是原扫描对象。

### 7. 增量扫描与计划失效

文件系统变化通知是失效提示，不是删除授权，也不能在丢事件时作为完整性证明。

同一应用会话内的增量刷新遵循：

1. 在全量基线开始前启动目录变化监视器。
2. 基线扫描完成后，以 sequence fence 排空扫描期间的变化，再提交完整 generation。
3. 后续变化按父目录合并，使用 targeted generation 重新观察受影响子树；删除和重命名至少重扫父目录。
4. targeted 扫描期间出现新事件时继续收敛；达到轮次、路径或容量上限时回退全量 reconciliation。
5. buffer overflow、sequence gap/regression、session token 不匹配、root identity 变化、监视句柄丢失、解析错误或应用重启均使增量检查点失效并回退全量。
6. 在尚未证明可以安全跳过目录前，产品不得把 shadow change monitor 宣传为已完成的 Git 式增量扫描。

执行计划与增量状态联动：

- 变化命中直接文件目标、其祖先或批准根时，相应目标或动作变为 `STALE`；厂商整体策略范围内任一变化使整个策略动作失效。
- 监视器全局失效、变化范围无法归属或根身份变化时，作用域内所有未执行计划失效。
- 最终确认记录 `confirmed_sequence`；执行前必须再次排空事件并完成 preflight。
- 每个执行动作产生的变化仍需核对。出现计划外变化时停止剩余动作，不得把所有同期事件都当作“本工具产生”。
- 执行结束后发布新的 targeted 或 full generation；旧 generation 仅供审计读取。
- 只有路径、file ID 和 payload digest 全部未变的候选才能在刷新后保留人工标记。受影响的 AI 包必须重新导出和复核。

跨进程快速复核在没有可靠持久变化源时仍需遍历并复用旧 payload；界面必须诚实标注。未来 USN 或签名只读 broker 需要独立 ADR 和权限审计。

### 8. UI 状态与控件

主流程采用五步状态：

```text
1 扫描与分类
2 人工选择
3 AI 复核
4 最终确认
5 执行与验证
```

扫描时，选择、AI 导出/导入、计划、确认和执行控件全部禁用。扫描完成后默认零选择。界面至少提供：

- 全量扫描、同会话增量刷新、停止；
- 显式“选择低风险可清理项”，但不得默认勾选；
- 导出 AI 复核包、导入 AI 建议、显式采用建议；
- 生成计划、最终确认并执行；
- 查看逐项结果、查看/恢复私有隔离、导出审计报告；
- “基线有效”“检测到变化”“计划已失效”“需要全量扫描”等状态提示。

后台任务必须携带 scan/session token；旧任务的完成事件不得覆盖较新的扫描会话或恢复已失效的计划。

## 被拒绝的方案

- 扫描过程中自动删除或边扫描边清理。
- 把 AI 的 `RECOMMEND_RECYCLE` 当作本机授权、自动勾选或确认清除选择。
- 从导入文件读取路径、命令、glob、action ID 或批准根。
- 对任意扫描目录提供无身份复核的不可恢复清除，或从原路径直接永久删除。
- 把候选的父目录临时提升为批准根。
- 删除成功后才写 JSONL 历史，或崩溃后盲目重试。
- 增量监视器丢事件时继续执行旧计划。
- 调用 Windows Shell 回收站并假设它一定可恢复；把进入私有隔离的逻辑字节报告为已经释放的物理空间。

## 最小发布门

带删除能力的构建只有同时满足以下条件才可交付；否则必须继续标为 inventory-only：

1. 至少打通 `QUARANTINE` 与 `CONFIRMED_PURGE` 的完整闭环，包括计划、分模式最终确认、隔离、恢复/隔离后清除、reconcile、verify 和审计；不存在原路径直接永久处置。
2. 自动化测试证明扫描、分类、AI 导出和 AI 导入对全部扫描目标零修改；复现 2026-07-12 扫描期误删场景时所有金丝雀内容、身份和时间戳不变。
3. AI 契约测试覆盖陌生/重复/遗漏 ID、错误 digest、额外字段、路径/命令注入、越界 JSON、过期和 stale 包；所有失败均为零执行动作。
4. 文件系统测试覆盖 identity replacement、rename race、junction/reparse、Cloud Files、hardlink、锁、ACL、只读、新文件插入、批准根变化和保护资产；批准根外零写入。
5. 在每个意图日志状态边界注入进程终止，恢复后不自动重放；部分成功和无法判定结果进入 `PARTIAL` 或 `INDETERMINATE`。
6. 增量结果与独立全量扫描 oracle 在随机变化序列中一致；overflow、sequence gap、root change 和 monitor loss 均回退全量并使旧计划失效。
7. 所有动作默认未选；UI 自动化证明导入 AI 建议不会选择或执行，扫描期间执行控件不可用。
8. Windows 11 非管理员真机与 disposable VM 验证通过；网络、移动卷、未知文件系统和需提权范围不产生通用执行动作。
9. 发布静态检查限制删除 API 只出现在 allowlist 执行模块；scanner、classifier、review importer 和扫描 worker 不得依赖执行模块，也不存在通用删除 fallback。
10. 审计文档明确已实现范围、仍为 report-only 的类别、私有隔离的零释放口径、不可恢复动作和所有未通过门槛，不得用“AI 认证安全”或“可重下等于可恢复”措辞。

## 后果

- DevClean 从只读盘点工具升级为具有受控删除能力的清理产品，版本、发布说明和安全声明必须同步变化。
- AI 可以减少人工逐项解释成本，但不会成为执行主体；最终权限始终来自本机 capability、有效快照和用户确认。
- 为保证崩溃恢复、增量失效和 TOCTOU 防护，实现成本明显增加，删除能力必须按 capability 分批开放。
- 普通文件进入私有隔离后明确没有释放空间；产品需要同时展示“已从原位置移除且可恢复”和“已不可恢复清除并具有逻辑释放上限”两种事实。
- 未满足最小发布门时，代码中的实验性执行器不得出现在面向用户的可执行发行物中。
