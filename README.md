# DevClean

DevClean 是面向 Windows 11 x64 的磁盘扫描与垃圾清理工具。最终用户直接运行
单文件 `DevClean.exe`，不需要安装 Python。

它把扫描结果分成两类：

- 工具依据规则确定可以删除的内容，直接进入“可以删除”列表。
- 工具无法确定的内容，导出给 AI 逐项说明；导回结论后自动分类。AI 仍不确定
  的项目由用户作最终决定。

界面会直接提醒：AI 判断不一定准确，使用外部或付费模型可能产生费用。导出时，
同一目录下证据一致的生成型文件名会保守地合并成一个判断，并把代表范围和统计
信息写入请求；导入后组内每个已观察文件都会分别保存精确结论。普通文件名、
不同目录或风险条件不同的文件仍分别询问。导出包含本机完整路径，界面会提醒
用户只交给自己信任的模型。

用户可以选择“移到回收站”或“彻底删除”。某一项删除失败时会记录失败并继续
下一项，再次点击清理可以重试仍存在的项目；双击左右任意一行可以直接在资源
管理器中打开目录或选中文件，便于手工处理占用、权限等原因留下的项目。

## 直接使用

下载 [release/DevClean.exe](release/DevClean.exe) 后运行。程序首次启动会在 EXE
旁创建 `DevClean-data`。规则、默认备份、AI 导入索引和有界删除日志都集中在
这里，不写入 AppData；删除 EXE 和这个文件夹就是完整卸载。其中三份可编辑规则为：

- `rules/scan-rules.json`
- `rules/delete-rules.json`
- `rules/keep-rules.json`

规则支持精确路径、路径前缀、通配符和正则表达式。程序内置规则编辑界面，并
提供默认规则备份与恢复功能。AI/用户结论中的用户目录会保存成 Windows 环境
变量，日期、UUID、哈希和生成型数字标识会在条件允许时形成同类路径规则，避免
换账号、换电脑或文件名变化后重复询问 AI。详细操作见
[docs/使用说明.md](docs/使用说明.md)。

## 从源码构建

构建环境只使用 Python 3.13：

```powershell
uv sync --frozen --python 3.13
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

产物位于 `artifacts\windows-exe\dist\`。构建脚本会检查 EXE 体积、GUI 构造、
许可证文件和发布目录白名单。

当前 DevClean 功能测试：

```powershell
uv run --frozen pytest
```

这些测试只覆盖现在的扫描、规则、AI 导入、删除编排和有界日志，不包含已删除的
Reclaimer 旧架构测试。

## 项目结构

- `src/devclean/ui/`：Windows 图形界面
- `src/devclean/scanner/`：只读文件扫描
- `src/devclean/core/`：分类、AI 合同、规则与删除编排
- `src/devclean/platform/windows/`：Windows 文件身份核对、回收站和永久删除
- `src/devclean/config/`：三份默认规则模板

架构说明见 [docs/架构与部署.md](docs/架构与部署.md)。

## 许可证

DevClean 使用 [GNU GPL v3 或更高版本](LICENSE)。
