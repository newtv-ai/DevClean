# 实施状态：DevClean `0.2.0a1`

## 状态结论

当前工作树正在收口“扫描、分类、人工选择、AI 导出/导入、显式采纳、最终确认、可恢复私有隔离/确认清除、执行后核验”的代码闭环。产品不再是 inventory-only，也不允许扫描时清理。

本文件描述代码能力，不替代发布证据。当前版本仍为 Pre-Alpha；本次工作树必须在完整测试、Windows 真机 canary、wheel/EXE 构建、哈希与安全复核全部完成后，才能形成具体发布结论。

## 已在代码中实现

### 1. 只读扫描、分类与展示

- `scanner/filesystem.py` 流式遍历本地文件系统，不下穿 reparse/Cloud Files 边界，并支持取消与进度。
- `core/triage.py` 以来源域、具体类别、分析队列、风险、证据、恢复能力和独立执行策略分类；分类本身从不选择或执行。
- GUI 展示五个队列：确定性候选、厂商管理候选、AI/人工复核、仅报告、受保护。每队列保留最大 500 个可见大项，并保留完整汇总。
- 大文件重复检测是独立只读页：先按大小分组，再对有界候选计算 SHA-256，不自动加入清理计划。
- 初次扫描与任意刷新完成后均默认零选择；扫描期间选择、AI、确认和执行控件禁用。

### 2. 同会话增量扫描

- `scanner/change_monitor.py` 使用有界 `ReadDirectoryChangesW` 通知。
- `scanner/incremental_session.py` 在全量基线前启动监视器，提交前收敛扫描期间的事件；之后按变化父目录/子树重扫并复用未变化记录。
- 完整快照以不可变 tuple 原子发布；取消或失败的轮次不会暴露部分结果。
- 缓冲区溢出、序列/令牌异常、监视器丢失、根身份变化、失效子树/收敛轮次超限等都回退全量扫描，GUI 显示 `FULL`、`INCREMENTAL` 或 `FALLBACK`。
- 快速增量当前仅限同一进程/同一监视会话；未实现跨重启 USN Journal 增量。

### 3. AI 惰性建议契约

- `core/ai_review_contract.py` 为当前扫描中用户标记的最多 100 个项生成随机包 ID、nonce、随机候选 ID与快照摘要绑定。
- 导出只包含有界、脱敏元数据，不包含文件内容、命令、API Key 或本地执行 capability。
- 导入严格拒绝未知/重复/遗漏 ID、错误摘要、额外字段、路径/命令/通配符、非标准 JSON、超深/超大响应和过期批次。
- 模型词汇固定为 `KEEP`、`RECOMMEND_RECYCLE`、`UNSURE`；导入对象的 `execution_authority` 固定为 `NONE`。`RECOMMEND_RECYCLE` 的用户语义是“建议私有隔离”，不对应 Windows 回收站。
- 导入不会修改清理选择。用户必须另行点击“采纳 AI 建议”，再在本地查看最终作用域并确认。

### 4. 受控文件执行

- `core/postscan_cleanup.py` 只接受完成扫描中的密封候选。可执行候选默认只进入同卷私有隔离；用户可在最终页另行选择不可恢复清除，但 AI 建议不能选择或升级该模式。
- 一次用户计划最多 256 个文件/1 TiB，自动拆成每批最多 32 个文件/256 GiB 的独立 SQLite 意图批次；最终页一次展示完整计划并使用 10 分钟口令绑定计划摘要、模式、文件数、逻辑字节数和随机码。
- 执行前再次验证原始扫描根、批准根、固定本地卷、普通文件、非 reparse/Cloud、单硬链接、卷序列、128 位 file ID、大小、时间戳和最终句柄路径。
- 执行器保留独立硬拒绝清单，覆盖仓库/VCS、Codex/Claude、编辑器状态、用户资产目录、同步目录、凭据/密钥/证书/密码库、系统关键目录和 DevClean 自身状态。
- `platform/windows/exact_cleanup.py` 只使用句柄绑定的重命名/处置；目标文件句柄不共享 WRITE/DELETE，没有通用路径删除或递归目录 fallback。
- 隔离目录是批准根的随机直属子目录，以最终私有 DACL 原子创建；预存在路径拒绝接管。可恢复模式不调用 Windows Shell 回收站、不改写文件原 DACL、不释放空间；GUI 提供启动对账、隔离项查看和不覆盖的显式恢复。
- 确认清除固定执行“精确隔离 -> 核验同一身份 -> 持久化 `PURGE_PENDING` -> 从隔离区精确处置”。当前用户 Temp/CrashDumps 的确定性规则只用于便捷选择/风险展示，不能绕过隔离步骤直接永久删除。
- `PERMANENT` 与 `CONFIRMED_PURGE` 都固定经过隔离和 `PURGE_PENDING`；源码已不存在从原路径直接永久处置的产品分支。

