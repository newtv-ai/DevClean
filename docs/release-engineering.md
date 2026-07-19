# DevClean `0.2.0a1` 发布工程

## 两类构建产物

DevClean 当前有两个相互独立的构建入口：

1. `scripts/build_release.ps1` 生成可复现 Python wheel、CycloneDX 1.6 SBOM 和 `SHA256SUMS.txt`，并在 `artifacts/release-validation.json` 写入结构化验证证据。
2. `scripts/build_windows_exe.ps1` 生成面向用户的单文件、无控制台 `DevClean.exe`，执行会真实构造完整窗口和控件树的 `--ui-smoke`，并强制 50 MB 上限。

wheel/SBOM 的 `artifacts/release/` 是严格三文件载荷；GUI EXE 位于 `artifacts/windows-exe/dist/`，当前不混入该校验和清单。构建脚本不会签名、上传、发布 Release、创建 Git 标签或批准公开分发。

## 受控清理发布边界

wheel 包含扫描、分类、AI 合同、同会话增量、持久意图和窄执行模块，但 console entry point 仍只暴露 `scan`、`report`、`plan`。用户写入流程只通过 GUI 的“完成扫描 -> 本地选择 -> 最终确认”链路到达。

`validate_release_artifacts.py` 对每个运行时 Python 文件执行 AST 边界检查：

- 拒绝已撤销的 `ai_review.py`、`auto_clean.py`、`recycle.py` 和旧 `permanent_delete.py`；
- scanner、triage、AI 合同与增量/inventory 观察层不得导入执行模块；
- 原始文件变更符号只允许出现在 `platform/windows/exact_cleanup.py`；隔离目录的原子 `CreateDirectoryW` 只允许出现在 `platform/windows/security.py`。`recycle_bin.py` 被列为禁止打包模块，不得重新进入运行时；
- 所有模块都禁止通用 `os.remove`、`os.unlink`、`shutil.rmtree`；
- CLI 不得注册 `apply`、`clean`、`delete`、`execute`、`prune`、`recycle`、`remove` 等执行命令；
- GUI `_scan_worker` 不得引用批次执行、精确处置、隔离或清除原语。
- 所有不可恢复清除必须在源码结构与测试中表现为“精确隔离 -> 持久化 `PURGE_PENDING` -> 隔离对象精确处置”；从原路径直接永久处置或 Shell 回收站桥接均为发布拒绝条件。

验证器证明的是打包代码的分层/表面约束，不证明业务语义、Windows 真机行为或任意未来调用路径安全。它必须与测试、canary 和人工审计共同使用。

## 锁定与许可契约

- `uv` 固定为 `0.11.6`，同步前运行 `uv lock --check`，依赖由 `uv.lock` 与 `uv sync --frozen` 固定。
- Hatchling 在 build-system 与开发依赖中均精确固定为 `1.28.0`；wheel 使用锁定环境并禁用构建隔离。
- wheel 是 `py3-none-any`，运行时不声明第三方依赖。
- METADATA 使用 PEP 639 `License-Expression: GPL-3.0-or-later`，并精确列出 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`。
- `LICENSE` 固定为 GNU GPLv3 官方 UTF-8/LF 文本：35,149 字节，SHA-256 `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`。
- GitHub Actions 必须固定到完整 40 位提交 SHA，且 checkout 不持久化凭据。

## 本地构建

先运行质量门禁：

```powershell
uv sync --frozen --python 3.13
uv run --frozen --python 3.13 ruff check .
uv run --frozen --python 3.13 mypy src/devclean
uv run --frozen --python 3.13 pytest
uv run --frozen --python 3.13 pytest --cov=devclean --cov-report=term-missing
```

已提交 revision 的 wheel/SBOM 构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 `
  -Python 3.13 -SourceRevision <full-commit-sha>
```

脚本默认以最后一次 Git 提交时间设置 `SOURCE_DATE_EPOCH`。未提交工作树只允许本地演练，并应显式标记：

```powershell
$env:SOURCE_DATE_EPOCH = "1783728000"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 `
  -Python 3.13 -SourceRevision WORKTREE_UNCOMMITTED
```

`WORKTREE_UNCOMMITTED` 证据不能用于 G0 发布验收。wheel 输出：

```text
artifacts/release/DevClean-<version>-py3-none-any.whl
artifacts/release/DevClean.cdx.json
artifacts/release/SHA256SUMS.txt
artifacts/release-validation.json
```

GUI EXE 构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_exe.ps1 `
  -Python 3.13 -MaximumMegabytes 50
```

脚本输出 JSON，包括 `DevClean.exe` 的绝对路径、字节数、SHA-256、大小上限、“用户无需 Python”的标志以及许可证目录/文件清单。公开分发必须把整个 `artifacts/windows-exe/dist/licenses/` 与 EXE 放在同一下载包中；该目录由精确构建环境中的 CPython、Tcl/Tk、PyInstaller 许可证及项目许可证生成，任一必需文本缺失都会使构建失败。该输出应连同文件哈希写入当次交接证据；不得沿用旧 EXE 哈希。

## wheel 失败关闭验证顺序

`build_release.ps1` 依次：

1. 检查 lockfile，建立冻结开发环境并离线校验仓库 JSON Schema；
2. 用固定 Hatchling 构建 wheel；
3. 在全新 runtime venv 中以 `--no-deps --no-index` 安装，要求环境仅包含 DevClean；
4. smoke import、版本一致性、CLI 帮助与无执行命令检查；
5. 生成并校验可复现 CycloneDX 1.6 SBOM；
6. 在相同 `SOURCE_DATE_EPOCH` 下第二次构建，要求 wheel 和 SBOM 分别逐字节一致；
7. 生成无 BOM、小写 SHA-256 清单；
8. 校验 ZIP 路径/碰撞/压缩/大小、METADATA、WHEEL、RECORD、许可证原始字节、SBOM graph、三文件白名单和受控清理分层；
9. 原子写入 `release-validation.json`，包括 source revision、wheel/SBOM/checksums、builder/validator/lockfile 哈希和 `controlled_cleanup_surface_validated=true`。

可以不重建而复核现有三文件载荷：

```powershell
uv run --frozen python scripts/validate_release_artifacts.py `
  --directory artifacts/release
```

## 带删除能力构建的额外发布门

wheel 验证通过仍不足以交付 GUI。具体 `DevClean.exe` 至少还需要：

- 在 Windows 11 标准用户 token 下启动，提升 token 必须被拒绝；
- 复现历史扫描期误删场景，证明扫描/分类/AI 导出/导入对 canary 内容、身份和时间戳零修改；
- 真机验证同卷隔离 -> 恢复，以及独立确认后的隔离 -> 精确清除；
- 覆盖 replacement/rename race、reparse、Cloud placeholder、hardlink、锁定、ACL、根目录变化和保护资产；
- 将增量结果与独立全量 oracle 比较，并验证所有 monitor 不确定性回退全量；
- 人工走通默认零选择、AI 导入不选中、显式采纳、可恢复隔离/确认清除分开确认、失败状态与执行后刷新；
- 固化 source revision、EXE 字节数/SHA-256、wheel/SBOM/checksum 哈希、测试数、覆盖率和审计结论。

在这些门禁未对同一 source revision 和同一制品完成前，只能称为本地 Pre-Alpha 构建，不能称为已签名、已认证、生产就绪或公开发布。详细能力门见 [ADR-004](adr/ADR-004-controlled-cleanup-workflow.md) 与[需求追踪矩阵](requirements-traceability.md)。
