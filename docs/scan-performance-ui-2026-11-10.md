# 扫描性能、计时、界面卡顿、临时文件夹与清理拒绝（2026-11-10）

针对 b156d9b 构建的用户实测反馈：

| 项 | 用户反馈 |
| --- | --- |
| 扫描速度 | 完整扫描 110 分 2 秒，扫描中已看过 405,827 个文件（118.2 GB） |
| 计时 | 扫描中要实时显示实际已用时间（2026-11-11 更正：此前误解为“扫完才显示”） |
| 界面卡顿 | 扫描一两个小时后切换窗口再切回，窗口“未响应” |
| (A) 最终统计 | 扫完后“已看过 N 个文件”的统计消失 |
| (B) 清理拒绝 | 66 项中 36 项未完成：“所属程序正在运行” 35 项，系统根黑名单 1 项（crx_cache） |

| 项 | 内容 |
| --- | --- |
| 基线 | `b156d9b` |
| 提交状态 | §1～§10 已随 `472364d` 提交、推送并由 Windows CI 构建；§11 随 `344c101` 提交、推送；实时计时随 `c20d81a` 提交、推送；§12 随 `80e944d` 提交、推送；§13 随 `4061697`、`9459930` 提交、推送；§14 随提交“Keep cleanup moving row by row and never wait on a prompt”提交、推送；§15 进行中，未 commit |
| 审计原文 | `docs/cleanup-safety-audit-2026-11-07.md` 未改动 |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13.18，pytest 8.4.2，mypy 2.2.0（`--platform win32`），ruff 0.15.21；`winreg` 为临时目录中的桩。**本轮没有在 Windows 上运行任何东西**，见 §9 |
| 测试数据 | 只用 `tmp_path` 和会话临时目录里生成的文件；没有删除真实用户文件、没有卸载、没有清系统缓存 |

## 1. 用户决定（原样记录）

- **临时文件夹：** temp/tmp 临时文件夹整个目录放“安全可清理”，默认勾选、直接删除；删除前复核目录身份、链接、越界和占用；删除时被占用的文件跳过并记录，不让整个清理失败；其他文件夹照旧。
  - **它放宽了 2026-11-07 审计“仅凭时间不能证明临时文件未在用”的结论。** 现在只要位于临时文件夹就视为可清理，正在使用的文件只靠删除时无法打开来保护。
- **正在运行的程序：** 不应因为程序在运行就拦下它的全部缓存，而应按文件实际是否被占用逐个跳过；只有确实不能在程序运行时动的目录，才提示先关闭程序。
  - 这同样改变了原有的进程守卫行为，见 §6。

**我按保守口径定的范围（可在核查时调整）：**
- “名字明确是 temp/tmp 的临时目录”只认 AppData\Local、AppData\Roaming、AppData\LocalLow 下的。
- ProgramData 下的不算：它在系统根黑名单里，算进来删除时会被拒，就会重演 crx_cache 的矛盾。
- 项目、文档里的 tmp 文件夹照常逐项判定。

## 2. 根因（Linux 基准剖析）

### 2.1 基准方法

1. 在会话临时目录生成仿 Windows 用户目录的文件树（脚本不在仓库内），各部分占比：

   | 部分 | 占比 |
   | --- | --- |
   | Temp | 18% |
   | 已安装程序 | 6% |
   | Chrome 缓存 | 10% |
   | npm 缓存 | 10% |
   | 应用数据 | 12% |
   | 源码仓库（node_modules、bin/obj） | 28% |
   | 编辑器扩展 | 8% |
   | .cache | 5% |
   | Downloads 安装包 | 3% |

2. 按测试夹具的方式隔离环境变量，并给进程列表、服务、注册表、Known Folder、子进程打桩。
3. 无界面直接运行真实的 `ProductDevCleanWindow._scan_worker`。

### 2.2 热点（b156d9b，cProfile 比例）

| 热点 | 占比 | 原因 |
| --- | --- | --- |
| `evaluate_application_path` | 约 66% | 每个文件都依次跑约 38 个应用模块的全部规则（仅 Opera 每 2 万文件就有 233 万次规则比较），绝大多数文件根本不在这些应用目录下 |
| 实时预览 `_rows_of` | 约 28%，并随扫描增长 | 每 1.5 秒对全部保留样本重算分类桶；`is_within_kept_directory` 对每级祖先跑完整匹配器。样本越多越慢，扫描线程留给扫描本身的时间越来越少 |
| `has_installed_program_markers` | 随目录变大超线性 | 每个 exe 都重新列一遍所在目录；几千个安装包同在 Downloads 时是平方级 |
| 目录遍历 | 约 9% | 合理。Windows 上文件大小来自目录枚举，不额外打开文件；每个目录在枚举前后各读一次身份（扫描时的替换防护，保留） |

### 2.3 用户提出的几个检查点

- **stat：** Windows 上每个文件 0 次额外 stat（枚举自带），每个目录 2 次身份读取；Linux 夹具上每个文件 1 次。
- **内容读取 / 哈希：**
  - 没有哈希。
  - 只有 `.exe` 和安装包后缀（.msi/.msix/.appx/.iso 等）读取有限字节做安装包识别；已知根、临时目录、已安装程序目录内的不读。
  - 基准中读取次数前后相同（2 万文件 683 次，4 万文件 1366 次）。
- **重复遍历：**
  - 整树厂商缓存先汇总一次，再从逐文件扫描中排除；没有其他重复遍历。
  - Temp 现在只遍历一次、不分类。
- **缓存失效：**
  - 各应用的根函数都有扫描快照缓存（有效）。
  - 失效的有三处：预览没有任何缓存；安装程序标记没有缓存；Claude/Cursor 的根辅助函数每次调用都重新解析整张环境变量表。

### 2.4 “未响应”的根因

1. 扫描线程长时间占用 CPU（逐文件规则 + 不断变重的预览）。
2. 界面线程每次预览都整表清空、再插入 2×300 行。
3. `_tkinter` 每次 Tcl 调用都会释放并重新获取 GIL；扫描线程占着 GIL 时，每次重新获取要等约 5 ms 的切换间隔，几百次调用就是数秒。这几秒里窗口消息得不到处理，Windows 就标“未响应”。
4. 扫描越久，扫描线程越忙，卡得越久。

## 3. 性能修复

| 改动 | 位置 | 等价性依据 |
| --- | --- | --- |
| 应用规则前缀索引：扫描快照内，只调用“路径落在其根下”或“文件名/路径分段命中其项目文件触发器”的模块；37 个评估器纳入（Claude 的规则含整个用户目录，不纳入） | `core/application_cleanup.py`（`_SCOPED_APPLICATIONS`、`_applications_not_holding`） | 逐个阅读各模块匹配函数；离线差分 154,140 个路径（含约 9.7 万个文件名触发探针）与 b156d9b 逐一相同；仓库测试 `test_skipping_applications_that_cannot_hold_a_path_changes_no_decision`；删掉 npm 触发器做变异，该测试立即失败 |
| Claude/Cursor 规则循环先判断根能否包含路径；`_expand_braces` 缓存；`_native_paths`、Claude 应用根、Cursor 临时根放入扫描快照缓存 | `_application_cleanup_impl.py`、`claude_*`、`cursor_*` | 同上差分覆盖 |
| 预览按会话缓存每个文件条目的分类桶（一次扫描内规则固定），目录条目仍每次重算 | `ui/app.py` `_rows_of` | `test_live_preview_classifies_each_retained_file_once`：结果与全量重算一致，每个文件只分类一次 |
| `is_within_kept_directory`：一次规范化 + 逐级集合查询（目录 KEEP 只有精确路径；其他匹配列表非空时退回原写法） | `_user_rules_impl.py` | 语义等价 |
| 安装程序标记每次扫描/清理按目录只列一次（快照外行为不变） | `core/installer_files.py` | `test_installer_markers_are_listed_once_per_folder_in_a_run` |
| 临时文件夹整体汇总（§5） | `core/temp_folders.py` | §5 |

## 4. 界面：计时、最终统计、主线程

- **计时 (2)：**
  - **用户 2026-11-11 要求（更正此前“扫完才显示”的误解）：** 扫描一开始就显示实际已用时间，约每秒刷新一次，例如“扫描耗时：45 秒”“扫描耗时：1 分 05 秒”“扫描耗时：2 分 30 秒”。
  - 实现：`ProductDevCleanWindow._schedule_scan_clock` 用 Tk `after` 每 1 秒走一次，只改标签文字；同一时间最多一个待执行的计时，重新扫描会先取消旧的；扫描结束、失败或换了新扫描时自动停止。测试：`tests/test_scan_ui_refresh.py`。
  - 停表只在扫描真正结束、结果和表格都已填好之后（新的 `_scan_finished`）。AI 导入、规则修改等重新发布不再触碰计时。
  - 完成后显示“扫描总耗时：X 分 Y 秒”，取消时显示“扫描已取消，用时 …”。
- **最终统计 (A)：** 扫描线程把总文件数、总文件夹数、总大小（含整树汇总部分）随完成事件一起发回。状态栏保留为“扫描完成：共 N 个文件、M 个文件夹，总大小 X，总耗时 T。左边是…”。
- **主线程 (3)：**
  - 最终分区和按大小排序在扫描线程完成，界面线程只渲染。
  - 进度事件按时间节流（每 0.25 秒最多一次，最终一次必发）。
  - 实时预览按差异更新：离开的行一次调用删除，新行插入，移动的行移动，文本变化的行才改写；相同的预览零次 Tk 调用。
  - 结合 §3，扫描线程长时间占用 GIL 的情况大幅减少，界面线程每次刷新的 Tk 调用从约 600 次降到只与变化行数相关。
  - 测试：`tests/test_scan_ui_refresh.py`、`test_the_scan_worker_hands_over_totals_and_the_finished_partition`。

## 5. 临时文件夹整目录（用户决定）

**识别（`core/temp_folders.py`）：**
- %TEMP%、%TMP%、AppData\Local\Temp、%SystemRoot%\Temp，以及应用数据区（Local、Roaming、LocalLow）里名为 temp/tmp、且不在任何已知根内的目录。
- 名字必须是 temp/tmp；不能是盘根；不能等于或包含用户目录、AppData、ProgramData、Program Files、Windows；必须通过系统根黑名单。C:\Windows\Temp 已被配置根 `windows-temp` 明确允许。

**扫描：**
- 临时文件夹内的记录只累加数量和大小，不分类、不读内容、不进候选列表。
- 每个条目只做一次挖除判断：DevClean 数据目录、文件夹内的其他已知根、用户 KEEP 规则、应用规则（如 Cursor 更新恢复安装包、NuGet scratch、Claude temp、npm 项目元数据）。命中的条目挖出来按原流程分类。
- 结束时每个临时文件夹生成一个“安全可清理”的目录条目（`DirectoryScope.TEMP_FOLDER`），默认勾选，大小来自汇总。

**删除：**
- 复核：
  - 文件夹身份与扫描时一致；
  - 不在系统根黑名单；
  - 在扫描根内（文件夹本身就是扫描根时以自身为边界）；
  - 文件夹本身不是运行中程序的目录。
- 清空过程（`purge_temp_folder_contents`）：
  - 用不含删除权限的句柄钉住文件夹，保留文件夹本身；
  - 每个子项逐个用句柄删除并做边界校验，删除时同样的挖除判断再做一次，并加上“运行中的程序或服务在使用”；
  - 被占用（32/33）、无权限（5）、目录非空（145）的条目跳过并计数；
  - 链接只删链接本身；链接替换、越界等安全错误仍然中止该项。
