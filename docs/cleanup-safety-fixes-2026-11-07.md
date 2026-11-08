# 清理安全修复记录（2026-11-07）

依据：[`docs/cleanup-safety-audit-2026-11-07.md`](cleanup-safety-audit-2026-11-07.md)（审计报告正文未改动，SHA-256 与修复前一致）与审计规程 [`docs/cleanup-safety-audit-prompt.md`](cleanup-safety-audit-prompt.md)。

| 项 | 内容 |
| --- | --- |
| 基线 commit | `2089fa6`；本轮在上轮（扫描提速、普通安装包删除）未提交改动之上修复 |
| 提交状态 | 未 commit、未 push；全部改动留在工作树 |
| 完成时间 | 2026-11-07T10:42Z（本地 04:42 CST） |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13.18，pytest 8.4.2，mypy 2.2.0（`--platform win32`），ruff 0.15.21；`winreg` 为临时目录中的桩模块。**本轮没有在 Windows 上运行任何东西，不能据此宣称 Windows 通过** |
| 测试数据 | 只用 `tmp_path` 等临时夹具；没有删除真实用户数据，没有调用卸载器或清理系统缓存，测试不再启动宿主机上的包管理器 |

## 1. 摘要

| 审计项 | 结论 | 主要改动 | 回归测试（`tests/test_cleanup_safety_fixes.py` 等） |
| --- | --- | --- | --- |
| L-1 下载安装包被当作程序本体 | 已修复（产品行为调整） | `core/triage.py:511`、`:884`、`:898`；`platform/windows/known_folders.py` | Downloads 安装包进入用户决定并能被实际删除；安装目录、子目录、Package Cache、Windows Installer、其他用户 Downloads 仍受保护；符号链接是边界 |
| H-1 按裸名启动外部程序 | 已加防护；Windows 实测缺口见 §3 | 新增 `platform/windows/executables.py`；`platform/windows/subprocess_policy.py:42`；`ui/launcher.py`；16 处 `shutil.which` | 当前目录里植入的同名程序不会被解析或执行；Windows 工具解析到 System32；字符串命令被拒绝 |
| M-1 扫描后归属变化 | 部分修复：配置文件可见的变化已覆盖，剩余缺口见 §4 | `core/_application_cleanup_impl.py:159-222`；9 个包管理器查询；`ui/product_app.py:295` | 扫描后改了 `.npmrc` 的 npm 缓存被拒绝删除，且只重新询问读取该文件的 npm/pnpm |
| M-2 无关 CLI 启动 | 已修复 | `core/application_cleanup.py:992-1120` | 应用项删除后，下一个普通文件不再启动任何程序 |
| M-3 异常时间戳中止扫描 | 已修复；Windows 实测缺口见 §6 | `core/_application_cleanup_impl.py:225`；`core/triage.py`；`core/whole_tree_policy.py:89`；`ui/product_app.py:480-490` | 不可表示的时间视为未知；单个文件分类失败只跳过并计数，扫描仍完成 |
| M-4 文档策略矛盾 | 已修复；并更正审计报告的一处说法（§2） | `README.md`；`docs/generic-review-routing-reaudit.md`；`docs/review-lane-policy.md`；`core/triage.py:631` | — |
| L-2 失败后计时一直“计时中” | 已修复 | `ui/app.py:1215`、`:1993`；`ui/product_app.py:504-520` | `scan_error` 会停止计时并标明“扫描失败” |
| L-3 测试启动真实包管理器 | 已修复 | 新增 `tests/conftest.py` | 全量测试后临时 CWD 为空（此前会出现 `E:\npm-cache` 等目录） |
| 附带发现：失败原因标错 | 已修复 | `core/application_cleanup.py:1093`；`core/postscan_cleanup.py:764`；`ui/app.py:_reason_of` | 归属变为 KEEP、程序运行中、扫描后已改变三种原因分别显示 |

## 2. 审计报告更正（M-4 中关于临时目录的部分）

审计报告 M-4 称临时目录中的文件经路由后进入可删除的 AI 通道。复核发现，这在**默认配置下不成立**。产品扫描使用打包的已知根目录，其中 `user-temp`（`%TEMP%`）是 REPORT_ONLY 已知根，临时目录文件在该分支即被标为 `known_root, system_managed`，属于硬保护。

审计时的复现没有传入已知根，因此落到了后备的 `temp_root` 分支。复核输出（临时夹具，`TEMP` 指向夹具）：

