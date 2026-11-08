# 删除流程修复记录（2026-11-07）

本轮按“先把删除做好”的要求，打通 **扫描 → 规则三类（可删除 / 不能删除 / 不确定）→ AI 判断与人工复核 → 实际删除**。审计报告 [`cleanup-safety-audit-2026-11-07.md`](cleanup-safety-audit-2026-11-07.md) 正文未改动（SHA-256 与修复前一致）。上一轮记录 [`cleanup-safety-fixes-2026-11-07.md`](cleanup-safety-fixes-2026-11-07.md) §3 中“只有直接放在下载文件夹里的安装包才交给用户决定”的规则由本轮取代，已在该节开头加注。

| 项 | 内容 |
| --- | --- |
| 基线 commit | `2089fa6`；在前几轮未提交改动之上继续 |
| 提交状态 | 未 commit、未 push；全部改动留在工作树，等待审查 |
| 完成时间 | 2026-11-07T13:15Z |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13，pytest 8.4.2，mypy 2.2.0（`--platform win32`），ruff 0.15.21；`winreg` 为临时目录中的桩模块。**本轮没有在 Windows 上运行任何东西，不能据此宣称 Windows 通过** |
| 测试数据 | 只用 `tmp_path` 临时夹具；没有删除真实用户文件，没有调用卸载器，没有清理系统缓存，测试不写真实用户路径、不启动包管理器。另从官方地址下载 5 个公开安装包到会话临时目录，**只读取字节做格式识别，从未运行**，不在仓库内 |

## 1. 结果摘要

| 要求 | 现在的行为 | 代码 | 回归测试 |
| --- | --- | --- | --- |
| 按文件用途判断，不限下载根目录、不按扩展名一刀切、不禁止子文件夹 | 安装包按文件内容识别，任意文件夹、任意层级同一规则 | `core/installer_files.py`（新增）；`core/triage.py:522`、`:905` | `test_installers_are_deleted_by_rule_at_any_depth_and_in_any_scanned_folder` |
| 无副作用 | 删除普通文件不启动任何程序（卸载器、CLI、PowerShell 都没有） | 删除路径不变；识别只读字节 | 同上，断言进程启动记录为空 |
| 可删除：直接执行 | 规则可删的安装包进入“可以删除”并默认勾选，点清理即删除，无需额外复核 | `core/triage.py:905` | 同上（真实扫描 → 真实删除） |
| 不确定：交 AI/人工，批准前不删 | 进入“需要判断”；核心层拒绝未批准项；AI 导入或用户点“删除”后可删 | `core/postscan_cleanup.py:77`、`:337`；`ui/app.py:1558`、`:1788`、`:1902` | `test_review_items_are_not_deleted_until_approved_then_deleted` |
| 不能删除：具体原因 | 每类保护写明具体规则（哪个已安装程序、哪个系统缓存、为何是程序文件） | `core/triage.py:983`；`core/review_routing.py:44` | `test_protected_files_carry_a_specific_rule_reason` |
| 混合目录逐文件处理 | 同一文件夹里只删除可删除的文件，受保护文件原样保留 | 同上 | `test_mixed_folders_delete_only_what_the_rules_allow` |
| 删除前核对身份与归属 | 删除前再次核对：同一文件、路径无链接、未被新安装程序接管 | `core/postscan_cleanup.py:735`、`:812`；`platform/windows/exact_cleanup.py:215`；`platform/windows/volumes.py:62` | `test_ownership_change_after_scan_...`、`test_link_replacement_after_scan_...`、`test_link_swapped_in_after_the_last_check_...` |
| 失败原因可见 | 每个失败项显示中文原因 | `ui/app.py:557` | 上述测试均断言界面原因文本；`test_refusal_reasons_are_named_for_the_user` |
| 不逐文件重跑 CLI / 重复发现 | 已安装程序列表每次扫描读 1 次、每次清理读 1 次；安装包识别每个文件约 0.07 ms | `ui/app.py:1152`、`:1456`；`ui/product_vendor_app.py:373` | `test_discovery_runs_once_per_scan_and_once_per_cleanup`；`test_detection_reads_a_few_small_ranges_of_a_large_file` |