- 汇报：
  - 没清干净的文件夹留在列表，原因列写“已清理 N 个文件（X），留下：正被程序使用 a 项、当前账户没有删除权限 b 项（可能需要管理员）、由应用规则或你的保留设置单独处理 c 项”；
  - 回收站模式下明确拒绝（不释放空间）。
- 测试：`test_temp_folders_are_one_safe_item_emptied_around_what_others_claim`（端到端），`tests/test_temp_folders.py`（识别范围、占用跳过、挖除、链接、替换拒绝）。

## 6. 清理拒绝 (B)

- **(B1) “所属程序正在运行”过宽：**
  - **放开的：** 经审计的可再生缓存（各浏览器/编辑器 `_SAFE_CACHE_RULE_IDS`：HTTP/代码/GPU/着色器缓存、Chrome 更新器 crx_cache 等，以及 Claude 插件缓存里无人引用的暂存/孤儿版本）在程序运行时不再整项拒绝。
  - **仍要求先关闭程序（“所属程序正在运行，请关闭后再清理”）：** IDE 索引缓存（IDE 运行中持续维护）；编辑器可能正在安装使用的下载（VSIX、WSL 服务器下载）；其他要求关闭程序的规则（如 Claude 会话的工作目录标记）。
  - 整目录删除默认改为 `purge_directory_tree_skipping_in_use`：被占用或无权限的文件跳过并计数，其余删除，目录清空后连目录一起删除；原严格版本 `purge_exact_directory_tree` 保留不变。
  - 扫描分类最初没有跟上这条决定，仍把运行中程序的缓存标为“应用仍在运行”；`472364d` 之后补上，分类和删除前检查现在用同一个判断，见 §11.3。
  - **因用户决定而改写断言的 8 个测试：** Chrome、Brave、Edge、Opera、Vivaldi、Trae、VS Code、Windsurf 的进程守卫测试。缓存路径从“运行中拦下”改为“运行中允许、由删除逐个跳过被占用文件”；同测试中的日志、崩溃转储、WSL 下载仍断言拦下。新测试 `tests/test_cleanup_in_use.py`、`test_a_cache_tree_keeps_only_what_a_running_program_holds`。
- **(B2) crx_cache 分类与删除矛盾：**
  - 系统根黑名单提为共用模块 `core/system_anchor.py`。分类时落在 Windows/Program Files/ProgramData 等位置、且未被配置根允许的条目不再列为可清理，原因写“位于 Windows 系统保护位置…”。
  - 已受保护的条目加 `system_location` 标签，AI 路由和学到的 DELETE 规则都不能再把它抬升。
  - `Program Files (x86)\Google\GoogleUpdater\crx_cache` 因此不再出现在“安全可清理”，用户目录下的 GoogleUpdater\crx_cache 照常可清理。
  - 测试：`test_nothing_in_a_deny_listed_system_folder_is_offered`。
- **(B3) 未删除的条目留在列表：**
  - 原逻辑已保留失败项并在原因列显示“上次清理未完成：…”，本轮补测试确认；部分完成的目录同样留下并写明留下了什么。
  - 测试：`test_rows_that_were_not_removed_stay_listed_with_their_reason`。

## 7. 性能前后（Linux，同一生成树）

| 文件数 | b156d9b | 本轮 | 加速 |
| --- | --- | --- | --- |
| 18,800 | 39.6～46.6 秒 | 6.2～6.5 秒 | 约 6.5 倍 |
| 37,600 | 80.9～84.7 秒（另有一次 241 秒，机器波动） | 11.5～12.5 秒 | 约 7 倍，且随文件数线性 |

| 指标（18,800 文件） | b156d9b | 本轮 |
| --- | --- | --- |
| 读取内容的文件打开 | 683 | 683 |
| stat（Linux 夹具每文件 1 次） | 42,493 | 42,493 |
| scandir | 12,551 | 11,918 |
| 进度事件 | 120 | 31 |
| 预览事件 | 28 | 5 |
| 最终分区（界面线程） | 0.20 秒 | 0（已移到扫描线程） |

- **结果对比：** 与 b156d9b 相比，“待决定”路径集合完全相同；“可删除”只多了 Temp 文件夹这一项（用户决定）。
- **用户实测的推断：** 110 分钟约合每个文件 16 毫秒。按本轮 Linux 上约 0.3 毫秒/文件，再考虑 Windows 上 Python 稍慢、目录身份读取需要打开句柄，推断普通 C 盘应在几分钟级，**未经 Windows 实测**。

## 8. 验证（Linux）

| 检查 | 结果 |
| --- | --- |
| `ruff check src scripts tests` | 通过 |
| `python -m mypy --platform win32` | 通过（363 个文件） |
| 全量 pytest，b156d9b 等价工作树 | 232 失败 / 930 通过 / 30 跳过 |
| 全量 pytest，本轮 | 232 失败 / 949 通过 / 30 跳过 |
| 失败集合 | 与基线完全相同，失败信息逐字一致；均为既有的 Windows 路径语义测试。多出的 19 个通过为本轮新增测试 |

Linux 夹具上 POSIX 目录的 `st_nlink` 改报 1，与 Windows 目录一致（它数的是子目录，不是硬链接），只影响非 Windows 的元数据读取。

## 9. 未在 Windows 上实测

1. 扫描耗时（§7 的推断）、“未响应”是否消失、切换窗口后的响应。
2. 句柄版的清空和整目录删除：真实共享冲突时的跳过、`ERROR_DIR_NOT_EMPTY` 的处理、文件夹本身保留与删除。Linux 上测的是同语义的 POSIX 实现；Windows 上的占用测试（真实打开文件）在 `472364d` 的 CI 上首次运行并暴露了 §11.1 的缺陷。
3. C:\Windows\Temp 在普通账户下的表现：预期大部分文件“没有删除权限”被跳过并提示可能需要管理员。
4. Chrome/Claude 运行时的实际清理效果：哪些文件被锁、跳过数量。
5. Defender 实时扫描对 exe 内容探测的影响（读取次数没有变化）。
6. 界面在真实 Tk 下的差异刷新效果（测试用的是 Tk 替身）。

## 10. 改动文件

| 类型 | 文件 |
| --- | --- |
| 生产 | `core/application_cleanup.py`、`core/_application_cleanup_impl.py`、`core/_user_rules_impl.py`、`core/claude_cleanup.py`、`core/claude_native_cleanup.py`、`core/cursor_cleanup.py`、`core/cursor_updater_cleanup.py`、`core/installer_files.py`、`core/postscan_cleanup.py`、`core/review_routing.py`、`core/triage.py`、`core/system_anchor.py`（新）、`core/temp_folders.py`（新）、`platform/windows/exact_cleanup.py`、`platform/windows/filesystem.py`、`ui/app.py`、`ui/product_app.py`、`ui/product_vendor_app.py` |
| 测试（新） | `tests/test_temp_folders.py`、`tests/test_scan_hot_path.py`、`tests/test_scan_ui_refresh.py`、`tests/test_cleanup_in_use.py` |
| 测试（新增用例） | `tests/test_deletion_workflow.py`（临时文件夹端到端、扫描线程交接） |
| 测试（按用户决定改写断言） | `tests/test_{chrome,brave,edge,opera,vivaldi,trae,vscode,windsurf}_cleanup.py` 的进程守卫用例 |

## 11. `472364d` 的 Windows CI 失败修复（2026-11-11）

| 项 | 内容 |
| --- | --- |
| CI | run 42973479185：4 失败 / 1207 通过（日志 `/workspace/devclean-ci-472364d-failure.log`） |
| 基线 | `472364d`（已推到 origin/main），在其上继续改，没有 revert |
| 范围 | 只修这 4 个失败；为满足失败 4 的要求而必须同步的测试见 §11.3 |
| 提交状态 | 随本节改动一并提交、推送，由 Windows CI 构建 |
| 审计原文 | 未改动，SHA-256 仍为 `a078457ee2be031482f1f15073491b82976b4dcbf5aceeeface413794fc4ce0f` |

### 11.1 失败 1、2：被占用的文件让整个清理中止（生产缺陷）

- `tests/test_temp_folders.py::test_a_file_another_program_holds_open_is_left_and_the_rest_goes`
- `tests/test_temp_folders.py::test_a_cache_tree_keeps_only_what_a_running_program_holds`

**根因：**
- `exact_cleanup.py` 的 `_raise_windows_error` 用 `OSError(GetLastError(), 消息, 路径)` 抛错：Windows 错误码落进 `errno`，`winerror` 为 None；32 还会被 Python 变成 `BrokenPipeError`。
- `_TempPurgeState.skip()` 只在 `winerror` 上认 32/33/5/145。`winerror` 缺失时，它只拿 `errno` 比 POSIX 的 EBUSY/EACCES 等值，所以“文件正在使用”（32）被当作未知错误重新抛出，整个目录中止。
- Linux 上没暴露：测试用 `EBUSY` 模拟占用，走的是 POSIX 分支。

**修复：**
- `skip()` 先取 `winerror`；缺失时从 `errno` 读同一个 Windows 错误码，再按 32/33（占用）、5（无权限）、145（目录非空）、2/3（已消失）归类。POSIX 的判断保留不变。
- 安全错误（`ExactCleanupError`，如链接替换、越界）和未知错误仍然中止。
- 没有改 `_raise_windows_error` 的抛出方式：别处有 `errno == 2` 的判断，也有按异常类型分支的代码，改了会波及这 4 个失败之外的路径。
- 两条路径都修好了：临时文件夹清空（`purge_temp_folder_contents`）和程序运行时的整目录删除（`purge_directory_tree_skipping_in_use`）。两者的 7 处跳过都经过同一个 `skip()`。
- 数字重叠：这几个码里只有 5 在 POSIX 上另有含义（EIO）。POSIX 上遇到 EIO 的条目也会被留下并计数，不会多删任何东西；正式产品只在 Windows 上运行。

**测试（Linux 可复现）：**
- 两个失败测试在 Linux 上的占用模拟改为抛出与 Windows 相同的错误形态：`OSError(32, …)`，`winerror` 为 None（原来是 `EBUSY`）。
- 新增 `test_entries_in_use_or_without_rights_are_skipped_in_either_error_shape`（11 组）：同时覆盖“Windows 码在 errno、无 winerror”和 POSIX 两种形态。
- 新增 `test_safety_and_unknown_errors_still_stop_the_cleanup`（2 组）：安全错误和未知错误仍然中止。
- 变异检查：把 `exact_cleanup.py` 换回 `472364d` 版，Linux 上 7 个用例失败，包括 CI 的这两个；换回修复版全部通过。
- 计数推演（Windows 分支，未实跑）：被占用的文件让所在目录进入 `retained`，目录本身不再尝试删除，所以不会再多记一次“目录非空”。两个测试期望的“占用 1 项”在 Windows 上也成立。

### 11.2 失败 3：Claude 插件缓存进程守卫测试没跟上 B1（测试）

- 原名 `test_process_guard_requires_claude_closed_before_plugin_cache_cleanup`，改名为 `test_unreferenced_plugin_clones_are_cleaned_around_a_running_claude`，与那 8 个测试同一标准。
- **Claude 运行时：**
  - 无人引用的暂存克隆和孤儿版本：由“拦下”改为“允许”；删除时被占用的文件逐个跳过。
  - 新增一项必须先关闭 Claude 才能动的条目（Claude 会话的工作目录标记 `tmpclaude-*-cwd`），断言运行时拦下。
- **Claude 未运行时：** 三者都允许，原有断言保留。
- **Linux 上的情况：** 该测试属于既有的 Windows 路径失败基线（插件缓存模块只认 Windows 绝对路径），改前改后都在第一句暂存断言处以同样方式失败。
  - 我在 Linux 上直接验证了守卫：暂存/孤儿两个规则无论 Claude 是否运行都放行；工作目录标记在运行时拦下、关闭后放行。
  - CI 日志也显示，Windows 上 Claude 运行时暂存已被放行（原断言 `assert not True`）。