```
with product known roots  : lane=REPORT_ONLY exec=NONE tags=('known_root', 'system_managed', ...)
without known roots       : lane=AI_REVIEW exec=USER_CHOICE_DELETE tags=('temp_root', 'unknown', ...)
```

只有关闭 `include_known_cleanup_roots`，或 TEMP 不在已知根中时，才会走后备分支。本轮让后备分支与已知根保持一致：去掉 `unknown` 标签（`core/triage.py:631`），使它既不进入 AI 列表，也不能被学习到的文件规则放行。M-4 中关于 README 与重审文档的矛盾仍然成立，并已修正（见 §8）。

## 3. 产品行为：下载的旧安装包（L-1）

> 后续更新：本节“只有直接放在下载文件夹里的安装包才交给用户决定、子文件夹里的程序一律保护”的规则，已被 [`deletion-workflow-fixes-2026-11-07.md`](deletion-workflow-fixes-2026-11-07.md) 的按文件内容判断取代。

### 新规则

直接保存在当前用户“下载”文件夹中的独立安装包，进入“需要判断”列表：

- 文件夹位置：`%USERPROFILE%\Downloads`，以及 Windows Known Folder `FOLDERID_Downloads` 的重定向位置；
- 扩展名：`.exe`，以及规则中的 `installer_suffixes`（`.msi`、`.msix`、`.msixbundle`、`.appx`、`.appxbundle`、`.iso`）；
- 分类：`INSTALLERS_DOWNLOADS`，通道 `USER_REVIEW`，执行策略 `USER_CHOICE_DELETE`；
- 理由：“下载文件夹中的独立安装包：删除只移除这个文件，不会卸载已安装的程序，也不会运行任何安装或卸载程序；以后需要时可重新下载。如果它其实是你直接运行的便携程序，请保留”。

它们**默认不勾选**。用户在右侧选中并点“由你决定”选“是”后，项目移到左侧并勾选；点“永久清理所选”后，走与普通文件相同的句柄绑定删除（`purge_exact_file`）。删除前检查不启动任何外部程序。

### 不因此放开的位置

以下位置仍按程序本体保护，测试逐项覆盖：

- 下载文件夹的子目录（可能是解压出的便携程序）；
- `%LOCALAPPDATA%\Programs`、`Program Files`；
- `ProgramData\Package Cache`（WiX/Burn 缓存的安装包，是已安装产品的卸载入口）；
- `%SystemRoot%\Installer`（另外它从不被扫描，且 `Windows` 在系统锚点拒绝名单中）；
- 其他用户的 `Downloads`；
- 用 `..` 绕出下载文件夹的路径；
- 打包运行时 DevClean.exe 自身。

下载文件夹中指向别处的符号链接，扫描时是边界记录，不会成为文件候选。

### 四类对象的区分

| 类别 | 判定依据 | 行为 |
| --- | --- | --- |
| 普通下载的安装文件 | 下载文件夹的直接子文件 + 安装包扩展名，且没有应用规则归属 | 用户决定后作为普通文件删除，不启动任何程序 |
| 已安装程序文件 | 其他位置的 `program_payload` 后缀、已安装扩展、应用状态 | 硬保护，不进入删除或 AI 列表 |
| 包管理器缓存 | 已审计的应用规则与厂商根 | 只通过官方命令清理（如 `go clean -cache`、`dotnet nuget locals … --clear`），或保持保护 |
| Windows / Visual Studio Installer 系统缓存 | `%SystemRoot%\Installer`、VS Installer 包缓存（`CachePath` 策略可重定向） | 不扫描，或 KEEP，守卫拒绝 |

### 动作、原因、大小与失败原因的展示

- 结果列表新增“动作”与“原因”两列（`ui/app.py:129` `ROW_COLUMNS`，`ui/modern_app.py`）。动作为“删除此文件”“删除整个目录”“运行官方清理命令”，或“待你决定：…”；大小与位置列保留。
- 每个未完成项的原因都保留下来：
  - 准备阶段与批次级失败以 `(路径, 原因)` 回传；
  - `CleanupExecutionResult.failures` 携带执行阶段逐项的异常（`core/postscan_cleanup.py:583`）；
  - 状态栏按原因分组计数，该行“原因”列显示“上次清理未完成：…”，直到下次扫描。
