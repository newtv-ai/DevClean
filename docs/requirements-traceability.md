# Reclaimer 当前需求可追踪矩阵

本矩阵描述用户实际运行的 Windows GUI EXE。旧的 SQLite inventory、G0–G6 gate 和 REPORT_ONLY plan 文件是研发兼容资产，不覆盖也不否定本矩阵。

| 需求 | 当前实现 | 状态 | 测试/限制 |
|---|---|---|---|
| 单文件 Windows GUI，无 Python 前置 | `scripts/build_windows_exe.ps1` + PyInstaller | 已实现 | 构建检查 EXE ≤ 50 MB 并执行 `--smoke` |
| 不创建全盘 SQLite 扫描库 | `ui/app.py` → `scan_roots` → `TriageSession` | 已实现 | GUI 只保留每栏最多 500 条显示记录 |
| 可停止长扫描 | `CancellationToken` + GUI 停止按钮 | 已实现 | 取消仅停止后续扫描/自动清理 |
| 旧用户 TEMP 自动清理 | `triage.py` + `auto_clean.py` | 已实现 | 仅当前 `%TEMP%`、7 天、普通文件；失败不删除 |
| 旧用户崩溃转储自动清理 | `cleanup_catalog.py` + `auto_clean.py` | 已实现 | 仅现有 `%LOCALAPPDATA%\\CrashDumps`、7 天、普通文件；失败不删除 |
| 常见缓存目录册 | `cleanup_catalog.py` | 已实现 | 只扫描存在的固定本地根；浏览器/缩略图和 pip/uv/npm/pnpm/HF/Gradle/Yarn/Ollama/VS Code 默认进入 AI 审查 |
| 删除抗路径替换 | `permanent_delete.py` | 已实现 | 扫描快照 + 最终句柄路径 + 句柄元数据复核；Windows canary 覆盖 |
| 硬保护用户资产 | `triage.py`、`recycle.py` | 已实现 | 开发仓库、凭据、编辑器历史、reparse/Cloud、目录、硬链接拒绝 |
| AI 逐项解释候选缓存 | `ai_review.py` + GUI 粘贴流程 | 已实现 | 50 条批次、严格 JSON、固定 review/item ID；不传文件内容 |
| AI 不确定时用户决定 | GUI USER_REVIEW + Windows Recycle Bin | 已实现 | 最多 32 条、完整路径确认、执行前重验 |
| AI/自动删除轻量历史 | `action_history.py` + GUI 历史页 | 已实现 | 脱敏 JSONL ≤ 1 MiB，自动轮换；不是恢复机制 |
| 浏览器/系统/IDE 等通用分类清理 | 无 GUI 执行器 | 未实现 | 不得以路径猜测替代专用分类器 |
| HF/Conda/pip/uv/npm/pnpm/Docker/Ollama/IDE 语义清理 | 仅保留早期只读 adapter | 未实现 | GUI 未连接这些 adapter；后续需逐工具适配器 |
| 卷、类别空间汇总和目录排行 | `scan_insights.py` + GUI 空间概览 | 已实现 | 显示扫描卷可用/总量；类别完整；目录只聚合扫描根下的前 2,000 个首层桶 |
| 大文件精确重复检测 | `duplicates.py` + GUI 重复文件页 | 已实现 | ≥1 MiB、SHA-256、仅有限最大同尺寸组；每组自动保留一个基准 |
| 空目录、相似媒体、卷仪表盘/treemap | 无 | 未实现 | 后续专项功能，不可用路径猜测代替 |
| 系统维护、注册表、提权、应用卸载 | 无 | 明确非当前范围 | 需另立 ADR、预览、恢复与 Windows 专项验证 |
| BleachBit / Winapp2 执行 | 无 | 明确非当前范围 | 不 fork、不调用 `--clean`，第三方规则不能绕过本机复核 |

## 研发兼容层

`core/state.py`、CLI、报告、Schema 和九个 adapter 仍可用于只读盘点研究；它们不向 GUI 提供删除授权。任何未来复用都必须把结果重新转成 GUI 的精确扫描快照，并经过当前威胁模型规定的保护和复核。

## 不允许的快捷方式

- 不用单纯路径后缀或通配符把任意目录变成“安全垃圾”。
- 不允许 AI 返回路径、命令或未经扫描的项目。
- 不用 SQLite 保存全盘候选后再删除。
- 不通过 UAC、注册表清理、厂商 CLI 或 BleachBit 扩大当前清理范围。