### 11.3 失败 4：扫描分类没跟上删除侧的 B1（生产缺口）

- `tests/test_scan_hot_path.py::test_nothing_in_a_deny_listed_system_folder_is_offered`

**根因：**
- B1 只改了删除前的进程守卫（`process_guard_refusal`）。扫描时，各应用模块仍按“规则要求关闭程序且程序在运行”给出 `TOOL_KEEP_IN_USE`，分类成“应用仍在运行，避免清理正在写入的数据”。
- 真机影响：Chrome 开着时，它的缓存文件根本不会出现在可清理列表里。整树目录条目一直按“程序未运行”评估，不受影响。
- 测试结果取决于平台：测试套件禁止启动外部程序，Windows 上 `chrome_process_running()` 调不了 PowerShell，按“运行中”保守处理；Linux 上直接返回“未运行”。这条测试没有设定进程状态，所以 Windows 上失败、Linux 上通过。

**修复：** 分类和删除前检查用同一个判断。

| 位置 | 改动 |
| --- | --- |
| `application_cleanup.must_close_before_cleanup(rule)`（新） | `rule.requires_process_closed and rule.rule_id not in cleanable_while_running()`，扫描和删除前检查共用 |
| `evaluate_application_path`（扫描入口） | 某应用给出 `TOOL_KEEP_IN_USE`、而该规则不必关闭程序时，按“程序未运行”重新评估同一路径。闲置天数、最小释放量等其他门槛照旧，不是一律可删 |
| `process_guard_refusal`（删除前） | 改用 `must_close_before_cleanup`，结果不变 |
| `triage` 可清理条目 | “执行前必须确认 X 已关闭”和 `requires_process_closed` 标签只给真正必须关闭的规则 |
| `triage._inside_system_location` | 系统禁删位置里原本因“应用仍在运行”而不能删的条目，原因改写为禁删名单（持久的原因）；其他不能删的原因不变，只加标签 |

- 结果：
  - 用户目录下的 crx_cache 在更新器运行时也列为可清理；
  - `Program Files (x86)` 下的 crx_cache 列为不能删除，原因是“位于 Windows 系统保护位置…”；
  - IDE 索引、编辑器 VSIX/WSL 下载、Claude 会话标记等仍是“应用仍在运行”。

**测试：**
- **失败 4 这条测试：** 按“程序关闭/运行”参数化，显式设定 Chrome 和 Claude 的进程探测，结果不再依赖机器。
  - 两种状态下：用户目录的 crx_cache 可清理；系统目录的被禁删名单拒绝；原因里都没有“应用仍在运行”或“已关闭”。
  - 新增一项：系统目录里的 Claude 会话标记，原因也是禁删名单。
- **新增 `test_the_scan_lists_what_the_pre_delete_guard_lets_go_while_programs_run`：** 程序运行时，同一批路径分别走扫描和删除前检查：
  - 3 个 Chrome 缓存都可清理，且守卫放行；
  - Claude 会话标记为“应用仍在运行”，且守卫拦下。
- **为满足本项要求而同步的 10 个既有测试：** 它们在 `process_running=True` 下断言扫描入口对审计过的缓存返回 `TOOL_KEEP_IN_USE`，与用户要求的“扫描时就要列为可清理”直接矛盾。
  - 只把这一句改为 `TOOL_DELETE`，其余断言不动：Opera 系统缓存仍受保护，恢复副本、Vivaldi 站点存储仍由用户决定，崩溃转储仍受保护，未运行时的判定照旧。
  - 名字里写着旧行为的测试改了名。
  - 三个编辑器各补一句：运行时扩展安装包（VSIX）仍是 `TOOL_KEEP_IN_USE`。

  | 文件 | 原测试名 → 新名 |
  | --- | --- |
  | `test_chrome_cache_safety_semantics.py` | `test_chrome_process_activity_still_blocks_cache_cleanup` → `test_chrome_cache_is_offered_while_the_browser_runs` |
  | `test_brave_cache_safety_semantics.py` | `test_brave_process_activity_still_blocks_cache_cleanup` → `test_brave_cache_is_offered_while_the_browser_runs` |
  | `test_edge_cache_safety_semantics.py` | `test_edge_process_activity_still_blocks_cache_cleanup` → `test_edge_cache_is_offered_while_the_browser_runs` |
  | `test_firefox_cache_safety_semantics.py` | `test_firefox_process_activity_still_blocks_cache_cleanup` → `test_firefox_cache_is_offered_while_the_browser_runs` |
  | `test_vscode_cache_safety_semantics.py` | `test_vscode_process_activity_still_blocks_generated_cache_cleanup` → `test_vscode_generated_cache_is_offered_while_the_editor_runs` |
  | `test_vivaldi_opera_cache_safety_semantics.py` | `test_opera_process_system_cache_and_recovery_boundaries_are_unchanged` → `test_opera_running_cache_is_offered_and_other_boundaries_are_unchanged` |
  | `test_vivaldi_opera_cache_safety_semantics.py` | `test_vivaldi_process_and_protected_boundaries_are_unchanged` → `test_vivaldi_running_cache_is_offered_and_protected_boundaries_are_unchanged` |
  | `test_trae_cleanup.py` | `test_trae_known_electron_cache_is_tool_owned_and_process_guarded` → `test_trae_known_electron_cache_is_tool_owned_and_offered_while_trae_runs`（补 VSIX） |
  | `test_vscode_cleanup.py` | `test_vscode_cache_is_tool_owned_and_process_guarded` → `test_vscode_cache_is_tool_owned_and_offered_while_the_editor_runs`（补 VSIX） |
  | `test_windsurf_cleanup.py` | `test_windsurf_electron_caches_are_tool_owned_and_process_guarded` → `test_windsurf_electron_caches_are_tool_owned_and_offered_while_it_runs`（补 VSIX） |

- **变异检查（Linux）：**
  - 去掉扫描入口的重新评估：失败 4 测试的“运行”组和扫描/守卫一致性测试失败。
  - 原因文字仍按 `requires_process_closed`：3 个失败。
  - 去掉系统位置的原因改写：失败 4 测试的“运行”组失败。
- **模拟 Windows 进程探测（Linux）：** 把所有 `*_process_running` 探测强制为“运行中”，与 Windows 测试机上探测保守返回的效果相同，然后跑全量：
  - `472364d` 上失败 4 复现；本轮工作树上通过。
  - 其余失败集合与失败信息两边一致；唯一差别是 §11.2 改名的那条 Linux 基线测试。
  - 由此确认，本轮之前的扫描测试在两种进程状态下结果相同。
  - 30 个仅 Windows 运行的测试里，Docker 那条自己设定了进程状态；npm/pnpm 剪枝的不在“运行时可清理”名单内，不受影响。

### 11.4 验证（Linux）

| 检查 | 结果 |
| --- | --- |
| `ruff check src scripts tests` | 通过 |
| `python -m mypy --platform win32` | 通过（363 个文件） |
| 全量 pytest，`472364d` | 232 失败 / 949 通过 / 30 跳过 |
| 全量 pytest，本轮 | 232 失败 / 964 通过 / 30 跳过 |
| 失败集合 | 与 `472364d` 相同，失败信息一致；唯一差别是 §11.2 改名的那条（同一 Linux 基线原因，同一行失败） |
| 多出的 15 个通过 | 本轮新增用例：错误形态 11 组、安全/未知错误 2 组、失败 4 测试的“运行”组、扫描/守卫一致性测试 |
| 审计原文 SHA-256 | `a078457e…4fc4ce0f`，未变 |

`ruff format --check` 会报 `test_trae_cleanup.py`、`test_vscode_cleanup.py`、`test_windsurf_cleanup.py` 三个文件，它们在 `472364d` 上就是这样，本轮没有处理。

### 11.5 仍然只能在真实 Windows 上验证

1. **失败 1、2 的真实句柄行为：** Linux 只模拟了错误形态；计数是按代码推演的（§11.1）。需要 Windows CI 跑过才算确认。
2. **失败 3 的 Claude 运行分支：** 插件缓存只认 Windows 路径，Linux 上只验证了守卫本身。
3. **真机效果：** Chrome/Edge/编辑器开着时，它们的缓存是否列为可清理；删除时实际跳过多少被占用文件。
4. **相关但没改的一处：** 界面把删除失败转成原因文字的 `_reason_of`（`ui/app.py`）同样只在 `winerror` 上认 5/32/33。`_raise_windows_error` 抛出的占用错误如果从其他删除路径冒出来（如严格版整树删除），原因列不会写“被程序占用或权限不足”。不在这 4 个失败之内，未改。
5. §9 中其余各项仍然有效。

### 11.6 本轮改动文件

| 类型 | 文件 |
| --- | --- |
| 生产 | `platform/windows/exact_cleanup.py`（`skip()`）、`core/application_cleanup.py`（`must_close_before_cleanup`、扫描入口、删除前检查）、`core/triage.py`（原因文字、系统位置原因） |
| 测试（失败 1、2） | `tests/test_temp_folders.py` |
| 测试（失败 3） | `tests/test_claude_plugin_cache_cleanup.py` |
| 测试（失败 4） | `tests/test_scan_hot_path.py`、`tests/test_cleanup_in_use.py`，以及 §11.3 表中的 10 个测试 |

### 11.x 界面失败原因（核查时补）

- `ui/app.py` 的 `_reason_of` 原来只看 `winerror`；`_raise_windows_error` 抛出的错误 Windows 代码在 `errno`、`winerror` 为空（32 变成 `BrokenPipeError`，3 变成 `ProcessLookupError`），会显示成“其他（…）”。
- 现在 `winerror` 为空且错误类型是 `OSError`/`BrokenPipeError`/`ProcessLookupError` 时，按 `errno` 读 Windows 代码，与 `_TempPurgeState.skip()` 一致；其他子类不受影响。
- 测试：`tests/test_cleanup_safety_fixes.py::test_windows_codes_carried_in_errno_are_named_like_winerror`。

## 12. 一项删不掉就跳过、其余照删（`c20d81a` 用户实测，2026-11-11）

| 项 | 内容 |
| --- | --- |
| 基线 | `c20d81a`（HEAD，已推送），在其上继续改 |
| 用户实测 | 扫描总耗时 12 分 52 秒；勾选 21/23 项后清理，状态栏：“已清理 6 项；15 项未完成；所属程序正在运行，请关闭后再清理 14 项；已清理 0 个文件（0 B），留下：正被程序使用 2 项 2 项；已清理 0 个文件（0 B），留下：正被程序使用 5 项 2 项；安全检查拒绝（fresh whole-tree policy s…”（被截断）；`AppData\Local\Temp`（1.4 GB）未勾选 |
| 提交状态 | 未 commit、未 push、未构建 |
| 审计原文 | `docs/cleanup-safety-audit-2026-11-07.md` 未改动，SHA-256 仍为 `a078457ee2be031482f1f15073491b82976b4dcbf5aceeeface413794fc4ce0f` |
| 运行环境 | 同 §8；**本轮没有在 Windows 上运行任何东西** |
| 测试数据 | 只用 `tmp_path` 和会话临时目录；没有删除真实用户文件 |

### 12.1 用户决定（原样记录）

- 清理时，哪一项或哪个文件删不掉，就跳过它，继续删后面的。能删的全部删掉，最后列出删不掉的东西和原因。不要因为一项或一个文件删不掉，就停掉整项或整批。
- 程序在运行时也要照常清理，被占用的文件逐个跳过并记录。只有删除后确实会损坏正在运行的程序的东西（例如 IDE 索引、正在下载安装的包），才可以要求先关闭，而且必须逐条写明理由。
- 临时文件夹默认勾选（重申 §1）。

