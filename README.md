# Reclaimer

Reclaimer 是面向 Windows 11 的本地磁盘清理工具：扫描时不建立全盘 SQLite 索引，面向用户分发一个原生 GUI `Reclaimer.exe`，不要求安装 Python、uv 或虚拟环境。

## 清理流程

1. “扫描常见缓存”发现当前用户 TEMP、崩溃转储、浏览器 Cache/Code Cache/GPUCache、缩略图缓存，以及 pip、uv、npm、pnpm、Hugging Face、Gradle、Yarn、Ollama、VS Code 的常见本机缓存目录。仅 TEMP/崩溃转储中超过 7 天的普通文件会自动永久清理。
2. 每个自动删除文件都必须通过本地固定磁盘、非重解析点、非云占位符、单硬链接、扫描快照和删除句柄复核；任一条件不满足即跳过。
3. 开发/AI 缓存进入 AI 审查。界面复制最多 50 项的脱敏元数据包；将 Codex 或 Claude 的固定 JSON 回复粘贴回来后，只有该批次内 `DELETE` 的精确扫描文件才会再次句柄复核并永久清理。模型不能提供路径、命令、通配符或扩大范围。
4. AI 返回 `UNSURE`、文件已变化或句柄复核失败的项目会进入“需要你决定”。用户勾选后，Reclaimer 会再次核对身份，再移入 Windows 回收站。

`.git`、`.codex`、`.claude`、编辑器历史、`.env*`、密钥、证书和密码库始终受保护，不参与自动、AI 或用户删除路径。重解析点和云文件占位符也不会被遍历或删除。

扫描提供类别空间汇总与扫描根目录下的 Top 目录；不会把百万级扫描结果写入 SQLite。每栏最多保留 500 项；若 AI 或用户决定项超过上限，请缩小扫描目录后继续审查。还可以检查大于等于 1 MiB 的 SHA-256 精确重复文件：每组自动保留一个基准，其余副本转入用户回收站栏。

## 使用 EXE

从构建产物启动 `Reclaimer.exe`，选择目录后点击“开始扫描”，或直接点击“扫描常见缓存”。“空间概览”显示所扫描驱动器可用空间、类别和顶层目录占用；“检查大文件重复项”只会添加用户决定项，绝不自动删除重复文件。

“开始扫描”本身即授权第 1 条中极窄的自动清理规则；其它文件不会因为扫描而自动删除。模型审查需要用户自行把复制的审查包交给已登录的 Codex 或 Claude，再粘贴其 JSON 回复；Reclaimer 不保存 API 密钥，也不会上传文件内容。操作历史是上限 1 MiB 的脱敏 JSONL 文件，不是 SQLite；它只用于审计，不能恢复永久删除。

## 构建（开发者）

```powershell
uv sync --dev --python 3.13
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1
```

构建会生成 `artifacts\windows-exe\dist\Reclaimer.exe`，强制检查文件不超过 50 MB，并运行无窗口 smoke test。PyInstaller 单文件模式会在启动时在系统临时位置解包运行时，但不会创建持久扫描数据库。

开发验证：

```powershell
uv run --frozen --python 3.13 ruff check .
uv run --frozen --python 3.13 mypy src
uv run --frozen --python 3.13 pytest
```

旧的命令行库存与报告模块仅保留作开发兼容性验证；最终用户 EXE 不调用它们，也不创建其 SQLite 状态库。

## 许可证

GPL-3.0-or-later。
