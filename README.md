# DevClean

DevClean 是面向 Windows 11 的本地磁盘清理工作台，重点覆盖 AI、开发工具、应用缓存与通用大文件。当前版本为 `0.2.0a1`（Pre-Alpha）：代码已经打通受控清理闭环，但在本次工作树的测试、真机与构建证据全部重新生成前，不应视为正式发布版本。

## 产品闭环

```text
只读扫描 -> 分类 -> 用户选择 ->（可选）导出 AI 复核
         -> 导入惰性建议 -> 用户显式采纳 -> 最终确认
         -> 精确身份复核 -> 先写持久意图 -> 私有隔离/确认清除 -> 核验刷新
```

关键边界：

- 扫描、分类、重复文件分析、AI 导出和 AI 导入对被扫描文件均为零副作用；扫描过程中绝不删除、移动或回收文件。
- 扫描完成后默认零选择。只有用户明确勾选并通过最终确认的文件，才可能进入执行器。
- AI 只返回 `KEEP`、`RECOMMEND_RECYCLE` 或 `UNSURE` 建议。导入不会自动勾选、创建授权、选择不可逆模式或执行；“采纳 AI 建议”是另一次本地用户操作。
- 可执行候选默认只能移动到同卷私有隔离区：可显式恢复，但不释放空间。用户若确实要回收空间，必须另行选择“不可恢复清除”，输入包含文件数和逻辑字节数的更强口令；执行器仍会先精确隔离，再从隔离区按句柄清除。
- 执行器只处理完成扫描中已有的精确文件，不接受 AI 路径、glob、目录或命令。一次用户精确计划最多 256 个文件/1 TiB，内部自动拆为每批最多 32 个文件/256 GiB 的独立意图日志；最终页仍一次列出全部文件，确认口令 10 分钟后过期。

## 扫描与分类

界面按“来源域 + 具体类别 + 复核队列 + 执行上限”展示结果，而不是把“看起来像缓存”直接等同于“可以删除”。主要来源域包括：

- AI 模型与推理缓存；
- pip、uv、npm、pnpm、Conda、Gradle、Yarn 等包管理缓存；
- 容器与虚拟化；
- IDE、编辑器与项目构建产物；
- 浏览器和应用缓存；
- 日志、转储、临时文件；
- 安装包、下载与通用空间分析；
- Windows 与系统维护项目。

分析队列和执行上限是两个正交维度。例如 `AI_REVIEW` 只表示“适合获得模型解释”，不会自动选择；但用户可直接选择本地执行策略为 `RECYCLE_ONLY` 的精确文件，AI 不是权限门槛。Windows Update、组件存储等系统维护范围保持 `REPORT_ONLY`/`NONE`；厂商缓存、浏览器缓存和 VS Code 缓存可人工选择或进行可选 AI 复核。

每个队列保留最大的 500 个可见候选，同时维持完整汇总。重复文件页只做至少 1 MiB 文件的有界 SHA-256 分析，不自动产生清理选择。

## 同会话增量扫描

首次扫描建立完整基线，并在遍历前启动 `ReadDirectoryChangesW` 监视器；之后的“增量刷新”只重扫发生变化的父目录/子树并复用未变化观察。扫描期间产生的变化会在提交基线前完成收敛。

增量通知只是失效提示，不是完整性或删除授权。遇到缓冲区溢出、序列异常、监视句柄丢失、根目录身份变化、收敛轮次/容量超限或应用重启时，界面会诚实回退为全量扫描。当前快速刷新只在同一 GUI 进程和同一监视会话内有效；跨进程持久 USN Journal 增量尚未实现。

## AI 复核契约

用户可把当前扫描中明确标记的不确定项导出为有界 JSON。对外数据使用批次内随机候选 ID、脱敏路径提示、大小、年龄、分类和本地证据，不包含文件内容、API Key 或执行能力。

导入器要求响应与当前批次 ID、摘要和全部候选精确对应；未知、重复、遗漏 ID，额外字段，路径、命令、通配符，超限文本和非标准 JSON 均失败关闭。导入结果只在当前会话中提供建议，不能扩大候选集或提高本地执行上限。

## 删除安全模型