### 12.2 根因

#### (1) “所属程序正在运行，请关闭后再清理” 14 项

- **唯一来源：** 删除前检查 `process_guard_refusal` 返回 `RUNNING_OWNER_REFUSAL`。条件是三者同时成立：
  - 匹配到的规则 `requires_process_closed=True`；
  - 规则不在 `cleanable_while_running()` 白名单里；
  - 所属程序正在运行。
- **白名单太窄：** §6 B1 只放进了 9 个浏览器/编辑器模块的审计缓存和 Claude 插件的两条规则。其余约 90 条要求关闭程序的规则（Codex 桌面版、Cursor、Claude、各类日志、崩溃报告……），程序一运行就整项拦下。默认仍是“拦”，正是用户说的“笼统地拦整项”。
- **逐项对照截图上看得到的行**（在 Linux 上把 Chrome、Codex 的进程探测设为“运行中”，对 `c20d81a` 和本轮分别跑删除前检查）：

  | 截图行 | 规则 | `c20d81a` | 本轮 |
  | --- | --- | --- | --- |
  | `Chrome\User Data\Default\C…`（311.1 MB，`Cache` 或 `Code Cache`） | `chrome-http-cache` / `chrome-code-cache` | 放行 | 放行 |
  | `Chrome\User Data\GPUPersi…`（6.6 MB） | `chrome-gpu-persistent-cache` | 放行 | 放行 |
  | `Packages\OpenAI.Codex_2p2nqsd0c7…` 三行（36.5 / 34.9 / 11.0 MB） | `codex-desktop-browser-cache`、`codex-desktop-default-browser-cache` | **拦下** | 放行 |
  | `.codex\.tmp\plugins`（20.9 MB） | `codex-plugin-marketplace-snapshot` | **拦下** | 放行 |

  - 任务说明里举的“Chrome 的 Default\Cache 和 GPU 缓存”，在 `c20d81a` 上**不会**被“程序正在运行”拦：它们都在白名单里。截图里这两行留在列表上，原因只能是 (2) 的“已清理 0 个文件…留下”或 (3) 的整树复检拒绝。
  - 14 项里其余看不到的行，截图上没有，无法逐行确认。按代码，Codex 运行时会被拦的还有它的桌面版其他缓存目录、崩溃报告、旧 TUI 日志、孤儿临时文件；其他程序运行时被拦的见 §12.4 表二。

#### (2) “已清理 0 个文件（0 B），留下：正被程序使用 N 项”

查了两个问题，结论如下：

- **不是“遇到第一个被占用的子项就停”：** 同一目录里，一个文件删不掉会计数后 `continue`，接着删下一个。
- **但有四处把整个目录当成一个单位：**
  1. 子目录打不开删除句柄时（另一个程序以不共享删除的方式开着它，比如当作工作目录或监视目录；或者有写句柄；或者权限不允许删除这个目录），整个子目录计 1 项“正被程序使用”，**里面的文件一个都不再尝试**。
  2. 被选中的目录本身打不开删除句柄时，整项直接失败（“被程序占用或权限不足”），什么都不删。
  3. 某个子目录列不出内容时，异常一路抛出，整项中止。
  4. 程序自己在清理途中删掉了某个文件（文件已消失），它所在的目录仍被当作“留下了东西”，目录删不掉。如果此外没有任何计数，整项会被判为“tree deletion stopped before the whole tree was removed”。
- **另外两处误报：**
  - 只读属性的文件（缓存里的 git 对象包等）被报成“当前账户没有删除权限”。它们不是被占用，用户要求能删的都删掉。
  - 状态栏的“已清理 6 项”把一个文件都没删的项也算成已清理：只删了一部分的项按“完成”计数。
- **关于“5 项”和“2 项”：**
  - Chromium 的块文件缓存（GPUCache、ShaderCache、GrShaderCache、Dawn*Cache 等）通常正好是 `index` 加 `data_0`～`data_3` 五个文件，浏览器运行时全部开着。这种目录“删 0 个、留 5 个”是正确结果，没有别的可删。
  - “2 项”符合“两个子目录打不开删除句柄、里面的文件没试”的情形，例如 `Code Cache\js` 与 `Code Cache\wasm`。这是推断，截图上无法确认；上面四处无论如何都已修复。

#### (3) “安全检查拒绝（fresh whole-tree policy s…”

- **完整原因：** `fresh whole-tree policy scan was incomplete at <路径>`。截断处另一种可能是 `fresh whole-tree policy scan has no reliable last-use timestamp`，它只在整个目录什么都读不到时出现。
- **触发路径：** 状态栏被窗口宽度截断，路径看不到；以下按代码说明。
- **来源：** `whole_tree_policy._fresh_tree_evidence`。删除整目录应用缓存之前（`_preflight_candidate`，以及构造删除对象时），它把整棵树重新走一遍，任何一个条目产生错误记录，就拒绝整项。
- **错误记录的来源：**
  - 子目录在列出和读取之间被删掉；
  - 子目录在遍历中被改名或替换（`directory identity changed during scan`）；
  - 子目录没有列出权限。
- 程序运行时，浏览器会不断新建和删除缓存子目录，这种竞争很容易出现。
- **这条复检不是安全底线：** 同一模块明确写着年龄、大小不是安全门槛；真正的边界（链接不跟随、句柄最终路径不出界、身份不变）由删除时逐个条目检查。

#### (4) Temp 没有勾选

- **代码上默认是勾选的：** 扫描发布时，“安全可清理”的每一行都勾选，包括 Temp。在 Linux 上用真实扫描线程加窗口发布流程复现：Temp 行已勾选。
- **能让它变成未勾选的路径：**
  - 点它的方框；
  - “清空选择”；
  - 之后的重新发布会保留用户的取消；
  - **双击方框（新发现）：** Tk 把双击的第一下当单击（切换勾选），第二下只交给双击处理（打开资源管理器）。结果是方框被切换了一次，还弹出一个窗口。界面提示正是“双击路径可在资源管理器中确认”。
- **截图本身：** 清理前 25 行里已有 2 行未勾选，Temp 是其中之一；它不在这次清理的结果里，说明清理前就没勾。截图无法判断是哪种操作造成的。
- **处理：** 双击方框是唯一一种不是有意取消的途径，已修复；**未能证实它就是原因**。

#### (5) 失败原因看不全

- 状态栏是单行标签，没有设置换行宽度，超出窗口宽度的部分直接被裁掉。
- `_reason_of` 把原因截到 120 个字符。
- 表格的“原因”列在一个会拉伸的“位置”列右边，没有横向滚动条，实际上看不到。
- 状态栏按原因文字分组。每个“部分清理”项的说明文字都不一样，于是出现“…正被程序使用 2 项 2 项”这种重复。

### 12.3 修复

| 问题 | 改动 | 位置 |
| --- | --- | --- |
| (1) | “运行时可清理”改为逐条决定：要求关闭程序的规则，要么放进 `cleanable_while_running()`，要么放进 `_MUST_CLOSE_REASONS`（每条都有写给用户的理由，见 §12.4）。新测试要求两者覆盖全部要求关闭程序的可删规则、互不重叠、理由非空。新出现而没有归类的规则仍按“拦下”处理（保守），测试会提示归类 | `core/application_cleanup.py` |
| (1) | 拦下时说清楚是谁、为什么：新增 `process_guard_detail` 返回 `RUNNING_OWNER_REFUSAL: <程序>: <规则>`，界面显示“Codex 正在运行，这一项要等它关闭后再清理：<理由>”。`process_guard_refusal` 返回值不变 | `core/application_cleanup.py`、`core/postscan_cleanup.py`、`ui/app.py` |
| (1) | 扫描分类的原因文字带上同一条理由（“应用仍在运行…（理由）”“执行前必须确认 X 已关闭（理由）”）；扫描和删除前检查仍共用 `must_close_before_cleanup` | `core/triage.py` |
| (2)-1 | 子目录打不开删除句柄：计 1 项留下后，改用只列目录、不含删除权限、仍不共享删除（钉住不被替换）的句柄进入，逐个删除里面的文件；这个子目录本身保留。连列目录的句柄都打不开，才整个留下 | `platform/windows/exact_cleanup.py` `_empty_temp_folder` |
| (2)-2 | 被选中的目录本身打不开删除句柄：同样用只列目录的句柄清空内容、保留目录，计 1 项留下。连列目录的句柄都打不开，仍整项失败（原行为） | `_purge_tree_skipping_in_use_windows` |
| (2)-3 | 子目录列不出内容：只留下这个子目录并计数，其余照删。POSIX 实现同样处理（含列目录失败、读条目信息失败） | `_empty_temp_folder`、`_purge_temp_folder_contents_posix` |
| (2)-4 | 已自行消失的条目不再让上级目录“留下”，也不计数 | `_is_gone` |
| (2) | 只读文件：整目录和临时文件夹清理时，删除请求带 `FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE`（Windows 10 1809 起支持；更早的系统返回“参数无效”，走原来的基本删除，只读文件仍报“没有删除权限”）。单文件删除和严格版整树删除不变 | `_delete_leaf`、`_set_delete_disposition` |
| (3) | 删除前整树复检：树里某个条目读不到，不再拒绝整项，跳过它继续统计；只有被选中的目录本身读不到才拒绝（“删除前复检：这个目录本身无法再次读取（原因）”）。规则身份复检（仍是同一条可整目录删除的 TOOL 规则）不变 | `core/whole_tree_policy.py` |
| (3) | 安全底线只作用于出问题的那一项：清理途中某个条目变成链接、最终路径出了边界、被改名或替换（`ExactCleanupError`），只跳过该条目，计入“安全检查未通过、只跳过它们自己 N 项”，其余照删。被选中目录本身的身份、边界、链接检查和系统禁删名单仍拒绝整项 | `exact_cleanup.py`（`refuse`、`unsafe` 计数）、`postscan_cleanup.py` |
| (4) | 双击方框改为再切换一次（与连点两下一致），不再打开资源管理器；双击其他列照旧打开 | `ui/app.py`、`ui/product_vendor_app.py` `_on_row_double_click` |
| (4) | 新测试走真实扫描线程加窗口发布：Temp 行默认勾选；清理后部分留下仍勾选；重新发布后仍勾选 | `tests/test_deletion_workflow.py` |
| (5) | 状态栏按窗口宽度自动换行，不再裁掉 | `ui/modern_app.py`、`ui/app.py`（`wrap_to_width`） |
| (5) | 状态栏改为：“已清理 N 项；M 项删了能删的部分，被占用等删不掉的留下；释放 X；K 项未完成；未完成原因：…；删了一部分的项共留下：正被程序使用 a 项、…；每一项删了多少、留下多少、为什么留下，见「清理结果」”。一个文件都没删的项不再算“已清理” | `ui/app.py` `_report_deletion` |
| (5) | 新增“清理结果”窗口：清理后有留下或失败时自动打开，也可用“永久清理所选”旁的「清理结果」按钮重新打开。逐项列出：全部删除的（文件数、大小）；删了一部分的（删了多少、留下多少及原因、最多 20 个留下的例子）；未删除的（完整原因）。文字可选中复制 | `ui/app.py`（`cleanup_outcomes`、`render_cleanup_report`、`open_cleanup_report`）、`ui/product_vendor_app.py` |
| (5) | 原因不再截断；整树复检的拒绝改为中文说明 | `ui/app.py` `_reason_of` |
| (5) | 执行结果新增 `removed`（每项删了多少文件、多少字节），供逐项报告使用 | `core/postscan_cleanup.py` |

### 12.4 程序运行时的逐条决定

