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
| 提交状态 | 随本文档一并提交、推送，由 Windows CI 构建 |
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
2. 句柄版的清空和整目录删除：真实共享冲突时的跳过、`ERROR_DIR_NOT_EMPTY` 的处理、文件夹本身保留与删除。Linux 上测的是同语义的 POSIX 实现；Windows 上的占用测试（真实打开文件）在 CI 上首次运行。
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
