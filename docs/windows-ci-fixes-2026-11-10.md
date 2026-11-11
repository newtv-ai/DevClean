# Windows CI 失败修复记录（2026-11-10）

Windows CI [run 42429796894](https://github.com/newtv-ai/DevClean/actions/runs/42429796894)（`a3f6da9`）：1175 通过 / 11 失败，打包步骤被跳过，没有产出新 EXE。本轮逐项查明每个失败是生产代码问题还是测试隔离问题，并分别修复。没有使用 skip、没有放松或删除任何断言、没有删除测试。审计报告 [`cleanup-safety-audit-2026-11-07.md`](cleanup-safety-audit-2026-11-07.md) 未改动（SHA-256 `a078457e…4fc4ce0f`，与 `a3f6da9` 一致）。

| 项 | 内容 |
| --- | --- |
| 基线 commit | `a3f6da9`，工作树开始时干净 |
| 提交状态 | 未 commit、未 push、未触发 CI；改动全部留在工作树，等待核查后统一提交 |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13.18，pytest 8.4.2，mypy 2.2.0（`--platform win32`），ruff 0.15.21；`winreg` 为临时目录中的桩模块。**本轮没有在 Windows 上运行任何东西**，Windows 结论见 §6 |
| 测试数据 | 只用 `tmp_path` 临时夹具。没有删除真实用户文件，没有卸载任何程序，没有清理系统缓存 |
| 未做（按要求） | 旧配置迁移；“不能删除”列表 |

## 1. 结论一览

| CI 失败 | 归类 | 根因 | 修复 |
| --- | --- | --- | --- |
| `test_mixed_folders_delete_only_what_the_rules_allow`（多出 `crx_cache`） | 测试隔离 | `ws` 夹具只覆盖了部分环境变量。runner 上的 `PROGRAMFILES(X86)` 让扫描把真实的 `C:\Program Files (x86)\Google\GoogleUpdater\crx_cache` 当成已知根 | 夹具从空环境开始（§3.1） |
| `test_live_state_is_read_once_per_cleanup_run_not_per_file`（`6 == 5`） | 测试隔离 | 同上。断言的是“可删除条目数”，多出的第 6 条就是 runner 的 `crx_cache`，不是进程列表多读了一次 | 同上 |
| `test_review_items_are_not_deleted_until_approved_then_deleted`（`old-download.zip` 未变为可删除；另多出 `crx_cache`） | **生产代码** + 测试隔离 | AI 批准的规则被存成在本机展开后指向另一个文件的“可移植路径”（§2.1）；`crx_cache` 同上 | §2.1；§3.1 |
| `test_unrelated_hidden_program_…`（`KeyError: helper-tool.exe`） | **生产代码** | 同 §2.1：用户批准不作用于被批准的文件 | §2.1 |
| `test_a_name_alone_never_vouches_for_a_hidden_process`（`KeyError: tool.exe`） | **生产代码** | 同上 | §2.1 |
| `test_unrecognised_programs_anywhere_…`（`KeyError: portable.exe`） | **生产代码** | 同上 | §2.1 |
| `test_a_reviewed_item_cannot_be_relabelled_as_rule_proven`（`KeyError: tool.exe`） | **生产代码** | 同上 | §2.1 |
| `test_link_swapped_in_after_the_last_check_…`（原因是“内容已改变”，不是“链接/越界”） | **生产代码** | Windows 删除路径先比对快照、后检查句柄真实位置；POSIX 路径顺序相反（§2.2） | §2.2 |
| `test_path_tool_never_picks_a_program_planted_…` | 测试夹具不跨平台 | 夹具生成无扩展名的 POSIX 脚本 `npm`；Windows 只按 PATHEXT 找 `npm.cmd` 等，生产行为正确 | §3.2 |
| `test_gui_process_policy_starts_programs_by_absolute_path_only` | 测试夹具不跨平台 | 同上（`go`） | §3.2 |
| `test_windows_refuses_junction_inserted_before_nested_directory_open` | 测试隔离 | 夹具用 `cmd.exe mklink /J` 建 junction，被 `conftest.py` 的“禁止启动外部程序”拦截 | §3.3 |
| `test_deletion_workflow.py` 在 runner 上耗时约 14 分钟 | 测试隔离 | 与 §3.1 同源：每个测试都会扫描 runner 上由环境变量指向的真实目录（CI 环境明确有 `UV_CACHE_DIR=D:\a\_temp\setup-uv-cache`，runner 镜像还带有 `ANDROID_HOME` 等）。具体是哪些目录占了时间未在 Windows 上测量 | §3.1 |

生产行为保持不变：扫描后仍分为可删除（直接删除）、不能删除、不确定（AI 或人工批准后删除）三类，没有按子文件夹一刀切禁删。

## 2. 生产代码修复

### 2.1 批准的决定必须仍指向被批准的文件（`core/_user_rules_impl.py:1028-1064`）

**问题。** 用户或 AI 批准一个文件后，`_portable_path_value` 会把路径改写成可移植形式，规则匹配时再用本机环境变量展开。改写时会按 Windows 惯例推断：`X:\Users\<名>\AppData\Local\…` 一律写成 `%LOCALAPPDATA%\…`，不检查本机的 `%LOCALAPPDATA%` 是否真指向那里。两者不一致时会出两个问题：

1. 被批准的文件展开后对不上，批准不生效，文件一直停在“不确定”；
2. 规则展开到另一个路径，相当于“批准”了一个从未看过的同名文件。

runner 上 pytest 临时目录在 `C:\Users\runneradmin\AppData\Local\Temp\pytest-…`，而夹具把 `LOCALAPPDATA` 指向临时 home，于是 `…\Data\tool.exe` 被存成 `%LOCALAPPDATA%\Temp\pytest-of-%USERNAME%\…\data\tool.exe`，展开后落在临时 home 下。Linux 上路径不以 `C:\Users\` 开头，不触发这条推断，所以只在 Windows 失败。

真实场景同样会触发：

- 用户 profile 迁到 `D:\Profiles\alice`，或重建为 `C:\Users\alice.000`，旧的 `C:\Users\alice` 仍在磁盘上；
- 管理员清理另一个账户的文件夹。

用 ntpath 语义重放“profile 迁到 D: 盘”的场景（脚本见 §5）：

| | 存储的规则值 | 匹配 C:\ 上被批准的文件 | 匹配 D:\ 上未看过的同名文件 |
| --- | --- | --- | --- |
| 修复前 | `%LOCALAPPDATA%\Temp\old\tool.exe` | 否 | **是** |
| 修复后 | `c:\users\alice\appdata\local\temp\old\tool.exe` | 是 | 否 |

**修复。** 候选的可移植形式仍按“根目录越具体越优先”排序，但每个候选要在本机展开回原路径才采用。展开不回原路径时：

- 文件在本机存在：试下一个候选，全都不行就存精确路径；
- 文件不在本机（例如从别处带来的规则）：保留原来的惯例转换。

跨机器、跨用户的可移植约定因此不变，`test_conventional_local_temp_rule_survives_redirected_temp` 仍然成立。存在性检查只在“展开不回原路径”这一少见情况下才做一次 `lexists`，正常路径没有额外 I/O。

**回归测试。** `tests/test_rules.py:246` `test_a_decision_about_a_file_on_this_computer_keeps_naming_that_file`：profile 迁到 D: 盘后，批准 C: 上的旧文件，用户决定和 AI 决定都要只作用于该文件、不作用于 D: 上的同名文件。它与同文件其他测试一样使用 Windows 路径，**只在 Windows 上有意义**：在 Linux 上能通过，但不经过 Windows 分支。Windows 语义已用 ntpath 重放验证（§5）。

### 2.2 链接被换进路径时，报“链接/越界”而不是“内容已改变”（`platform/windows/exact_cleanup.py:196-200`、`:307`、`:426`）

**问题。** 三个 Windows 变更操作（永久删除文件、移入回收站、删除目录树）在打开句柄后，都是先比对扫描快照，再检查句柄的真实路径（`GetFinalPathNameByHandle`）是否在批准的根目录内。路径中的文件夹被换成链接后，打开的是根目录外的另一个文件：快照先对不上，于是界面显示“扫描后内容已改变”。POSIX 分支（`_purge_exact_file_posix`）则是先查链接和边界，所以 Linux 上显示正确的原因。

**修复。** 三处都改为先 `_require_handle_in_boundary`，再比对快照。两项检查都仍在任何变更之前执行，任一失败都拒绝，安全性不变，只是先报出的原因更准确。

**回归测试。** `tests/test_exact_cleanup_safety.py:156` `test_a_link_swapped_into_the_path_is_reported_as_a_boundary_escape`，按三种操作参数化。它用桩模拟 Windows 句柄，Linux 上也执行 Windows 分支，并断言界面原因是“路径被替换成链接或离开了扫描范围，安全检查拒绝”、且没有发生任何变更。把 `exact_cleanup.py` 换回 `a3f6da9` 版本后，三个参数全部失败（已验证）。

## 3. 测试隔离修复

### 3.1 `ws` 夹具不再读取宿主目录（`tests/test_deletion_workflow.py:73-113`、`:185-189`、`:210`）

- **环境变量**：只保留 `DEVCLEAN_DATA_DIR`（conftest 已指向 `tmp_path`）和 `PYTEST_CURRENT_TEST`，其余全部删除，再把 `USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA`、`TEMP`、`TMP`、`PROGRAMDATA`、`SYSTEMROOT`、`WINDIR` 指向 `tmp_path`，`PATH` 置空。之前 `WINDIR` 仍是真实的 `C:\Windows`，Edge 规则会把 `%WINDIR%\Temp` 当作更新日志根。
- **不经环境变量的 Windows 来源**：
  - Downloads Known Folder（`triage.downloads_path`）指向夹具的 Downloads；
  - Edge 策略注册表（`edge_cleanup._edge_policy_paths`）返回“无策略”；
  - Visual Studio 安装器缓存位置的 HKLM 查询（`visual_studio_installer_cleanup.first_hklm_string_value`）返回“确定不存在”。最后这项沿用 `test_scan_root_snapshot.py`、`test_ordinary_installer_cleanup.py` 已有的打桩方式。
- **守卫**：`_scan` 在扫描前断言所有已知根和扫描根都在 `tmp_path` 内；一旦混入宿主目录，就给出列出这些路径的明确失败，而不是悄悄扫描 14 分钟。

在 Linux 上把宿主变量 `UV_CACHE_DIR`、`ANDROID_HOME` 指向临时目录外的文件夹做对比：旧夹具会把宿主的 uv 缓存同时纳入已知根和扫描根，新夹具下为空；在新夹具里人为注入外部目录，守卫会报错。

### 3.2 PATH 工具测试使用 Windows 能启动的文件名（`tests/test_cleanup_safety_fixes.py:52`、`:138`、`:183`、`:211`）

生产代码 `path_tool` 在 Windows 上对裸程序名只按 PATHEXT 查找（npm 在 Windows 上就是 `npm.cmd`，见 `core/npm_maintenance.py:663`），这是正确行为。修改只在测试侧：`_executable` 在 Windows 上生成 `<名>.cmd`（从不执行），并把两个测试的 `PATHEXT` 固定为 Windows 默认值，不依赖宿主设置。

修改后，“当前目录里植入的程序不会被选中”在 Windows 上也真正被检验了。以前植入的是无扩展名文件，即使搜索了当前目录也不会被找到，那条断言在 Windows 上其实是空检验。在 Linux 上用 `os.name == "nt"` 的替身重放这两个测试：旧夹具精确重现 CI 的两个失败，新夹具通过。

### 3.3 junction 在进程内创建，不放开任何子进程（`tests/test_exact_cleanup_tree_pinning.py:121-142`、`:165`）

`cmd.exe /c mklink /J` 改为 `_winapi.CreateJunction(目标, 链接)`。这是 CPython 自带的 Windows 函数，CPython 自己的测试也用它建 junction。`conftest.py` 的“禁止启动外部程序”保持原样，没有加任何例外。辅助函数 `_junction_inside` 先断言链接和目标都在本测试的 `tmp_path` 内，建好后再断言它确实是指向目标的 junction。

按 CPython 3.13 `Modules/_winapi.c` 源码：该函数会临时启用 SeRestorePrivilege 并在返回前恢复，会触发 `_winapi.CreateJunction` 审计事件，并自己创建链接目录。它结束时用 `GetLastError()` 判断成败，非管理员账户可能在 junction 已建成时仍报 `ERROR_NOT_ALL_ASSIGNED`（1300）。因此辅助函数只容忍这一个错误码，且前提是 junction 已存在。CI 的 runneradmin 是提权管理员，不走这条分支。

## 4. 验证结果（Linux）

| 检查 | 结果 |
| --- | --- |
| `ruff check src scripts tests` | 通过 |
| `python -m mypy --platform win32`（CI 同一配置，357 个文件） | 通过 |
| 三个失败文件 `test_deletion_workflow.py`、`test_cleanup_safety_fixes.py`、`test_exact_cleanup_tree_pinning.py` | 19 + 15 + 3 通过，2 个需要真实 Windows 的测试跳过（与基线相同） |
| 全量 pytest（修复前，`a3f6da9` 副本） | 232 失败 / 924 通过 / 30 跳过 |
| 全量 pytest（修复后） | 232 失败 / 928 通过 / 30 跳过 |
| 失败集合对比 | 完全相同（无新增、无减少），失败信息逐字一致；多出的 4 个通过是新增的 4 个测试 |

这 232 个失败是已知的 Linux 基线：这些测试使用 Windows 路径语义（如 `C:\…`、`PureWindowsPath` 绝对路径判断），在 Linux 上本来就失败，与本轮无关。

## 5. 复现与佐证脚本（不在仓库内）

均在会话临时目录中，只操作字符串或 `tmp_path`：

- ntpath 重放 `_portable_path_value`：重现 runner 布局下 `tool.exe` 被存成 `%LOCALAPPDATA%\Temp\pytest-of-%USERNAME%\…` 且不匹配；修复后存精确路径并匹配；不在本机的 Alice 路径仍按惯例转为 `%USERPROFILE%`。
- ntpath 重放“profile 迁到 D: 盘”：结果见 §2.1 表格。
- `exact_cleanup.py` 换回旧版本运行新测试：3 个参数全部失败。
- `os.name == "nt"` 替身重放 PATH 工具测试：结果见 §3.2。
- 宿主变量泄漏对比与守卫注入：结果见 §3.1。

## 6. 未在 Windows 上实跑的部分

以下结论来自源码分析、Linux 运行和 Windows 语义重放，**需要以下一次 Windows CI 为准**：

1. 11 个 CI 失败在 Windows 上是否全部转绿。特别是 `test_link_swapped_in_after_the_last_check_…`（依赖真实符号链接和 `GetFinalPathNameByHandle`）和 `test_windows_refuses_junction_…`（Linux 上跳过）。
2. `_winapi.CreateJunction` 在 runner 上的实际行为，以及非管理员账户下 1300 的容忍分支。
3. `test_deletion_workflow.py` 的耗时是否从约 14 分钟降到秒级，以及 14 分钟具体由哪些宿主目录造成。
4. `_scan` 守卫在 runner 上是否通过。若 Windows 上还有本轮没发现的宿主目录来源，这个守卫会失败并列出具体路径。这是有意为之：宁可明确失败，也不悄悄扫描宿主目录。
5. 新增的 `test_a_decision_about_a_file_on_this_computer_keeps_naming_that_file` 在 Windows 上的真实结果（Linux 上不经过 Windows 分支）。

另：CI 日志中 `test_cleanup_journal.py` 单个测试约 24 秒。经查它在 `tmp_path` 内写入 130 个批次的 SQLite 日志，没有读取宿主目录，属于 Windows 上事务落盘较慢，本轮未改动。

## 7. 改动文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/devclean/core/_user_rules_impl.py` | 生产 | §2.1 |
| `src/devclean/platform/windows/exact_cleanup.py` | 生产 | §2.2 |
| `tests/test_deletion_workflow.py` | 测试隔离 | §3.1 |
| `tests/test_cleanup_safety_fixes.py` | 测试夹具 | §3.2 |
| `tests/test_exact_cleanup_tree_pinning.py` | 测试夹具 | §3.3 |
| `tests/test_exact_cleanup_safety.py` | 新增回归测试 | §2.2 |
| `tests/test_rules.py` | 新增回归测试 | §2.1 |
| `docs/windows-ci-fixes-2026-11-10.md` | 文档 | 本文件 |