**表一：仍要求先关闭程序（`_MUST_CLOSE_REASONS`，界面显示同样的理由）**

| 规则 | 理由 |
| --- | --- |
| `android-avd-temporary-partition` | 运行中的模拟器把它当作磁盘分区挂载读写，删掉会损坏模拟器里的系统 |
| `android-sdk-install-temp` | SDK 管理器可能正在这里下载、解压要安装的包，删掉会让安装失败 |
| `toolbox-download-cache` | Toolbox 可能正在用这里下载的安装包安装或更新 IDE |
| `toolbox-install-temp` | Toolbox 安装 IDE 时在这里解压 |
| `vscode-extension-vsix-cache`、`trae-cached-extension-vsix`、`windsurf-extension-vsix-cache`、`cursor-local-cached-extension-vsix`、`cursor-roaming-cached-extension-vsix` | 编辑器可能正在从这里安装扩展 |
| `vscode-wsl-server-download-cache` | VS Code 可能正在把这里下载的服务器装进 WSL |
| `cursor-superseded-windows-update-staging` | 规则也覆盖“没有安装包的暂存目录”，它可能就是正在下载的更新（见 `cursor_updater_cleanup` 说明） |
| `jetbrains-index-cache`、`android-studio-index-cache` | IDE 运行时持续读写项目索引 |
| `jetbrains-vcs-log-cache`、`android-studio-vcs-log-cache` | IDE 运行时持续维护版本控制日志的索引 |
| `jetbrains-system-temp`、`android-studio-system-temp` | IDE 运行时把正在使用的解包文件和中间结果放在这里 |
| `visual-studio-component-model-cache` | Visual Studio 运行时使用并会在退出时写回这份组件缓存 |
| `visual-studio-roslyn-analyzer-cache` | 运行中的 Visual Studio 从这里加载代码分析器 |
| `claude-cwd-temp-marker` | 运行中的会话用它记录工作目录 |
| `claude-session-locks` | 运行中会话的锁，删掉后两个 Claude 可能同时操作同一会话 |
| `claude-session-env` | 运行中的会话要读取这里保存的环境信息 |
| `claude-shell-snapshots` | 运行中的会话每次执行命令都要读取 shell 快照 |
| `claude-task-state` | 运行中的会话在这里记录后台任务状态 |
| `claude-temp-scratch` | 运行中的会话把正在进行的任务和临时输出放在这里 |
| `claude-plans` | 运行中的会话在计划模式下读写计划文件 |
| `claude-image-cache` | 运行中的会话还会引用这里的图片附件 |
| `claude-legacy-todos` | 运行中的会话可能还在这里记录待办 |
| `claude-remote-settings-cache`、`claude-policy-limits-cache` | 运行中的 Claude 按这份服务器下发的设置/策略运行，删掉后在重新获取前可能不按组织的设置运行 |
| `codex-plugin-sync-lock`、`codex-rollout-maintenance-lock`、`codex-rollout-compression-lock` | 运行中的 Codex 用这些锁防止两次同步或维护同时进行 |
| `pnpm-dlx-cache` | 正在运行的 `pnpm dlx` 命令从这里加载程序 |

这些文件大多不被一直开着，删除时的“被占用就跳过”保护不到，所以仍要求先关闭。

**表二：本轮起程序运行时照常清理（被占用的文件逐个跳过）**

| 规则 | 理由 |
| --- | --- |
| `codex-desktop-browser-cache`、`codex-desktop-default-browser-cache`、`cursor-{local,roaming}-{cache,cached-data,cached-extensions,code-cache,dawn-cache,gpu-cache,grshader-cache,shader-cache}`、`claude-cache` | 与已放开的 Chrome/VS Code 缓存同类，程序只会重新生成 |
| `codex-desktop-crashpad-reports`、`cursor-{local,roaming}-crashpad-{pending,reports}`、`vscode-crashpad-{pending,reports}` | 正在写的报告开着、会跳过；写完的不会被读回 |
| `claude-daemon-log`、`claude-debug`、`claude-legacy-logs`、`codex-legacy-tui-log`、`cursor-{local,roaming}-logs`、`vscode-logs`、`jetbrains-product-logs`、`toolbox-product-logs`、`npm-default-logs`、`npm-external-debug-logs`、`npm-legacy-debug-log`、`trae-logs`、`windsurf-logs`、`visual-studio-servicehub-logs` | 正在写的日志开着、会跳过；旧日志不会被读回 |
| `claude-config-backups`、`claude-legacy-statsig`、`codex-orphan-rollout-temp`、`claude-native-old-launcher`、`claude-native-stale-version` | 运行中的版本不再使用。正在运行的旧程序文件 Windows 本身就不允许删除，会报告出来 |
| `codex-plugin-marketplace-snapshot`、`codex-plugin-sync-marker`、`pnpm-metadata-cache`、`pnpm-update-state` | 程序会重新下载或重写的副本与标记 |
| `vscode-portable-temp` | 便携版的临时文件夹，与 %TEMP% 同样处理（§1 用户决定） |

- **与已有审计文档的关系：**
  - `docs/visual-studio-servicehub-logs-audit.md` 记录了“用户批准删除 ServiceHub 日志时 Visual Studio 必须关闭”。该文档没有给出损坏的理由，微软的指引本身就是让用户删除这个日志目录。
  - 按本次用户决定，ServiceHub 日志改为运行时照常清理；该文档未改动。
  - USER 类日志（npm、Trae、Windsurf、ServiceHub）仍需用户或 AI 批准才会删除，只是不再因为程序在运行而拦下。
- **存在的风险，需 Windows 实测：** `codex-plugin-marketplace-snapshot` 若恰好在 Codex 同步时被删，快照可能不完整。当时正在写的文件开着，会被跳过；Codex 之后能否自行重新同步，见 §12.7。

### 12.5 改写的测试断言（逐条理由）

以下 10 个文件的断言改动，理由都是 §12.1 的用户决定：程序运行时照常清理，只有表一的规则等待关闭。没有删除、跳过任何测试。

| 文件 | 测试（原名 → 新名） | 改动 |
| --- | --- | --- |
| `test_whole_tree_policy.py` | `test_incomplete_fresh_scan_fails_closed` → `test_an_unreadable_entry_no_longer_refuses_the_tree_but_an_unreadable_root_does` | 树中一个条目读不到：原断言整项拒绝，改为返回其余部分的统计（1 个文件、64 MiB）；新增断言：被选中目录本身读不到仍拒绝。理由是 §12.1 第一条和 (3) |
| `test_claude_cleanup.py` | `test_process_guard_allows_user_choice_blocks_keep_and_rechecks_tool_process`（未改名） | Claude 运行时 `debug\session.log`：拦下 → 放行（日志，表二）；同测试的 `sessions\*.json` 仍断言拦下 |
| `test_codex_legacy_log_cleanup.py` | `test_live_codex_process_blocks_legacy_tui_log_execution` → `test_legacy_tui_log_is_offered_while_codex_runs_but_its_locks_wait` | 运行时旧 TUI 日志：`TOOL_KEEP_IN_USE` → `TOOL_DELETE`；新增断言 `plugins.sync.lock` 运行时仍是 `TOOL_KEEP_IN_USE` |
| 同上 | `test_process_guard_rechecks_codex_before_legacy_log_cleanup`（未改名） | 运行时旧日志：拦下 → 放行；新增断言同步锁运行时拦下、关闭后放行 |
| `test_cursor_cache_safety_semantics.py` | `test_cursor_process_activity_still_blocks_generated_cache_cleanup` → `test_cursor_generated_cache_is_offered_while_cursor_runs` | 运行时 GPUCache：`TOOL_KEEP_IN_USE` → `TOOL_DELETE`；新增断言 VSIX 运行时仍是 `TOOL_KEEP_IN_USE` |
| `test_cursor_cleanup.py` | `test_cursor_known_cache_is_tool_owned_and_process_guarded` → `test_cursor_known_cache_is_tool_owned_and_offered_while_cursor_runs` | 运行时 Code Cache：`TOOL_KEEP_IN_USE` → `TOOL_DELETE`；新增 VSIX 断言同上 |
| 同上 | `test_cursor_process_guard_allows_user_database_but_rechecks_tool_cache`（未改名） | 运行时 `Cache\data_0`：拦下 → 放行；新增断言 VSIX 拦下 |
| `test_npm_cleanup.py` | `test_npm_process_guard_rechecks_live_npm_process_for_user_logs` → `test_npm_user_logs_are_cleaned_while_npm_runs` | 两个 npm 日志：拦下 → 放行 |
| `test_pnpm_cleanup.py` | `test_pnpm_update_state_is_blocked_while_pnpm_is_running` → `test_pnpm_update_state_is_offered_while_pnpm_is_running` | `TOOL_KEEP_IN_USE` → `TOOL_DELETE` |
| 同上 | `test_pnpm_user_cache_mutation_is_blocked_when_process_is_running` → `test_pnpm_dlx_waits_for_pnpm_but_its_metadata_mirror_does_not` | dlx 仍断言拦下（表一）；metadata：拦下 → 放行 |
| `test_trae_cleanup.py` | `test_trae_process_guard_allows_user_choice_and_cache_but_blocks_live_logs` → `test_trae_process_guard_allows_user_choice_cache_and_logs_but_not_crash_reports` | 运行时日志：拦下 → 放行；崩溃报告仍断言拦下（它是 KEEP，与运行无关） |
| `test_windsurf_cleanup.py` | `test_windsurf_process_guard_cleans_cache_but_blocks_running_user_logs` → `test_windsurf_process_guard_cleans_cache_and_user_logs_while_it_runs` | 同 Trae |
| `test_visual_studio_servicehub_logs.py` | `test_servicehub_user_approved_mutation_requires_visual_studio_closed` → `test_servicehub_user_approved_mutation_runs_while_visual_studio_runs` | 运行时：拦下 → 放行（见 §12.4 与审计文档的关系）；关闭时放行的断言不变 |

- `tests/test_temp_folders.py::test_safety_and_unknown_errors_still_stop_the_cleanup` 未改：它测的是 `_TempPurgeState.skip()` 对安全错误仍返回 False，这一点没变。遍历层现在用 `refuse()` 只跳过出问题的那一项。

### 12.6 验证（Linux）

| 检查 | 结果 |
| --- | --- |
| `ruff check src scripts tests` | 通过 |
| `python -m mypy --platform win32` | 通过（364 个文件） |
| 全量 pytest，`c20d81a` | 231 失败 / 972 通过 / 30 跳过 |
| 全量 pytest，本轮 | 231 失败 / 998 通过 / 30 跳过 |
| 失败集合 | 与 `c20d81a` 完全相同，失败信息逐字一致，均为既有的 Windows 路径基线 |
| 多出的 26 个通过 | 均为新增用例：`tests/test_cleanup_skip_continue.py` 22 个，`tests/test_cleanup_in_use.py` 3 个，`tests/test_deletion_workflow.py` 1 个 |
| 审计原文 SHA-256 | 未变 |

**在 Linux 上模拟 Windows（`tests/test_cleanup_skip_continue.py`）：**

- **做法：** Windows 版遍历（`_empty_temp_folder`、`_purge_tree_skipping_in_use_windows`、`_purge_temp_folder_contents_windows`）在 `tmp_path` 的真实目录上运行，只把 Win32 调用换成用 `os` 删除的替身。
- **替身抛出两种 Windows 错误形态：**
  - Windows 上 `os` 函数的形态：`PermissionError`，errno 13，`winerror` 为 32 或 5；
  - `_raise_windows_error` 的形态：`OSError(32 或 5, …)`，errno 为 32 或 5，`winerror` 为 None（32 在 Linux 上是 `BrokenPipeError`）。
