# 扫描性能、计时、界面卡顿、临时文件夹与清理拒绝（2026-11-10）

针对 b156d9b 构建的用户实测反馈：

| 项 | 用户反馈 |
| --- | --- |
| 扫描速度 | 完整扫描 110 分 2 秒，扫描中已看过 405,827 个文件（118.2 GB） |
| 计时 | 扫描未完成就显示已用时间 |
| 界面卡顿 | 扫描一两个小时后切换窗口再切回，窗口“未响应” |
| (A) 最终统计 | 扫完后“已看过 N 个文件”的统计消失 |
| (B) 清理拒绝 | 66 项中 36 项未完成：“所属程序正在运行” 35 项，系统根黑名单 1 项（crx_cache） |

| 项 | 内容 |
| --- | --- |
| 基线 | `b156d9b` |
| 提交状态 | §1～§10 已随 `472364d` 提交、推送并由 Windows CI 构建；§11（`472364d` 的 CI 失败修复）未 commit、未 push、未构建 |
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
  - 扫描中标签固定为“扫描耗时：扫描进行中，完成后显示总耗时”，不显示任何数字。
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
