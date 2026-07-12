# 实施状态：Windows GUI 清理产品

## 已实现

- Windows 11 单文件 `Reclaimer.exe`：PyInstaller `--onefile --windowed` 构建，不要求用户安装 Python、uv 或虚拟环境。
- 原生 GUI：选择扫描目录、扫描常见缓存、取消长扫描、空间概览、重复文件组、四栏分类展示、AI 审查包复制/粘贴、用户回收站确认和操作历史。
- 流式文件扫描：不遍历 reparse/Cloud Files 边界，不读取文件内容，不创建 GUI 扫描数据库；每栏只保留最大的 500 条显示项。
- 常见根目录目录册：当前用户 TEMP、CrashDumps、浏览器 Cache/Code Cache/GPUCache、缩略图缓存、pip、uv、npm、pnpm、Hugging Face、Gradle、Yarn、Ollama 和 VS Code 的既有本地缓存目录。
- 自动永久清理：只作用于当前用户 `%TEMP%` 与 CrashDumps 内超过 7 天的普通文件，并进行固定卷、身份、时间戳、单硬链接、最终路径和句柄级复核。
- AI 审查：每批最多 50 个已扫描的开发缓存文件；路径脱敏、严格 JSON、固定 `review_id`、全量精确 `item_id` 回应。AI 不能创建候选、命令或路径。
- AI 确认删除：仅精确的模型批准项可删除，且删除前再次以句柄复核文件；`UNSURE` 或任何复核失败都进入用户决定。
- 用户决定：最多 32 个精确扫描文件，完整路径确认后移入 Windows 回收站，执行前两次快照重验。
- 空间概览：显示所扫描驱动器的可用/总空间；类别统计完整，扫描根目录的首层目录占用以最多 2,000 个桶聚合并显示 Top 100。
- 重复文件：无 SQLite 的两阶段大文件检测，先按大小、再以 SHA-256 精确确认；每组保留一个基准，其余副本仅进入用户决定。
- 操作历史：最多 1 MiB 的脱敏 JSONL，记录自动永久、AI 永久和用户回收站动作；自动轮换，不保存文件内容。
- 保护规则：拒绝开发仓库、凭据、编辑器历史、重解析点、云占位符、目录与硬链接文件。

## 未实现

当前版本不是通用系统清理器，尚无：

- 浏览器、缩略图、WinGet、Windows Update、`Windows.old` 等分类清理器；
- Hugging Face、Conda、pip、uv、npm/pnpm、Docker、Ollama、VS Code/JetBrains 的工具语义执行适配器；
- 卷仪表盘、treemap、相似媒体、空目录扫描；
- 自动/AI 永久删除的可恢复策略或按规则回滚；
- 内建 Codex/Claude API 连接、API Key 存储或模型来源证明；
- 提权、注册表修改、应用卸载、服务控制、BleachBit 执行、系统组件维护或运行时规则下载。

## 旧 inventory 子系统

仓库仍包含 SQLite、CLI 报告和九个只读适配器。这些是早期盘点/审计子系统，用于开发兼容性与后续专用适配器研究；GUI 不调用 `StateStore`，也不以它作为删除授权。

旧的 G0–G6 证据、release 工程与只读 plan 文件同样保留为历史研发资产。它们不能把 GUI 的永久删除描述成不存在，也不能为未实现的系统清理或厂商执行授权。

## 当前验证

本机 Windows 集成测试覆盖：旧 TEMP canary 的句柄删除、扫描后变更拒绝、AI 批准的 `pip` cache canary 删除、reparse 边界与回收站前置复核。完整 Python 测试、Ruff、Mypy 和打包 EXE smoke 必须在每次产品变更后重新通过。