- 每个场景在两种形态下都跑：
  - 两个文件被占用、一个子目录本身被程序开着：其余 6 个文件全部删除，子目录里的文件也删掉、子目录留下，计 3 项留下；只读删除标志用于每个文件；句柄全部关闭。32 与 5 两种代码都跑。
  - 被选中的目录本身打不开删除句柄：内容全部删掉，目录留下，计 1 项。连列目录都不行时仍整项失败（原行为）。
  - 某子目录列不出内容：只留下它，其余照删。
  - 一个文件、一个子目录的最终路径出界：只跳过这 2 项，其余照删，不抛异常。
  - 一个文件在删除前自己消失：不计数，整棵树仍删掉。
  - 未知错误码（1117）：仍中止该项。
  - 临时文件夹同样处理，并保留文件夹本身。
- **POSIX 实现：** 子目录在清理中被换成链接、子目录列不出内容，两种情况都只跳过该项。这两个测试在 Windows 上走真实 Windows 实现，在 Linux 上走 POSIX 实现，不用 skip。
- **执行层：** 树只因一个“安全检查跳过”而留下时，记为“删了一部分”，不再是“tree deletion stopped…”拒绝。
- **界面层：** 状态栏与“清理结果”的文字（逐项、不截断）；换行绑定；双击方框；整树复检原因的中文。

**变异检查**：逐项撤回本轮改动，相应测试失败。

| 撤回 | 失败的用例数 |
| --- | --- |
| 子目录打不开删除句柄就整个跳过 | 6 |
| 被选中目录打不开删除句柄就整项失败 | 2 |
| 子目录列不出内容就中止 | 2 |
| 文件安全检查失败就中止 | 1 |
| 已消失的文件让上级目录留下 | 2 |
| 整树复检任一条目出错就拒绝 | 1 |
| Codex 缓存放回“必须关闭” | 2 |
| 只删了一部分的项算作已清理 | 1 |
| 双击方框打开资源管理器 | 1 |

**删除前检查对照**：在 Linux 上把进程探测设为运行中，截图上的行（Chrome `Default\Cache`、`Default\Code Cache`、`GPUPersistentCache`，Codex 桌面版四个缓存目录，`.codex\.tmp\plugins`，旧 TUI 日志）在 `c20d81a` 和本轮的结果见 §12.2 (1) 的表。

### 12.7 只能在真实 Windows 上验证

1. **只列目录的句柄能否打开：** 被 Chrome/Codex 等开着的目录，能否用“只列目录、共享读写、不含删除权限”的句柄打开。如果持有者自己带删除权限，或不共享读，这个目录仍会整个留下（并计数）。
2. **只读文件的删除标志：** `FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE` 在用户的 Windows 版本上的效果；旧系统会回退。
3. **真实计数：** Chrome、Codex 运行时，每个缓存目录实际删掉多少、留下多少；块文件缓存是否正好留下 `index`、`data_*`。
4. **Codex 重新同步：** Codex 运行时 `.codex\.tmp\plugins`、桌面版缓存被删后，Codex 是否正常重新生成或重新同步。
5. **界面：** 状态栏换行、“清理结果”窗口、「清理结果」按钮、双击方框的实际表现（测试用的是 Tk 替身）。
6. **Temp 未勾选：** 用户这次 Temp 未勾选的确切原因（§12.2 (4)，未证实）。
7. **Linux 基线里的测试：** 涉及这些模块、但在 Linux 上本来就失败的测试（Windows 路径基线），只能在 Windows CI 上跑完整断言。我逐个检查了其中设置“程序运行中”或调用删除前检查的用例，涉及的规则（IDE 索引、AVD、SDK、Visual Studio 组件缓存、Claude 会话标记等）本轮都没有放开。

### 12.8 发现但未改

- **扫描阶段的整树汇总仍是全有或全无：** `scanner/tree_summary.summarize_tree` 只要树里有一项读不到，整个应用缓存根就不列出，它的文件也不会逐个列出。这属于扫描列出的范围，不是清理，本轮未改。（已在 §13 修复。）
- **清理后重新发布会把已删除的行重新列出并勾选：** 清理后若再导入 AI 结果、保存规则等，会从扫描结果重新分区，已删除的行会再出现、再勾选；再次清理时按“已不存在”计为完成。与本次反馈无关，未改。

### 12.9 改动文件

| 类型 | 文件 |
| --- | --- |
| 生产 | `core/application_cleanup.py`、`core/postscan_cleanup.py`、`core/triage.py`、`core/whole_tree_policy.py`、`platform/windows/exact_cleanup.py`、`ui/app.py`、`ui/modern_app.py`、`ui/product_app.py`、`ui/product_vendor_app.py` |
| 测试（新） | `tests/test_cleanup_skip_continue.py` |
| 测试（新增用例） | `tests/test_cleanup_in_use.py`、`tests/test_deletion_workflow.py` |
| 测试（按用户决定改写断言，§12.5） | `tests/test_whole_tree_policy.py`、`test_claude_cleanup.py`、`test_codex_legacy_log_cleanup.py`、`test_cursor_cache_safety_semantics.py`、`test_cursor_cleanup.py`、`test_npm_cleanup.py`、`test_pnpm_cleanup.py`、`test_trae_cleanup.py`、`test_windsurf_cleanup.py`、`test_visual_studio_servicehub_logs.py` |
| 文档 | 本文件 §12 |

## 13. 扫描整树汇总：读不了的只跳过那一条（2026-11-11）

| 项 | 内容 |
| --- | --- |
| 基线 | `80e944d`（已推送，CI 通过） |
| 用户批准 | “改吧”（2026-11-11），针对 §12.8 第一条 |
| 范围 | 只做这一项；§12.8 第二条（清理后重新发布会重新勾选）本轮未改 |
| 提交状态 | 未 commit、未 push、未构建 |
| 审计原文 | `docs/cleanup-safety-audit-2026-11-07.md` 未改动，SHA-256 仍为 `a078457ee2be031482f1f15073491b82976b4dcbf5aceeeface413794fc4ce0f` |
| 运行环境 | `uvx uv@0.11.6 run --frozen`（复用仓库 `.venv`，CPython 3.13.18），`winreg` 桩在 `/tmp/winreg-stub`。**本轮没有在 Windows 上运行任何东西** |
| 测试数据 | 只用 `tmp_path` 和会话临时目录；没有删除真实用户文件 |

### 13.1 根因

- **整目录缓存根不逐个分类：** 对“可整目录删除的应用缓存根”（`_is_rule_covered_whole_tree`：厂商管理、TOOL 规则、允许整目录、根本身可删），产品扫描不逐个分类文件，而是：
  - 用 `summarize_tree` 汇总数量和大小，作为一行列出；
  - 同时把这些根放进通用遍历的跳过列表，避免重复遍历。
- **全有或全无：** 树里任何一个条目出错，`summarize_tree` 都会抛 `TreeSummaryIncomplete`，`_scan_worker` 捕获后直接 `continue`。出错包括：读不到条目信息、子目录列不出、列到一半中断、子目录在汇总途中消失。
- **后果：**
  - 这一行不会列出；
  - 它又被通用遍历跳过，所以整个缓存里的文件一个都不会提供；
  - 界面没有任何提示。
- **与用户反馈的关系：** 浏览器、编辑器运行时会不断新建、删除缓存子目录，汇总途中有子目录消失或被占用很常见。于是一整个缓存（可能几百 MB）不出现在列表里，这很可能是“还是感觉很多没删掉”的原因之一。
- **同时修的另一个缺口：** 子目录是按路径重新打开来列出的。如果某个子目录在父目录列出之后被换成链接或联接点，`os.scandir` 会跟进去，把树外的文件算进大小。这只影响显示的大小，删除时有逐句柄校验，不会删到树外。

### 13.2 改动

| 位置 | 改动 |
| --- | --- |
| `scanner/tree_summary.py` | **读不了的条目只跳过它自己：**子目录列不出、条目信息读不到、子目录列到一半中断，都只记一项，其余照常汇总。记录的原因分四类：正被程序使用、没有读取权限、扫描时被换成链接或其他对象、读取出错。汇总途中自己消失的条目不记，它已经不在了 |
| 同上 | **换成链接只跳过该目录：**子目录打开列目录后，用不跟随链接的 `lstat` 再确认它仍是列出时的那个目录。如果它已变成链接、联接点或非目录（POSIX 上还比对列出时的 inode），只跳过这个目录，记为“被换成链接或其他对象”，不跟进去 |
| 同上 | **硬限制不变：**根目录本身读不了（读不到信息、列不出、列到一半中断）或扫描被取消，仍抛 `TreeSummaryIncomplete`。链接和重解析点仍然不跟随、不计入 |
| 同上 | `TreeSummary` 新增 `skipped`、`skipped_by_reason`、`skipped_examples`（最多 20 条），原有字段不变 |
| `ui/product_app.py` | **照常列出：**有跳过条目的根仍列为一行，大小只含读到的部分。行的原因后面附上“扫描时其中 N 项读不到，已跳过、未计入大小（各原因及数量）；例：路径（原因）”，并加标签 `scan_skipped_entries` |
| 同上 | **根本身读不了：**仍整项不列，但会记下来；扫描期间根自己消失的不记 |
| 同上 | **扫描完成时状态栏会说明**（状态栏已按窗口宽度换行）：“其中 X 个应用缓存目录里有 N 项读不到，已跳过、未计入大小，这些目录的其余部分照常列出（路径…；清理后见「清理结果」）。”以及“K 个应用缓存目录本身无法读取或无法确认是同一个目录，整项未列出，里面的文件也没有逐个列出（路径…）。” |
| `ui/app.py` | 「清理结果」里，这些行下面多一行“扫描时：…”，说明扫描时跳过了什么 |

**安全分类不变：**

- 根行仍由 `triage_directory` 加 `assess_application_whole_tree_policy` 判定；被跳过的条目不改变它能否删除（年龄、大小本来就不是安全门槛）。
- 以下检查都没有改：
  - 根目录前后两次身份比对（`_identified_tree_summary`）；
  - 系统禁删名单；
  - 删除前的规则身份复检；
  - 删除时的逐句柄校验。

### 13.3 与删除前复查一致

三处规则相同：问题只作用于出问题的那个条目，根本身出问题才整项处理。

| 阶段 | 子项读不了 | 子项是链接 | 根本身读不了 |
| --- | --- | --- | --- |
| 扫描汇总（本节） | 跳过并计数 | 不跟随；扫描中被换成链接的，跳过并计数 | 整项不列，并在状态栏说明 |
| 删除前复查（`whole_tree_policy._fresh_tree_evidence`，§12） | 错误记录跳过 | 作为边界记录，不跟随 | 拒绝（“删除前复检：这个目录本身无法再次读取”） |
| 删除（`purge_directory_tree_skipping_in_use`，§12） | 留下并计数，其余删除 | 只删链接本身；清理中被换成链接的只跳过它 | 打不开删除句柄就用只列目录的句柄清空；连列目录都不行时整项失败 |

新测试把整条链路走了一遍：

1. 扫描时 `locked` 子目录列不出；
2. 这一行照常列出（2 个文件，48 B），原因里写明跳过了什么；
3. 删除前复查通过；
4. 删除后 `locked` 留下（“没有删除权限 1 项”），其余全部删除，这一项记为“删了一部分”；
5. 「清理结果」里该行下有“扫描时：…”。

### 13.4 性能