## 2. 分类规则（安装包与可执行文件）

> 后续更新：第 6 行“其他位置没有安装包特征的 `.exe` 按程序文件保护”已改为任意扫描文件夹都进入“需要判断”，删除前另加进程/服务归属复查，见 [`final-three-fixes-2026-11-07.md`](final-three-fixes-2026-11-07.md)。

对 `.exe` 以及 `installer_suffixes`（`.msi .msix .msixbundle .appx .appxbundle .iso`）文件，在应用规则、已安装扩展之后、通用“程序文件”规则之前判断（`core/triage.py:905`）。已知清理根目录和临时目录仍按它们自己的策略处理。

| 顺序 | 条件 | 结果 | 界面原因（摘要） |
| --- | --- | --- | --- |
| 1 | 位于 `%SystemRoot%\Installer` 或 `%ProgramData%\Package Cache` | 不能删除，标签 `system_installer_cache` | “Windows Installer 缓存：已安装软件修复和卸载要用到其中的安装包，不能删除” |
| 2 | Windows“已安装的程序”列表（Uninstall 注册表项，HKLM 64/32 位与 HKCU）登记的安装位置、卸载程序或图标文件所在文件夹内 | 不能删除，标签 `installed_program` | “已安装程序「名称」的文件……删除会影响它的运行、修复或卸载” |
| 3 | 文件名是卸载程序（`unins000.exe`、`Uninstall*.exe`、`uninst.exe`），或同文件夹有 `unins###.dat` / `Uninstall.exe` / `uninst.exe` | 不能删除，标签 `installed_program` | “已安装程序的卸载程序或安装文件夹……” |
| 4 | 文件内容证明是安装包 | **可以删除**（`DETERMINISTIC_CANDIDATE`，默认勾选） | “安装包（格式）：只用于安装软件，不属于任何已安装程序；删除只移除这个文件，不会卸载已安装的软件，需要时可重新下载” |
| 5 | `.iso`；扩展名是安装包但内容不符；下载文件夹（任意层级）中没有安装包特征的 `.exe` | **需要判断**（`AI_REVIEW`） | 分别说明“光盘镜像……可能正被虚拟机挂载”“内容不是有效的安装包格式”“可能是便携程序” |
| 6 | 其他位置没有安装包特征的 `.exe` | 交给通用程序文件规则：不能删除 | “程序文件、库文件或虚拟磁盘：被程序直接运行、加载或挂载，没有安装包特征……” |

“内容证明是安装包”的依据（`core/installer_files.py:100-275`），只做几次小范围读取（每个 EXE 最多约 140 KB），从不执行：

- MSI：OLE 复合文件头 `D0CF11E0A1B11AE1`。
- MSIX/APPX：ZIP 且含 `AppxManifest.xml` / `AppxBundleManifest.xml` / `AppxBlockMap.xml`。
- EXE：按 PE 结构定位，而不是全文搜索：
  - 程序映像之后的附加数据（跳过紧随映像的数字签名）以 NSIS `NullsoftInst` 或 7-Zip 自解压 `;!@Install@!UTF-8!` 开头；
  - 或有 WiX 的 `.wixburn` 节；
  - 或版本信息资源写明 Inno Setup / InstallShield / Advanced Installer / Nullsoft Install System；
  - 或版本信息的 FileDescription / ProductName / OriginalFilename / InternalName 中有独立单词 setup、installer，或“安装程序”“安装向导”。
- 版本信息含 uninstall/卸载 的文件视为卸载程序，不算安装包。