### 5. 持久意图、停止与核验

- `core/cleanup_journal.py` 在首次副作用前持久化完整批次意图，使用 `journal_mode=DELETE`、`synchronous=FULL` 和 `BEGIN IMMEDIATE`；它不是扫描索引，只保留最近 128 个已完成批次，未决/可恢复/不确定动作永不因容量规则被裁剪。
- 首个动作失败后停止后续执行；后续意图经再次观察后结算为 `FAILED_UNCHANGED` 或 `UNKNOWN`，不会被自动重放。
- `reconcile_unfinished_actions` 仅观察源位置/隔离位置的精确身份，输出 `QUARANTINED`、`FAILED_UNCHANGED` 或 `UNKNOWN`，不执行删除。
- UI 显示逐项状态，并在执行后触发增量核验；若增量会话不可用则全量刷新。
- 私有隔离模式不宣称释放空间；确认清除只累计日志中已验证 `PURGED` 文件的逻辑字节，不宣称实测卷空闲空间。

### 6. 发布边界检查

- wheel 静态验证器拒绝已撤销的旧执行模块、扫描/分类/AI 层导入执行能力、非 allowlist 的原始 Win32 删除符号、通用 `os.remove`/`unlink`/`rmtree` 和 CLI 删除命令。
- GUI `_scan_worker` 被单独检查，不得调用执行原语。
- CLI 仍只提供 `scan`、`report`、`plan`，不暴露 `clean`/`delete`/`execute` 等命令；带写能力的用户流程仅在 GUI。
- wheel/SBOM 构建保持锁定依赖、清洁运行时安装、逐字节可复现、RECORD/许可证/校验清单验证。

## 当前明确未实现

- 跨进程持久增量（USN Journal）与跨重启选择恢复；
- pip、uv、npm、pnpm、Conda、Hugging Face、Docker、Ollama、VS Code/JetBrains 等厂商 CLI/API 的可执行 capability；
- Windows Update、WinSxS、`Windows.old`、注册表、服务、卸载和管理员 broker；
- Windows Shell 回收站桥接、目录递归清理、空目录删除和 VHDX 压缩；
- 完整操作历史浏览器、隔离保留策略和单项恢复筛选；
- 内置 Codex/Claude API、凭据存储、自动上传和模型来源证明；
- 代码签名、安装包、自动更新与已批准的公开发布渠道。

## 验证入口

主要自动化覆盖位于：

- `tests/test_controlled_cleanup_closed_loop.py`：扫描零副作用、默认零选择、AI 导入惰性、显式采纳、意图先行、停止/不重放；
- `tests/test_ai_review_contract.py`：不可信 JSON 与批次绑定；
- `tests/test_postscan_cleanup.py`、`tests/test_cleanup_journal.py`：候选、确认、执行和崩溃状态；
- `tests/test_incremental_session.py` 与 `tests/fs_integration/test_windows_incremental_session.py`：增量/全量 oracle 和 Windows 变化序列；
- `tests/test_exact_cleanup.py` 与 `tests/fs_integration/test_windows_exact_cleanup.py`：精确隔离、恢复、隔离后处置 canary；
- `tests/test_release_engineering.py`：构建与静态权限边界。

最终结果、制品大小与 SHA-256 应写入单独的交接审计文档；不能用历史测试数或旧构建哈希代替当前工作树证据。