执行前会重新验证扫描根和目标的本地固定卷、普通非重解析文件、非云占位符、单硬链接、128 位文件 ID、大小、时间戳和最终句柄路径。`.git`、`.codex`、`.claude`、编辑器历史、用户文档/媒体目录、云同步目录、凭据、密钥、证书、密码库及 DevClean 自身状态目录还受运行期硬拒绝清单保护。

可恢复路径只把精确对象按句柄移动到同卷私有隔离区，并停留在可核验的 `QUARANTINED` 状态；不再转交 Windows Shell 回收站。隔离目录是批准根的随机直属子目录，以私有 DACL 原子创建，任何预存在同名路径均拒绝接管；文件自身 DACL 不被改写，因此显式恢复可保留原权限。恢复/对账只观察或显式恢复，不自动重放删除。

不可恢复清除是用户在最终页主动选择的独立模式。它只面向本地已判定可执行、且通过同一硬保护与身份复核的候选；AI 建议本身不能选择或升级到该模式。执行顺序固定为“原位置 -> 精确隔离 -> 持久化不可逆意图 -> 从隔离区精确处置”，不回退到 `os.remove`、`unlink`、`rmtree` 或目录递归删除。低风险 Temp/CrashDumps 可以被便捷选中，但不再拥有绕过隔离步骤的直接永久路径。

完整批次的执行意图会在首次文件变更前写入独立 SQLite 日志（`journal_mode=DELETE`、`synchronous=FULL`、`BEGIN IMMEDIATE`）。GUI 扫描结果只保存在当前进程内存中，不建立全盘扫描数据库；日志只保存删除安全状态，最多保留最近 128 个已完成批次，所有未决、可恢复或不确定状态始终保留。不确定的崩溃状态不会被自动重试。私有隔离只证明文件离开原位置，释放空间固定为 0；只有完成隔离后精确处置的不可恢复模式才报告已清除文件的逻辑字节总和，并明确不把它冒充卷空闲空间实测值。

## 当前限制

- 只执行文件级动作，不递归删除目录；reparse point、Cloud Files placeholder、硬链接和身份不完整对象拒绝执行。
- pip/uv/npm/pnpm/Conda/Hugging Face/Ollama/IDE 等尚未接入厂商命令/API 动作；其中本地普通文件仍可由用户加入通用精确隔离/强确认清除，容器虚拟磁盘及系统管理对象继续只报告。
- 不提权，不修改注册表、服务或系统组件，不执行 Windows Update/WinSxS/`Windows.old` 清理，不调用 BleachBit `--clean`。
- 不内置 Codex/Claude API，不保存模型凭据，也不自动上传扫描数据。
- CLI 仅保留 `scan`、`report`、`plan` 盘点接口；受控删除只存在于 GUI 的扫描后确认链路。
- Windows Shell 回收站桥接已移除，避免其无法强制可恢复时退化成永久删除；GUI 提供启动对账、隔离项查看和不覆盖原路径的逐项恢复。

详细设计见 [ADR-004](docs/adr/ADR-004-controlled-cleanup-workflow.md)、[开源工具对比审计](docs/open-source-comparison.md)、[威胁模型](docs/threat-model.md)、[实施状态](docs/implementation-status.md)和[需求追踪矩阵](docs/requirements-traceability.md)。参与开发前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [SECURITY.md](SECURITY.md)。

## 运行与构建

开发环境：

```powershell
uv sync --frozen --python 3.13
uv run --frozen --python 3.13 ruff check .
uv run --frozen --python 3.13 mypy src/devclean
uv run --frozen --python 3.13 pytest
```

构建用户 GUI：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1
```

输出为 `artifacts\windows-exe\dist\DevClean.exe`。脚本使用 PyInstaller `--onefile --windowed`，执行无窗口 smoke test，并限制 EXE 不超过 50 MB；用户无需安装 Python、uv 或虚拟环境。公开分发时必须把相邻的 `artifacts\windows-exe\dist\licenses\` 目录与 EXE 一起提供；它包含 DevClean、CPython、Tcl/Tk 和 PyInstaller 的许可证文本。

构建可复现 wheel、SBOM 与校验清单：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1 `
  -Python 3.13 -SourceRevision <full-commit-sha>
```

构建流程和证据边界见[发布工程](docs/release-engineering.md)。一次成功的本地构建不等于代码签名、Windows 11 真机门禁或公开发布已获批准。

## 许可证

GPL-3.0-or-later。第三方边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