- 删除前检查原来对“扫描后归属变为 KEEP”也报“owning application is running”，界面又把所有拒绝都显示为“扫描后内容已改变”。现在 `process_guard_refusal` 返回具体原因（`PROTECTED_OWNER_REFUSAL` / `RUNNING_OWNER_REFUSAL`），界面分别显示：
  - “扫描后它已归属受保护的应用数据，已拒绝删除”
  - “所属程序正在运行，请关闭后再清理”
  - “扫描后内容已改变，安全检查拒绝”

### 已知限制

重定向到用户目录之外的 Downloads（例如 `D:\Downloads`）在分类时会被识别，但只有在扫描根覆盖它时才会被扫描（用户目录、已知根或 `additional_paths`）。本轮没有扩大扫描范围。

## 4. 逐项修复

### H-1 按裸名启动外部程序

**修复**

- 新增 `platform/windows/executables.py`，作为唯一的程序解析器：
  - `system_tool` / `resolve_program`：`powershell`、`tasklist`、`cmd`、`wsl`、`dism`、`cleanmgr` 一律解析到 `%SystemRoot%\System32`（沿用仓库已有的 DISM/PowerShell 绝对路径做法）；
  - `path_tool`：只在 PATH 中的**绝对**目录里查找，跳过 `.`、空项和相对项。显式位置必须是绝对路径。
- GUI 进程策略（`platform/windows/subprocess_policy.py:42`）对每个子进程先把 `argv[0]` 换成绝对路径。找不到即按“未安装”处理（`FileNotFoundError`，各调用点原本就按工具不可用、失败即保护的方式处理）。相对路径与字符串命令被拒绝。
- 打包入口原本就先安装此策略；`ui/launcher.py` 的 `main()` 也安装一次（幂等），使源码运行启动器时同样受保护。
- 16 处 `shutil.which`（npm/pnpm/pip/go/NuGet 维护、Codex、Claude、Cypress、Git、Podman、Meson、Bazel、Unreal、Cargo、WSL、Hugging Face）改为 `path_tool`。原因是 Windows 上 `shutil.which` 会把当前目录排在最前，调用方随后会把命中结果转成绝对路径，GUI 策略也就无法再拦截。

**验证（Linux）**

- 当前目录植入的 `npm`/`go` 可执行文件：`path_tool` 返回 `None`，`_resolve_npm_tool({"PATH": "."})` 抛出“未找到 npm CLI”，GUI 策略抛出 `FileNotFoundError`。植入文件写标记的脚本从未被执行（标记文件不存在）。
- PATH 中的绝对受信目录可以正常解析。
- `resolve_command(["powershell.exe", …])` 得到 `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`。

**Windows 实测缺口**

- 未在真实 Windows 上验证打包 EXE 的子进程确实全部经过该策略，也未验证以绝对路径启动 `.cmd` 的行为。
- 植入同名程序这一利用场景本身未在 Windows 上复现；修复后，它在代码路径上已没有按名称搜索的入口。
- 不经 GUI 入口直接调用 Python API 时，扫描期的 CLI 查询仍以裸名传给 `subprocess`（不受 GUI 策略约束）。这不影响用户使用的 DevClean.exe。

### M-1 扫描后归属变化与缓存失效

**修复**

- 9 个会启动包管理器的位置查询改为 `ConfigCachedQuery`（`core/_application_cleanup_impl.py:159`）。缓存的答案只在“环境变量 + 该工具自己的配置文件 stat 签名”不变时复用；签名变化时只重新询问这一个工具。涉及：
  - npm：用户与全局 npmrc；
  - pnpm：全局 rc 与用户 npmrc；
  - Yarn：`.yarnrc`、`.yarnrc.yml`；
  - uv：`uv.toml`；
  - pip：`pip.ini` 与 `PIP_CONFIG_FILE`；
  - Conda：`.condarc` 与 `CONDARC`；
  - Go：`go env -w` 写入的 env 文件；
  - NuGet：`NuGet.Config`。
- 每次扫描开始先清空全部进程状态与发现缓存（`ui/product_app.py:295`），同一会话的第二次扫描不再沿用上一次的状态。
- 删除前检查（`process_guard_refusal`）用这些缓存重新判断归属；环境变量、settings 文件、注册表与目录结构本来就每次重新读取。

**验证**

回归测试：扫描后用户把 npm 缓存移到某文件所在目录，并写入 `.npmrc`。删除前检查返回 `PROTECTED_OWNER_REFUSAL`，只重新询问读取 `.npmrc` 的 npm 与 pnpm，没有启动 go、uv、pip、conda、dotnet、yarn 或 PowerShell。

