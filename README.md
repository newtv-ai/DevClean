# DevClean

DevClean 是面向 Windows 11 x64 的磁盘扫描与垃圾清理工具。最终用户直接运行
单文件 `DevClean.exe`，不需要安装 Python。

它把扫描结果分成两类：

- 工具依据规则确定可以删除的内容，直接进入“可以删除”列表。
- 工具无法确定的内容，导出给 AI 逐项说明；导回结论后自动分类。AI 仍不确定
  的项目由用户作最终决定。

用户可以选择“移到回收站”或“彻底删除”。某一项删除失败时会记录失败并继续
下一项，再次点击清理可以重试仍存在的项目。

## 直接使用

下载 [release/DevClean.exe](release/DevClean.exe) 后运行。程序首次启动会在 EXE
旁创建 `DevClean-data`，其中包含三份可编辑规则：

- `rules/scan-rules.json`
- `rules/delete-rules.json`
- `rules/keep-rules.json`

规则支持精确路径、路径前缀、通配符和正则表达式。程序内置规则编辑界面，并
提供默认规则备份与恢复功能。详细操作见
[docs/使用说明.md](docs/使用说明.md)。

## 从源码构建

构建环境只使用 Python 3.13：

```powershell
uv sync --frozen --python 3.13
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

产物位于 `artifacts\windows-exe\dist\`。构建脚本会检查 EXE 体积、GUI 构造、
许可证文件和发布目录白名单。

## 项目结构

- `src/devclean/ui/`：Windows 图形界面
- `src/devclean/scanner/`：只读文件扫描
- `src/devclean/core/`：分类、AI 合同、规则与删除编排
- `src/devclean/platform/windows/`：Windows 文件身份核对、回收站和永久删除
- `src/devclean/config/`：三份默认规则模板

架构说明见 [docs/架构与部署.md](docs/架构与部署.md)。

## 许可证

DevClean 使用 [GNU GPL v3 或更高版本](LICENSE)。
