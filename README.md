# Reclaimer

Reclaimer 是面向 Windows 11 的本地磁盘清理工具：扫描时不建立全盘 SQLite 索引，面向用户分发一个原生 GUI `Reclaimer.exe`，不要求安装 Python、uv 或虚拟环境。

## 清理流程

1. 仅对当前用户 `%TEMP%` 内超过 7 天的普通文件自动永久清理。每个文件都必须通过本地固定磁盘、非重解析点、非云占位符、单硬链接、扫描快照和删除句柄复核；任一条件不满足即跳过。
2. 识别到的开发缓存会进入 AI 审查。界面将复制一个最多 50 项的脱敏元数据包；其中没有文件内容。将 Codex 或 Claude 的固定 JSON 回复粘贴回来后，只有该批次内 `DELETE` 的精确扫描文件才会再次句柄复核并永久清理。模型不能提供路径、命令、通配符或扩大范围。
3. AI 返回 `UNSURE`、文件已变化或句柄复核失败的项目会进入“需要你决定”。用户勾选后，Reclaimer 会再次核对身份，再移入 Windows 回收站。

`.git`、`.codex`、`.claude`、编辑器历史、`.env*`、密钥、证书和密码库始终受保护，不参与自动、AI 或用户删除路径。重解析点和云文件占位符也不会被遍历或删除。

扫描仅在内存中保留各栏最大的 500 项用于展示；不会把百万级扫描结果写入 SQLite。若 AI 审查项超过展示上限，请缩小扫描目录后继续审查。

## 使用 EXE

从构建产物启动 `Reclaimer.exe`，选择要扫描的目录并点击“开始扫描”。

“开始扫描”本身即授权第 1 条中极窄的自动清理规则；其它文件不会因为扫描而自动删除。模型审查需要用户自行把复制的审查包交给已登录的 Codex 或 Claude，再粘贴其 JSON 回复；Reclaimer 不保存 API 密钥，也不会上传文件内容。

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
