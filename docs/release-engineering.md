# v0.1 发布工程

## 发布边界

v0.1 的发布载荷只包含 Windows 通用 Python wheel、CycloneDX 1.6 JSON SBOM 和
`SHA256SUMS.txt`。构建脚本不会发布、签名、上传或创建 Git 标签；GitHub Actions 也只把三项文件保存为 CI artifact。
wheel 内的 `dist-info/licenses/` 必须逐字节包含仓库的 `LICENSE` 与
`THIRD_PARTY_NOTICES.md`，METADATA 使用 PEP 639 `License-Expression: GPL-3.0-or-later`
并精确列出两份 `License-File`。`LICENSE` 固定为 GNU 官方 GPLv3 纯文本的 UTF-8/LF
字节契约：35,149 字节，SHA-256
`3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`；
`.gitattributes` 与 `.editorconfig` 均固定该文件为 LF。仓库文件和 wheel 内文件必须同时
匹配此契约，不能以“二者相同”替代完整正文验证。

所有工作流中的第三方 action 必须固定到完整 40 位提交 SHA，并在行尾记录对应发布标签。
CI 与项目元数据把 uv 固定为 `0.11.6`，并在同步前运行 `uv lock --check`；依赖环境由
`uv.lock` 与 `uv sync --frozen` 固定。构建后端同时固定在
`pyproject.toml` 的 build-system 和开发依赖中，wheel 使用已锁定环境的后端构建。

## 本地复现

从普通 PowerShell 运行：

```powershell
uv sync --frozen
uv run --frozen python scripts/validate_schemas.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 `
  -Python 3.13 -SourceRevision <full-commit-sha>
```

已提交的 Git 仓库会自动把最后一次提交时间设为 `SOURCE_DATE_EPOCH`。尚无提交的工作树必须显式提供非负整数：

```powershell
$env:SOURCE_DATE_EPOCH = "1783728000"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 `
  -Python 3.13 -SourceRevision WORKTREE_UNCOMMITTED
```

输出位于 `artifacts/release/`：

```text
reclaimer-<version>-py3-none-any.whl
reclaimer.cdx.json
SHA256SUMS.txt
```

`artifacts/release-validation.json` 位于发布载荷目录之外，记录 source revision、构建器/
校验器/锁文件哈希、干净运行时安装、两次 wheel/SBOM 逐字节一致、Schema/RECORD 校验和
发行 wheel 不暴露执行命令的结构化结果。`WORKTREE_UNCOMMITTED` 只供本地演练，G0 验收会拒绝。
自定义 `-EvidenceOutput` 只能是 `artifacts/` 的直接子文件，不能进入
`artifacts/release/` 或工作区外部；已有证据通过同目录临时文件和原子替换更新。

## 失败关闭验证

构建过程依次验证：

1. wheel 从锁定开发环境中的固定 Hatchling 版本构建；
2. wheel 以 `--no-deps --no-index` 安装到新建运行时虚拟环境，且环境中只允许存在 Reclaimer；
3. CycloneDX 工具使用随包离线 schema 生成并校验 1.6 SBOM，输出启用 reproducible 模式；
4. 使用相同 `SOURCE_DATE_EPOCH` 连续生成两次 wheel 与 SBOM，要求两者分别逐字节一致；
5. wheel 的 ZIP、METADATA、RECORD 哈希/大小、路径安全属性、PEP 639 许可证表达式、完整 GPLv3 固定字节契约以及两份许可/告知文件的原始字节通过独立校验；
6. SBOM 根组件必须与 `pyproject.toml` 名称、版本和 GPL 表达式一致，运行组件为空且 dependency graph 只能引用 root；
7. 校验和清单必须以无 BOM UTF-8 保存，并精确覆盖 wheel 与 SBOM；
8. 发布目录不得夹带其他普通文件。

可以在不重建的情况下重新验证现有载荷：

```powershell
uv run --frozen python scripts/validate_release_artifacts.py --directory artifacts/release
```

本流程不构成 G2、代码签名或公开发布门槛已经通过的声明。