- **增加的开销：**每个子目录多一次 `lstat`（Windows 上是一次打开句柄读属性）；每个文件的开销没有变化。
- **Linux 基准：**5 万个文件、2,551 个目录，汇总 7 次取中位数。改前 125～142 ms，改后 136～142 ms，在同一机器多次运行的波动范围内。
- **Windows 推断（未实测）：**按每次 `lstat` 约 20～40 µs 估算，2,551 个目录多出约 0.05～0.1 秒；典型 C 盘扫描仍是分钟级。
- **界面线程：**只是扫描结束时多拼一段状态文字，没有增加 Tk 调用；汇总仍在扫描线程里进行，不会造成“未响应”。

### 13.5 测试

新文件 `tests/test_tree_summary_skips.py`，10 个用例，只用 `tmp_path`：

| 用例 | 验证 |
| --- | --- |
| `test_an_unreadable_folder_is_skipped_and_the_rest_of_the_tree_is_counted` | 一个子目录没有读取权限：其余 2 个文件、48 B 照常计入；跳过 1 项（没有读取权限），例子是该目录 |
| `test_a_folder_another_program_holds_is_counted_as_in_use` | 被占用（Windows：`winerror` 32；POSIX：EBUSY）：只跳过该目录，记为“正被程序使用” |
| `test_a_file_whose_details_cannot_be_read_is_skipped_alone` | 一个文件读不到信息：只跳过它，其余 4 个文件照常计入 |
| `test_a_folder_that_vanished_on_its_own_is_not_reported` | 汇总途中消失的子目录不算跳过 |
| `test_the_tree_itself_unreadable_still_stops_the_summary` | 根本身列不出或不存在：仍抛 `TreeSummaryIncomplete` |
| `test_a_folder_swapped_for_a_link_is_skipped_and_never_followed` | 子目录在列出后被换成指向树外的链接（Windows 上是联接点，用 `_winapi.CreateJunction`，不需要管理员；Linux 上是符号链接）：只跳过它，树外文件不计入 |
| `test_a_cache_root_with_an_unreadable_folder_is_still_listed_and_cleaned` | 真实产品扫描线程：这一行列出、大小只含可读部分、仍属可直接清理、原因带说明、状态栏说明；删除前复查和真实删除只留下读不了的目录；「清理结果」带“扫描时：…” |
| `test_a_cache_root_that_cannot_be_read_is_not_listed_but_is_reported` | 根本身读不了：整项不列，状态栏点名说明 |
| `test_a_fully_readable_cache_root_says_nothing_extra` | 全部可读时，行的原因和状态栏不多任何文字，计数 4 个文件 |
| `test_a_cache_root_gone_before_it_was_summed_is_not_reported_as_unreadable` | 扫描期间根自己消失：不列，也不报“无法读取” |

- 没有改动任何既有测试断言，没有新增 skip。

**变异检查：**逐项撤回修复，新测试都会失败。

| 撤回 | 失败的用例数 |
| --- | --- |
| 任何条目读不了就放弃整棵树（即 `80e944d` 的行为） | 5 |
| 子目录打开后不再确认它仍是列出时的目录 | 1（换成链接） |
| 根本身读不了也只跳过、不停止 | 2 |
| 行的原因不写跳过了什么 | 1 |
| 根本身读不了时不记录 | 1 |
| 扫描期间消失的根也报“无法读取” | 1 |
| 「清理结果」不带扫描说明 | 1 |

**全量结果：**

| 检查 | 结果 |
| --- | --- |
| `uvx uv@0.11.6 run --frozen ruff check src scripts tests` | 通过 |
| `uvx uv@0.11.6 run --frozen python -m mypy --platform win32` | 通过（365 个文件） |
| `uvx uv@0.11.6 run --frozen pytest`，`80e944d` | 231 失败 / 999 通过 / 29 跳过 |
| 同上，本轮 | 231 失败 / 1009 通过 / 29 跳过 |
| 失败集合 | 与 `80e944d` 完全相同，失败信息逐字一致，均为既有的 Linux 基线（Windows 路径语义） |
| 多出的 10 个通过 | 即上表的 10 个新用例 |

- 用 `uv run` 跑的结果是 999 通过 / 29 跳过，§12.6 直接用 `.venv` 的 Python 跑是 998 / 30。差的一个是环境导致的跳过，两者的失败集合相同。

### 13.6 只能在真实 Windows 上验证

1. **真实计数：**真实机器上哪些缓存根有读不到的条目、各有多少，例如浏览器、Codex 运行时。
2. **联接点识别：**扫描中子目录被换成联接点时能否识别。测试在 Windows CI 上会用真正的联接点运行，本机只跑了符号链接。
3. **Windows 上 `lstat` 的额外开销**（§13.4 只是推断）。
4. **界面显示：**状态栏和「清理结果」里新增的文字在真实 Tk 下的显示效果。
5. **非 NTFS 卷：**应用缓存根在 exFAT 等卷上时，身份不是 128 位，仍整项不列，现在会在状态栏说明；实际会出现多少，未知。
6. **用户感受：**“还是感觉很多没删掉”是否因此明显改善，需要用户实测对比。

### 13.7 未改

- §12.8 第二条（清理后重新发布会重新勾选）：按要求本轮不改。
- 通用遍历（`scan_roots`）里读不到的条目，仍只计入扫描统计，不在状态栏单独说明。这不属于整树汇总，不在本项范围。

### 13.8 改动文件

| 类型 | 文件 |
| --- | --- |
| 生产 | `scanner/tree_summary.py`、`ui/product_app.py`、`ui/app.py` |
| 测试（新） | `tests/test_tree_summary_skips.py` |
| 文档 | 本文件 §13；页首提交状态、§12.8 第一条加“已在 §13 修复” |

## 14. 清理长时间“一个都没删掉”（`9459930` 用户实测，2026-11-11）

| 项 | 内容 |
| --- | --- |
| 基线 | `9459930`（HEAD，已推送） |
| 用户反馈 | 真机 Windows，EXE 为 `9459930`。状态栏一直是“正在清理 128 项；普通安全项直接释放空间，包管理器缓存由官方命令处理…”，进度条在走，很长时间一个都没删掉。安全可清理 4.6 GB / 128 项，第一行是 `AppData\Local\Temp`（3.5 GB，删除整个目录）。原话：“现在删除有问题，到现在这么长时间了，一个没删掉” |
| 提交状态 | 已 commit、已 push（提交信息“Keep cleanup moving row by row and never wait on a prompt”）；Windows CI 结果见交付说明 |
| 审计原文 | `docs/cleanup-safety-audit-2026-11-07.md` 未改动，SHA-256 仍为 `a078457ee2be031482f1f15073491b82976b4dcbf5aceeeface413794fc4ce0f` |
| 运行环境 | `uvx uv@0.11.6 run --frozen`，`winreg` 桩在 `/tmp/winreg-stub`。**本轮没有在 Windows 上运行任何东西**，也没有在本机运行任何包管理器的清理命令（测试中都用假命令） |
| 测试数据 | 只用 `tmp_path` 和会话临时目录 |

### 14.1 复现（Linux）

**做法：**

- **目录：**在会话临时目录造一个仿 Windows 的用户目录：
  - `Temp` 里 100,000 个文件，分 3 层、1,000 个文件夹，其中 10 个文件模拟被占用；
  - 一个整目录缓存，5,000 个文件；
  - `Downloads` 里 50 个文件。
- **运行中的程序：**模拟一台普通 Windows 桌面：230 个能读到启动参数的程序；20 个启动参数被隐藏的程序，其中 5 个连程序路径都读不到；200 个已登记服务。
- **流程：**用真实的产品扫描线程扫描，再把“安全可清理”的行交给真实的产品清理线程（`_product_delete_worker`），并记录窗口收到的每个事件及其时间。

| | `9459930` | 本轮 |
| --- | --- | --- |
| 清理耗时 | 122.3 秒 | 27.3～33.9 秒 |
| 窗口收到的第一个事件 | 122.3 秒（就是唯一的“全部结束”） | 0.13 秒（准备进度） |
| 整目录缓存这一行的结果 | 122.3 秒才显示 | 0.44 秒 |
| Temp 这一行的结果 | 122.3 秒 | 27.3 秒（之前持续显示目录内的处理进度） |
| 清理结果 | 删除 99,990 个、留下 10 个被占用 | 相同 |

### 14.2 根因

1. **界面收不到任何进展。**产品窗口的清理线程从头到尾只发一个事件：所有行（包括最后的包管理器命令）都结束后的“全部结束”。它不报准备进度，不报每一行的结果，也不报大目录内部的进度。于是即使已经删掉了很多，状态栏也一直是开始时那句话，进度条只是在转。这正是截图上的样子。（旧版界面的清理线程会报这些进度，产品窗口改写清理线程时没有带上。）
2. **清空 Temp 时每个条目都很贵。**清空临时文件夹前，会对每个条目做两项安全检查：是否由别的规则管理，是否正被运行中的程序或服务使用。这两项检查必须保留，但实现上的开销和运行中的程序数量成正比。剖析（cProfile）显示 98% 的时间在这里：
   - **“是否正被使用”（约 67%）：**每个条目要对每个隐藏启动参数的程序，把路径重新拆分一遍（10 万个条目共 424 万次拆分）；还要把所有程序文件夹逐个比一遍（2,862 万次比较）；如果条目是文件夹，再把所有程序文件和文件夹逐个比一遍。
   - **“是否由别的规则管理”（约 30%）：**Edge 的更新日志放在 `%TEMP%`，所以 Edge 的规则要看 Temp 里的每个条目。它查找浏览器配置目录时用的是 `pathlib` 的 `relative_to`，每次都要构造路径的全部上级（40 万次调用）。Cursor、Claude 也在 Temp 下有自己的目录，同样要逐条看。
   - **推断：**Windows 上 Python 和 `ntpath` 更慢，每个条目还要调用一次 `GetLongPathNameW`；真机通常有两三百个进程，非管理员运行时还有更多“启动参数被隐藏”的进程。所以每个条目可能要几毫秒，一个有几十万条目的 Temp 要几十分钟，期间界面没有任何变化。用户这个 3.5 GB 的 Temp 里有多少条目，截图上没有。
3. **Temp 排在最前面。**行按大小排序，3.5 GB 的 Temp 是第一行，后面 127 行（包括 Claude 旧版本、浏览器缓存等很快就能删完的）都要等它结束。
4. **线程意外结束时，界面永远等下去。**清理线程只捕获几种已知的错误类型。遇到别的错误（例如命令超时的 `TimeoutExpired`，或其他 `RuntimeError`），线程直接结束，“全部结束”的事件永远不会发出，窗口就一直停在“正在清理”。
5. **官方命令：**排在所有文件行之后，按顺序执行；没有关闭标准输入，遇到提示会一直等到超时；超时时间为 30～600 秒。截图这次没有看到官方命令行，它们不是这次卡住的原因，但要求里点到了，所以一并处理。
6. **查过、没有问题的：**
   - 被占用的文件：一次失败就跳过，没有重试，也没有等待（新增测试确认只尝试一次）；
   - 没有锁或队列互相等待；
   - 不存在官方命令排在文件行前面的情况。

### 14.3 改动