只在附加数据里找标记，是为了避免误判：NSIS 编译器 `makensis.exe`、7-Zip 本体的代码里也有同样的字符串（上一版搜索文件前 2 MiB，会把它们当成安装包）。`WinSetupFromUSB` 这类名字里带 Setup 的工具不会被单词匹配误判。

真实样本核对（只读字节，未运行）：Notepad++ 8.6.9（NSIS，其 FileDescription 不含 setup，靠附加数据识别）、Git for Windows 2.47.1（Inno Setup）、VC++ 2015-2022 Redistributable（WiX）、7-Zip 24.08 `.exe` 与 `.msi` 均识别为安装包；仓库内 PyInstaller 构建的便携程序 `release/DevClean.exe` 与两个签名的 .NET DLL 均不是安装包，版本信息解析结果正确。

`scan-rules.json` 的 `installed_payload` 不再按文件夹名跳过 `installer`：`%SystemRoot%\Installer` 改由第 1 条按位置保护，并写明原因；`Downloads\Installer\` 这类用户文件夹不再被整个跳过。

## 3. 删除执行

- **可删除项直接执行。** 现有清理流程不变：勾选 → 点“永久清理所选” → 确认 → 删除。规则可删的安装包不需要再经人工或 AI。
- **审批闸门在核心层。** `candidate_from_triage_item` / `candidate_from_directory_item` 新增 `review_approved` 参数。`AI_REVIEW` / `USER_REVIEW` 项没有批准就拒绝，原因是“需要 AI 或你批准后才能删除，尚未批准”。界面在以下情况视为批准：
  - 本次扫描中 AI 导入或用户点“删除”的路径（`_review_approvals`，每次新扫描清空）；
  - 已保存的 DELETE 规则。

  界面原本只从左侧列表取勾选项，这个闸门是防止其他调用方绕过的第二道防线。
- **删除前复核**（`_preflight_candidate` 等）：
  - 已有：扫描根与文件身份、大小、时间一致；路径在本地固定卷且没有链接或重解析点；不属于受保护应用数据；所属程序没有运行。
  - 本轮新增：扫描后被新安装程序接管（例如把程序装进了这个文件夹）或落入系统安装缓存，就拒绝删除，原因是“扫描后它已归属已安装的程序或安装缓存，已拒绝删除”；文件被换成链接时明确报告。
  - 链接与 Windows：`has_reparse_ancestor` 现在也识别 POSIX 符号链接。Windows 上原本就靠重解析属性识别，行为不变。
- **最后一刻的链接替换。** 非 Windows 的删除实现（`_purge_exact_file_posix`，仅用于 Linux 夹具）按 Windows 版同样的规则执行：核对根目录身份；路径任一层是链接就拒绝；通过父目录句柄（`O_NOFOLLOW`）确认还是同一个对象后才 `unlink`。Windows 删除路径本轮未改。
- **非 Windows 的身份类型。** 在 Linux 上接受 `stat_ino` 文件身份，并放宽目录属性检查，这样真实流程能在 Linux 夹具上跑通。Windows 上仍只接受 128 位文件 ID，条件不变（`core/postscan_cleanup.py:81`）。
- **失败原因。** 新增按顺序匹配的原因表（`ui/app.py:557`）：
  - 硬链接 → “文件还有其他硬链接名称……已跳过”；
  - 链接、越界 → “路径被替换成链接或离开了扫描范围”；
  - 内容变化 → “扫描后内容已改变”；
  - 其余显示“安全检查拒绝（原文）”。

## 4. 性能

| 测量 | 修复前 | 修复后 |
| --- | --- | --- |
| 每个 `.exe` 的安装包识别（热缓存） | 5.4 ms（读前 2 MiB 并多次搜索；300 个 3 MB 文件） | 0.07 ms（真实安装包与 DevClean.exe，200 轮平均）；最多约 12 次小读取、约 140 KB |
| 已安装程序列表读取 | — | 每次扫描 1 次、每次清理 1 次（`scan_root_snapshot`），与文件数无关 |
| 3 202 个文件的合成开发者目录完整扫描 | 4.5–4.9 s（上轮） | 4.8–4.9 s；1 340 项分类结果与上轮逐行一致 |

合成目录里没有 EXE，所以第三行只证明其他文件的分类与速度不受影响；EXE 的开销以第一行为准。

## 5. 测试与检查

新增或改写的测试（都走真实代码，不是 mock 分类）：

- `tests/test_deletion_workflow.py`（8 项）：
  - 链路：真实产品扫描 worker → 真实 `_partition_items` → 真实产品删除 worker（候选、复核、日志）→ 真实精确删除（Linux 分支）。
  - 夹具：只替换了“已安装程序列表”，所有进程启动都被记录并拒绝。
  - 覆盖：多层子目录与非下载目录中的安装包；混合文件夹；规则可删项直接执行；待复核项先被拒绝，用户批准和 AI 批准后被删除；扫描后归属变化；文件或父目录被换成链接；删除最后一刻被换成链接；发现开销。
- `tests/test_installer_files.py`（11 项）：
  - 五种安装框架；签名安装包；只是“提到”安装字样的程序（makensis、7zFM、WinSetupFromUSB、Inno 卸载程序、带签名的普通程序）不算；
  - 7 种畸形文件不报错；MSI/MSIX 必须有真实格式；
  - 真实 `release/DevClean.exe` 不算安装包；大文件只做少量小读取；
  - 卸载程序与安装文件夹标记；已安装程序的文件夹、卸载程序、图标归属，忽略过宽位置与 msiexec；系统安装缓存；
  - 用假 `winreg` 验证 Uninstall 注册表三个视图的读取与 `REG_EXPAND_SZ` 展开。
- `tests/pe_samples.py`：生成结构合法的最小 PE 文件（节表、资源树、VS_VERSIONINFO、附加数据、签名表），替代上一轮的“MZ + 标记字符串”假字节。
- 改写：
  - `test_ordinary_installer_cleanup.py`：Windows Installer 缓存“按位置保护、不按文件夹名”，`Downloads\Installer\` 中的安装包可删除；
  - `test_generic_review_routing.py`：`.iso` 从硬保护改为需要判断；
  - `test_cleanup_safety_fixes.py`：删除 3 个按“仅下载根目录”设计的旧测试，补充失败原因断言；
  - `test_cleanup_execution.py`、`test_ordinary_installer_cleanup.py`：代表“已批准”的 AI 复核项改为显式传 `review_approved=True`。

变异核对：在临时副本中逐一去掉以下保护，再运行新测试。

- 以下 6 项每项都至少让一个测试失败：审批闸门、删除前归属复核、任意层级识别、扫描时归属判断、清理时发现快照、内容识别（把任意 EXE 当安装包）。
- 第 7 项只去掉 Linux 删除实现内的“路径中有链接”检查。下一层“目标在批准的根目录之外”仍以同样的界面原因拒绝，外部文件保持完好，所以测试无法区分是哪一层拒绝的。这一层是纵深防御的一部分，删除结果仍被测试覆盖。

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（Linux，`winreg` 桩，临时 CWD） | 231 failed / 889 passed / 28 skipped。231 个失败的测试 ID **和失败信息**与上轮基线逐条一致（无新增、无变化），都是 Windows 路径/API 语义在 Linux 上不成立的既有失败；通过数比上轮多 7 |
| `ruff check src scripts tests`（CI 同款） | 通过 |
| `mypy --platform win32 src tests scripts` | 通过（353 个文件） |
| 测试后临时 CWD 与仓库根 | 无残留文件或目录 |
| 审计报告 SHA-256 | 与修复前一致 |

## 6. 未验证范围与已知缺口

1. **Windows 未实测。**
   - Windows 删除路径（句柄式精确删除、回收站）本轮未改，但也没有在 Windows 上运行。
   - Uninstall 注册表读取只用假 `winreg` 测过，未枚举真实注册表。
   - 安装位置含 8.3 短路径、或 `UninstallString` 写法特殊（未加引号且路径含空格又不以 `.exe` 结尾）时，可能识别不出归属。
2. **界面不列出“不能删除”的文件。** 每个受保护文件都有具体规则原因（已测试），但现有界面隐藏 REPORT_ONLY 项，用户看不到这些原因。要展示，需要新增第三个列表或筛选，本轮未做。
3. **已保存的规则文件保持旧设置。** 已有用户数据目录中的 `scan-rules.json` 是权威配置，仍含 `installer` 跳过名，这些用户的 `…\Installer\` 文件夹仍不扫描（只是少扫，不会误删）。需要手动从 `installed_payload` 删除该项，或删除该文件让新版本重建。本轮未做自动迁移。
4. **识别覆盖面。**
   - 不带上述框架标记、版本信息也不写 setup/installer 的安装程序（如部分 Squirrel 安装器、自制安装器）不会被规则判为可删。它们在下载文件夹里进入“需要判断”，在其他位置按程序文件保护。
   - 下载文件夹以外、没有安装包特征的 `.exe` 被当作程序文件保护，而不是送 AI。这是判断取舍：游戏、工具目录里大量程序文件不应涌入复核列表。如需改变，只需改 `core/triage.py:905` 中的一行条件。
5. **归属来源。**
   - 归属只来自 Windows 已安装程序列表，以及卸载程序文件名或卸载数据。没有登记、也没有卸载数据的便携程序文件夹里如果放着一个真正的安装包，该安装包会被判为可删除。删除它不影响已安装的程序，但用户可能想留作备份。
   - 进程或服务持有的归属仍属上轮 M-1 剩余缺口。
6. **AI 判断。** 沿用现有导出/导入流程：用户导出，交给自己的模型，再导入结论。本轮没有新增任何内置 AI 调用，也没有假装有自动 AI 判断。
7. **下载文件夹被重定向到扫描范围之外**（如 `D:\Downloads`）时，需要用户把它加入扫描路径；这一点与之前相同。
8. `keep-rules.json` 中的 `.iso` 不再决定 `.iso` 的分类（安装包分支先判定为“需要判断”）；保留它不影响其他扩展名。

## 7. 改动文件（本轮）

- 新增：`src/devclean/core/installer_files.py`；`tests/test_deletion_workflow.py`；`tests/test_installer_files.py`；`tests/pe_samples.py`；本文档。
- 修改：
  - `src/devclean/core/triage.py`：安装包分类与归属，移除 `is_downloaded_installer`，程序文件原因写具体；
  - `src/devclean/core/postscan_cleanup.py`：审批闸门、删除前归属复核、链接替换原因、非 Windows 身份；
  - `src/devclean/core/review_routing.py`：两个新的硬保护标签；
  - `src/devclean/platform/windows/registry.py`：`uninstall_entries`；
  - `src/devclean/platform/windows/exact_cleanup.py`：非 Windows 删除实现；
  - `src/devclean/platform/windows/volumes.py`：识别 POSIX 符号链接；
  - `src/devclean/ui/app.py`：批准记录、失败原因表、扫描与清理快照；
  - `src/devclean/ui/modern_app.py`、`src/devclean/ui/product_app.py`：新扫描清空批准；
  - `src/devclean/ui/product_vendor_app.py`：清理快照；
  - `src/devclean/config/scan-rules.json`；`README.md`；
  - `docs/cleanup-safety-fixes-2026-11-07.md`：§3 加注取代说明；
  - 测试：`tests/test_cleanup_safety_fixes.py`、`tests/test_ordinary_installer_cleanup.py`、`tests/test_generic_review_routing.py`、`tests/test_cleanup_execution.py`。