**剩余缺口（未修复，如实记录）**

- 不经过上述配置文件的变化，对“扫描时无归属”的路径仍使用扫描时的结论。例如：
  - 升级后 npm 安装目录内的内置 npmrc；
  - 未列入的其他配置位置；
  - 由运行中进程推导的根（浏览器或编辑器以 `--user-data-dir` 启动、Android SDK 的运行进程等，需要 PowerShell 才能探测）。
- 审计报告的原复现脚本只改变 npm 的回答、不改任何文件，在修复后仍被放行，这正是上述缺口。
- 不为普通文件启动 PowerShell 或包管理器，是本轮明确的要求，两者无法同时满足。缓解措施有两项：
  - 执行前仍逐项核对文件身份（file id、大小、修改与创建时间），被新启动的应用写过的文件会被拒绝；
  - 每次扫描都从新鲜状态开始。

### M-2 无关 CLI 启动

**修复**

原先两份各 37 项的平行清单（进程探测的 if 链与 `clear_process_cache` 调用列表）合并为一张表 `_APPLICATION_STATE`（`core/application_cleanup.py:998`）。删除前检查：

- 无归属路径：不刷新任何缓存，不启动程序；
- 应用归属路径：只刷新该应用自己的发现与进程状态（`_refresh_application`），不再清空其他应用的缓存。

**验证**

| 场景 | 修复前（工作树） | 修复后 |
| --- | --- | --- |
| 删除 Toolbox 下载缓存项后，下一个普通文件的删除检查 | 启动 11 个程序（conda、dotnet、go、npm、pip、pnpm、python、python3、uv、yarn） | 0 |
| npm 归属项的删除检查 | 11 | 1（只有 npm），并拒绝删除 |
| 扫描后删除 Downloads 普通文件（上轮 harness） | 0（删除阶段） | 0 |

### M-3 异常时间戳

**修复**

- `file_time_utc`（`core/_application_cleanup_impl.py:225`）把 Python 无法表示的文件时间（`OverflowError`、`OSError`、`ValueError`）当作未知；
- 用于 `triage.py` 的分类与 `_is_older_than`、`whole_tree_policy.py:89`（整树最新活动时间）以及 Codex 活动时间。
- 产品扫描线程对每条记录单独捕获异常：无法分类的文件不列出（即受保护）并计数，扫描耗时一栏显示“N 个文件无法分类，已按保护处理、不列出”。
- 外层改为捕获所有异常并报告 `scan_error`，避免线程静默退出、界面一直停在“正在扫描”。

**验证（Linux）**

- 修改时间约为 14645 年的记录：分类正常，没有 `stale_metadata` 标签。
- 合成扫描（正常 + 异常时间 + 一条分类时抛出 `KeyError` 的记录）：收到 `scan_done`，没有 `scan_error`，`unclassified_files == 1`。
- 审计复现脚本由 `['scan_error']` 变为 `['scan_done']`。

**Windows 实测缺口**

未在 Windows 上用真实文件验证 1970 年以前、3000 年以后的时间实际触发哪种异常，以及这类文件的出现频率。修复按异常类型统一处理，不依赖具体类型。

### M-4 文档策略矛盾

- README 改为描述实际行为：
  - “需要判断”列表包含下载文件夹中的独立安装包，删除只删文件、不卸载、不启动程序；
  - 硬保护对象（安装目录中的程序本体、已安装扩展、应用状态、临时目录、系统与厂商目录，包括 Windows Installer 与 VS Installer 缓存）不进入删除或 AI 队列；
  - 无法证明安全或无法识别的普通文件，不勾选、不自动删除，只列在“需要判断”一侧；
  - 每行显示动作、大小、位置、原因，失败原因写在状态栏和该行。
- `docs/generic-review-routing-reaudit.md` 顶部加注“后续变更（当前行为）”，原文保留。
- `docs/review-lane-policy.md` 补充下载安装包这一用户判断示例。
- 临时目录后备分支与已知根保持一致（见 §2）。

### L-2 扫描失败后的计时

基础窗口在 `scan_error` 时调用新钩子 `_scan_failed()`（`ui/app.py:1993`）；产品窗口停止计时并显示“扫描耗时：X 秒（扫描失败）”（`ui/product_app.py:504`）。测试用伪事件队列驱动真实的 `_drain_events` 验证。

### L-3 测试隔离

新增 `tests/conftest.py`（自动生效）：