| 位置 | 改动 |
| --- | --- |
| `ui/product_vendor_app.py` `_product_delete_worker` | **逐项上报：**每一行一结束就发 `delete_item_done`（第几项、共几项，以及这一行删掉了什么、留下了什么、原因）；准备阶段发 `delete_prepare_progress`；大目录内部每 0.25 秒发一次 `delete_action_progress` |
| 同上 | **临时文件夹排到最后：**它们要逐个条目检查，最慢；其他行先删，先显示结果 |
| 同上 | **官方命令另开线程：**和文件行同时进行，每条命令结束就上报。一条命令卡住或超时，只让它自己那一行失败 |
| 同上 | **一定会结束：**任何意外错误都只让还没上报的行记为失败（写明原因），“全部结束”放在 `finally` 里，一定会发出 |
| `core/postscan_cleanup.py` `execute_cleanup_batch` | 新增 `on_action_done`：每个动作一结束，就交出只含这一项的结果（状态、删掉的文件数和字节数、留下的内容及原因、失败原因）。上报出问题也不会影响删除 |
| `ui/app.py` `_item_done` | **收到一行的结果就只改这一行：**删干净的从列表移除；留下或失败的在原因列写明。状态栏显示“正在清理… 已处理 x / N 项：已清理 a 项、删了一部分 b 项、未完成 c 项，已释放 X；点「清理结果」可随时查看每一项”，进度条按项前进。整张表只在全部结束时重画一次 |
| 同上 | **「清理结果」随时可看：**按钮在清理过程中也能用；已打开的窗口每秒最多刷新一次，原地更新文字，不会关掉重开。大目录的进度文字同时带上这份统计和当前目录的路径 |
| `core/live_owners.py` | **“是否正被使用”的检查变快：**<ul><li>每个目标只拆分一次路径、只计算一次各部分的名称片段和上级目录，供所有隐藏进程共用；</li><li>找“目标所在的程序文件夹”，改为按目标自身的各级上级去字典里查，与原来按顺序扫描取到的是同一个；</li><li>找“文件夹里有没有正在用的路径”，改为在排好序的列表里查前缀范围，与原来取到的是同一个。</li></ul>判断和拒绝原因的文字都不变 |
| `core/edge_cleanup.py` `_profile_root_for_path` | 先用字符串判断路径是否在数据根之下，不在就直接返回；在的话仍走原来的 `relative_to`。这个判断是必要条件，所以结果不变 |
| `core/_application_cleanup_impl.py` | `_could_hold` 和 `_matches` 里与根、候选路径有关的部分（折叠大小写、“它下面”的前缀、是否含通配符）各算一次后缓存，判断逻辑不变 |
| `core/{go,nuget,pip,uv,pnpm,conda}_maintenance.py` | 官方命令一律 `stdin=subprocess.DEVNULL`：需要回答的提示会立刻失败，不会等。原有超时（30～600 秒）不变；超时会变成这一行的失败原因 |

**安全判断没有放松：**

- 每个条目仍逐个检查：
  - 是否由别的规则或你的保留设置管理；
  - 是否正被运行中的程序或服务使用（包括启动参数被隐藏的程序）；
  - 链接只删链接本身，不跟进去；
  - 最终路径出界的跳过；
  - 被占用的跳过。
- 系统禁删名单、身份校验不变。
- 只是把同一条目上重复计算的东西算一次，把逐个扫描改成等价的查找。

**没有改为“沿用扫描时的检查结果”：**

- 扫描和清理之间，规则、应用配置（例如 Claude 的记忆目录）、正在运行的程序都可能变化。
- 沿用扫描结果，等于在这些情况下放松判断，所以仍在清理时逐个条目检查，只是检查变快了。

### 14.4 测试

**新文件 `tests/test_cleanup_progress.py`（12 个用例）：**

| 用例 | 验证 |
| --- | --- |
| `test_official_commands_never_wait_for_input_and_stop_at_their_timeout`（go、dotnet nuget、pip、uv、pnpm、conda，共 6 组；conda 为提交前复核时补上，它同样由清理时的官方命令调用） | 命令以 `stdin=DEVNULL` 启动并带超时；超时变成这一行的失败（`RuntimeError`），清理不会中断 |
| `test_the_owning_folder_lookup_finds_what_the_folder_scan_found` | 随机生成 300 组程序文件夹，含嵌套、带结尾分隔符、整个盘；按上级查找的结果与原来按顺序扫描逐一相同 |
| `test_the_used_inside_lookup_finds_what_the_scan_of_every_path_found` | 随机生成 300 组；按排序范围查找的结果与原来扫描所有路径逐一相同 |
| `test_hidden_process_relations_are_the_same_when_worked_out_once_per_target` | 多个进程共用一份拆好的路径时，与每个进程各自重新计算的结果完全相同（同名程序、名称片段、程序文件夹、`.msi`，文件和文件夹两种情况） |
| `test_cached_rule_matching_answers_as_the_original_did` | 缓存后的 `_could_hold`、`_matches` 与原实现在各种根、模式、匹配方式下逐一相同 |
| `test_the_edge_profile_lookup_answers_as_before_without_walking_parents` | Edge 的配置目录查找与原来基于 `relative_to` 的写法逐一相同，包括大小写差异 |
| `test_a_file_in_use_is_tried_once_and_never_waited_for` | Windows 版遍历中，被占用的文件只尝试删除一次，不重试、不等待 |
| `test_each_row_is_shown_the_moment_it_ends` | 窗口收到一行结果就移除或改写这一行；状态栏的统计和进度条随之更新；清理还在进行时就能打开「清理结果」 |

**新增到 `tests/test_deletion_workflow.py`（真实扫描线程加真实产品清理线程，3 个用例）：**

| 用例 | 验证 |
| --- | --- |
| `test_every_row_is_reported_as_it_ends_and_temp_folders_go_last` | **场景：**Temp 里 600 多个条目，其中一个文件被占用；另有 3 个安装包；同时运行 150 个程序，其中 10 个启动参数被隐藏。<br>**事件顺序：**第一个事件是准备进度；每行各有一个结果事件（1/4…4/4）；安装包先报，Temp 最后报；Temp 报结果之前已有目录内的进度事件；最后是“全部结束”。<br>**结果：**被占用的文件留下（留下 1 项）。<br>**开销守护（数调用次数，与机器快慢无关）：**每个条目最多拆分两次路径；每个条目的 `is_within` 调用远少于运行中的程序数（≤ 20 次，而程序有 160 个）；Edge 从不为 Temp 里的路径构造全部上级 |
| `test_a_hanging_official_command_holds_back_no_other_row` | 第一条官方命令一直挂着时，三行文件都已删掉并上报；放开后，另一条“超时”的命令只让它自己那一行失败；最后发出“全部结束” |
| `test_an_unexpected_error_still_reports_every_row_and_ends_the_run` | 执行时抛出意外错误（`KeyError`），或整段文件清理意外失败：每一行都上报为失败并写明原因，“全部结束”一定发出，没有删除任何东西 |

- 没有改动任何既有测试的断言，没有新增 skip。

**变异检查：**逐项撤回修复，都有新测试失败。

| 撤回 | 失败的用例 |
| --- | --- |
| 不按行发事件 | 逐项上报、命令挂起、意外错误这 3 个 |
| 临时文件夹不排到最后 | 逐项上报 |
| 大目录内不报进度 | 逐项上报 |
| 官方命令在同一线程里先跑 | 命令挂起 |
| 意外错误不兜底 | 意外错误 |
| 官方命令不关闭标准输入（go） | 官方命令（go 那一组） |
| 每个隐藏进程都重新拆分路径 | 逐项上报（开销守护） |
| 逐个扫描程序文件夹 | 逐项上报（开销守护） |
| Edge 去掉字符串预判 | 逐项上报（开销守护） |
| 窗口不在收到结果时改这一行 | 窗口逐行显示 |
| 逐个扫描所有在用路径 | 逐项上报（开销守护） |

**全量结果：**

| 检查 | 结果 |
| --- | --- |
| `uvx uv@0.11.6 run --frozen ruff check src scripts tests` | 通过 |
| `uvx uv@0.11.6 run --frozen python -m mypy --platform win32` | 通过（366 个文件） |
| `uvx uv@0.11.6 run --frozen pytest`，`9459930`（在 worktree 中运行） | 231 失败 / 1009 通过 / 29 跳过 |
| 同上，本轮 | 231 失败 / 1024 通过 / 29 跳过 |
| 失败集合 | 与 `9459930` 完全相同，失败信息逐字一致，均为既有的 Linux 基线 |
| 多出的 15 个通过 | 即上面新增的 15 个用例 |

### 14.5 只能在真实 Windows 上验证

1. **真实耗时：**用户的 Temp（3.5 GB）实际有多少条目、现在要多久；真实进程数量和“启动参数被隐藏”的进程数量下，每个条目的开销。§14.1 的数字来自 Linux 复现。
2. **界面表现：**真实 Tk 下逐行移除、改写原因列、状态栏和进度条随项前进、清理中打开「清理结果」并每秒刷新、窗口不出现“未响应”。测试用的是 Tk 替身。
3. **官方命令：**在 GUI 进程里以 `stdin=DEVNULL` 运行 dotnet、go 的实际效果；另开线程运行时与文件清理之间没有相互影响（两者处理的目录不重叠：官方命令管理的缓存不在通用遍历里）。
4. **用户这次到底卡在哪一步：**截图只能说明界面一直没有收到进展。实际是在慢慢删 Temp（最可能），还是线程因意外错误结束了，需要下次实测。新版本会在状态栏逐项显示，也会把意外错误写进那一行的原因。

### 14.6 未改

- **Claude 的规则：**仍对 Temp 里的每个条目都要看一遍（它的临时目录 `%TEMP%\claude` 在 Temp 下），这是剩下的主要开销之一，留给 §15 的扫描测速一起处理。
- **整目录应用缓存：**生成删除对象时和删除前各重新统计一遍整棵树，本轮未改。
- **§12.8 第二条：**清理后重新发布会把已删的行重新列出并勾选，仍未改。

## 15. 扫描测速（进行中，因 §14 暂停）

用户在真机 Windows 上用 `9459930` 扫描 C 盘：共 769,010 个文件、114,846 个文件夹、140.8 GB，总耗时 21 分 6 秒（约 1.65 毫秒/文件）。`c20d81a` 那次是 12 分 52 秒，当时没有记录文件数。

**已得到的基准（Linux）：**

- **基准树：**208,668 个文件、27,614 个文件夹，结构仿 Windows 用户目录。另把 9 个缓存目录登记为整目录缓存，并按 Windows 的 128 位身份处理，让它们走汇总路径。
- **方法：**用 git worktree 分别运行三个提交的真实产品扫描线程，每个提交跑 3 次。

| 提交 | 耗时（秒） | 看过的文件 / 文件夹 |
| --- | --- | --- |
| `c20d81a` | 39.6（第一次，冷缓存）/ 29.8 / 31.1 | 188,763 / 26,102 |
| `80e944d` | 31.2 / 31.8 / 32.3 | 188,763 / 26,102 |
| `9459930` | 29.5 / 30.6 / 31.9 | 188,763 / 26,102 |

- **同样的文件没有变慢。**三个提交的文件系统调用次数完全相同，只有 `9459930` 多了 1,221 次 `lstat`（整目录缓存里每个子文件夹一次，§13）。
- **两种可能都未能确认：**
  - 以前整目录缓存一遇到读不到的条目就整个放弃（不计数、也不遍历），现在会把它统计完，所以文件数可能变多；
  - 两次扫描之间磁盘内容本身发生了变化。
- **剖析（`9459930`）：**主要耗时是按文件做的分类（约 53%）：
  - Claude 的规则不在应用范围索引里，几乎每个文件都要看一遍（约 23%）；
  - 临时文件夹里每个条目都要检查是否由别的规则管理（约 20%）；
  - Edge 查找配置目录用 `relative_to`（约 10%）；
  - 应用范围索引本身的计算（约 9%）。
- **已随 §14 落地的部分：**Edge 的字符串预判，以及 `_could_hold` / `_matches` 的缓存，也作用于扫描。
- **待做：**
  - 把 Claude 纳入应用范围索引，注意它的自定义记忆目录在运行中可能变化，要动态处理；
  - 加入按目录统计耗时（扫描结束时写进状态栏或日志）；
  - 加入性能守护测试；
  - 重新测三个提交与修改后的版本。