- 每个测试都拒绝启动外部程序，按“未安装”处理。唯一例外是测试自身的 Python 解释器，`test_codex_history.py` 用它运行假的 Codex 服务端。
- 把 `DEVCLEAN_DATA_DIR` 指向临时目录。
- 在测试开始前清空进程与发现缓存。

修复前全量测试会在 CWD 留下 `C:\Users\person\AppData\Local\Temp`、`D:\pnpm-home`、`E:\npm-cache\_logs\*.log`；修复后临时 CWD 为空。

## 5. 测试与检查结果

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（Linux，临时目录作为 CWD，2026-11-07T10:36Z） | 收集 1141：**882 通过，231 失败，28 跳过** |
| 与审计基线对比 | 231 个失败用例与审计时完全相同，报错逐条一致；无新增失败、无修复项（这些用例按审计报告 §4 归因于 Linux 上的 Windows 路径/API 机制） |
| 新增回归测试 | `tests/test_cleanup_safety_fixes.py` 28 个，全部通过；上轮 8 个仍全部通过 |
| `ruff check src scripts tests` | 通过 |
| `ruff format --check`（本轮新文件） | 通过；既有 34 个文件的格式漂移未动 |
| `mypy`（strict，`--platform win32`） | 349 个文件，无问题 |
| 扫描吞吐（3,202 文件临时夹具） | 原始基线 173–179 files/s；上轮 678–701；本轮 710–712 |
| 分类一致性 | 上述夹具 1,340 条分类结果与原始基线逐字节一致；加入 Downloads 安装包的夹具中，差异恰好是 4 个直接位于 Downloads 的安装包变为 `USER_REVIEW`，子目录中的 exe、已安装程序与 `.zip` 不变 |

**关于 Windows**：最近一次真实 Windows 证据仍是审计引用的 CI run 39540236448（只覆盖基线 `f2dbfd3`，1105 通过）。本轮改动没有在 Windows 上运行，**不能宣称 Windows 通过**。231 个 Linux 失败中包含与本轮相关的 Windows 语义用例，例如：

- `tests/test_claude_cleanup.py:238` 设置变更刷新；
- 各 `*_process_guard_*`；
- Toolbox、Cursor updater 守卫。

对它们的结论来自代码推理与 Linux 复刻：

- 所有守卫用例都只替换应用的存活探测，新表格在调用时按名称查找，替换仍然生效；
- 应用归属项仍会刷新该应用的发现。

## 6. 未验证范围与 Windows 实测缺口

- 本轮全部改动未在 Windows 上运行。需要 Windows CI 或真机确认：
  - 全量测试；
  - 打包 EXE 的子进程解析；
  - Known Folder Downloads 的读取；
  - 句柄绑定删除对下载安装包的实际效果；
  - 以绝对路径启动 `.cmd`；
  - 1970 年以前 / 3000 年以后的文件时间。
- M-1 中不经配置文件的归属变化与由运行进程推导的归属（§4），仍是缺口。
- 重定向到用户目录之外的 Downloads 不会被自动扫描（§3）。
- 界面（Tk）没有被实际驱动。列表列、失败原因与计时的测试使用伪对象调用真实方法。
- 没有构建 EXE，没有运行 `scripts/build_windows_exe.ps1`。
- 用户报告的“删除普通安装包调用原应用卸载器”仍未在 Windows 上复现。本轮消除了已知的唯一机制（删除时按裸名启动外部程序），并让普通安装包删除完全不启动程序。

## 7. 改动文件（本轮）

- 新增：`src/devclean/platform/windows/executables.py`、`tests/conftest.py`、`tests/test_cleanup_safety_fixes.py`、本文档。
- 核心：
  - `core/_application_cleanup_impl.py`、`core/application_cleanup.py`、`core/triage.py`、`core/postscan_cleanup.py`、`core/whole_tree_policy.py`；
  - 9 个 `*_cleanup.py` 的包管理器查询：npm、pnpm、yarn、uv、pip、conda、go、nuget；
  - 16 个使用 `shutil.which` 的模块。
- 平台：`platform/windows/subprocess_policy.py`、`platform/windows/known_folders.py`。
- 界面：`ui/app.py`、`ui/modern_app.py`、`ui/product_app.py`、`ui/product_vendor_app.py`、`ui/launcher.py`。
- 文档：`README.md`、`docs/generic-review-routing-reaudit.md`、`docs/review-lane-policy.md`。

按要求，修复到此为止：未提交、未推送，没有开始新一批工作。
