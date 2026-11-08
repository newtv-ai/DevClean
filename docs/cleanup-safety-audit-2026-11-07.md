# 工作树清理安全审计（扫描提速 / 普通安装包删除改动）

Audited: 2026-11-07

规程：[`docs/cleanup-safety-audit-prompt.md`](cleanup-safety-audit-prompt.md)（本次新建；仓库内此前没有审计提示词或审计规程）。

## 1. 审计记录

| 项 | 内容 |
| --- | --- |
| 基线 commit | `2089fa6113a55d0251cc78d8302d78ed2787fe48` “Sync verified Windows release from f2dbfd3”（`src/`、`tests/` 与 `f2dbfd3` 逐字节相同，`git diff --stat f2dbfd3 2089fa6 -- src tests` 为空） |
| 审计 diff 范围 | `git diff 2089fa6`（40 个已跟踪文件，+111/−4）加未跟踪文件 `tests/test_ordinary_installer_cleanup.py`、`tests/test_scan_root_snapshot.py`。**包含上轮全部未提交改动**。本次新建的两份文档不属于被审代码 |
| 工作树保护 | 未使用 stash/checkout/reset/clean；审计前把 `git diff` 与两个未跟踪测试备份到临时目录并记录 SHA-256；基线对比用 `git archive 2089fa6` 导出副本 |
| 审计时间 | 开始 2026-11-07T07:41:39Z（本地 01:41:39 CST），证据收集结束 2026-11-07T08:56:45Z |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13.18，pytest 8.4.2，mypy 2.2.0，ruff 0.15.21；`winreg` 为临时目录中的桩模块（所有 `OpenKey` 抛 `FileNotFoundError`）。**未在真实 Windows 上运行任何东西** |
| 外部 Windows 证据 | GitHub Actions run 39540236448（`windows-latest`，Python 3.13.16，针对 `f2dbfd3`）：`1105 passed in 168.52s`。只覆盖基线代码，不覆盖未提交改动 |

被审改动概要（上轮）：

- `core/_application_cleanup_impl.py`：`_normalize` 结果缓存；新增 `scan_root_snapshot()` 与 `@snapshot_roots`，让应用根发现在一次扫描内只算一次。
- 37 个 `core/*_cleanup.py`：给根发现函数加 `@_impl.snapshot_roots`。
- `ui/product_app.py`：`_scan_worker` 运行在扫描快照内。
- `core/application_cleanup.py`：`process_guard_allows` 先判断归属，无归属路径直接放行，不再先清空全部缓存。
- `core/claude_cleanup.py`：新增 `clear_claude_settings_cache()`。
- 两个新测试文件（共 8 个用例）。

## 2. 结论摘要

- 没有发现默认配置下会直接误删、删除系统 Installer 缓存或调用卸载器的路径。全仓搜索未发现任何调用卸载器/安装器（`msiexec`、`unins*`、`Update.exe --uninstall` 等）的代码；上轮把“删除普通安装包调用卸载器”归因于裸名启动外部程序，这一点**仍未在 Windows 复现**。
- 上轮改动本身引入 **2 个中危问题**：删除前守卫对“扫描后才出现的归属”失去复核（M-1，fail-open）；“普通文件删除不启动程序”只在部分情况下成立（M-2）。
- 既有问题中最重要的是 **H-1**：扫描与守卫按裸名启动 PowerShell 和各包管理器 CLI。Windows 会先在 DevClean.exe 所在目录和当前目录查找同名程序，而 DevClean.exe 通常就在下载目录里运行。
- 扫描提速得到复核：同一临时夹具上 134–169 → 655–687 files/s，1,340 条分类结果逐字节一致。
- 231 个 Linux 失败全部按实际报错归因到 4 类平台机制；没有一个由上轮改动引起，也没有无法解释的失败。同一套源码在真实 Windows CI 上全部通过（见第 4 节）。

| 级别 | 已确认 | 待验证 | 合计 |
| --- | --- | --- | --- |
| 严重 | 0 | 0 | 0 |
| 高 | 0 | 1 | 1 |
| 中 | 3 | 1 | 4 |
| 低 | 3 | 0 | 3 |
| 合计 | 6 | 2 | 8 |

## 3. 发现

### H-1 扫描与删除守卫按裸名启动外部程序，Windows 会先在程序所在目录和当前目录查找

- **级别 / 状态 / 来源**：高 / 待验证（代码路径已确认，Windows 上的劫持未实测） / 既有
- **位置**：
  - `core/npm_cleanup.py:445`（`npm.cmd`）、`core/pnpm_cleanup.py:453`、`:482`（`pnpm.cmd`）、`core/yarn_cleanup.py:340`（`yarn.cmd`）
  - `core/uv_cleanup.py:242`（`uv.exe`）、`core/go_cleanup.py:326`（`go.exe`）、`core/nuget_cleanup.py:339`（`dotnet.exe`）、`core/conda_cleanup.py:317`（`conda.exe`）
  - `core/pip_cleanup.py:243-245`（`py.exe`、`python.exe`、`pip.exe`）、`core/_application_cleanup_impl.py:709`（`tasklist`）
  - `powershell.exe`：33 个文件中 46 处调用点
  - GUI 入口 `scripts/devclean_gui_entry.py:7` 安装 `platform/windows/subprocess_policy.py:23-25`，给子进程加 `CREATE_NO_WINDOW`
- **触发条件**：DevClean.exe 所在目录或进程当前目录（双击运行时两者都是 exe 所在目录，README 的用法是“下载 DevClean.exe 后运行”）中存在与上述名字相同的文件，然后执行扫描。删除时也会触发，见 M-2。
- **证据**：
  - Linux 上用只记录 argv 的 `Popen` 桩复现：对 Downloads 中一个无归属文件做一次分类，启动 11 个程序：`go env …`、`dotnet nuget locals all --list …`、`conda info --json`、`uv cache dir`、`python3 -m pip cache dir`、`python -m pip cache dir`、`pip cache dir`、`yarn --version`、`pnpm config list`、`pnpm store path --silent`、`npm config get …`。Windows 分支用的是上面列出的 `.exe/.cmd` 裸名。
  - 微软 CreateProcessW 文档（`lpCommandLine` 一节）原文：文件名不含目录时依次搜索 “The directory from which the application loaded.”、“The current directory for the parent process.”，然后才是系统目录和 PATH。Python 在 Windows 上 `shell=False` 且未给 `executable` 时以 `lpApplicationName=NULL` 调用 CreateProcess。
- **影响**：下载目录里一个同名文件（例如浏览器下载的 `npm.cmd`、`go.exe`）会在扫描时被静默执行，且不弹控制台窗口。这是执行任意程序的风险，也是“删除普通安装包却启动了其他程序”的唯一已知机制。
- **建议**：只从可信位置解析可执行文件，例如用 `shutil.which` 搜索不含当前目录与程序目录的 PATH，或用 `GetSystemDirectory` 拼出 `powershell.exe`、`tasklist.exe` 的绝对路径，再以绝对路径启动。也可考虑在 GUI 入口调用 `SetDllDirectoryW("")`，并把当前目录切换到安全目录。
- **验证缺口**：
  - 未在 Windows 上实测放置同名程序后是否被执行。
  - 未确认 PyInstaller onefile 子进程的“application directory”就是 DevClean.exe 所在目录。
  - `.cmd` 经 CreateProcess 启动时的解析行为未实测。

### M-1 删除前守卫不再复核扫描后才出现的、由外部命令或进程推导出的归属（上轮引入的 fail-open）

- **级别 / 状态 / 来源**：中 / 已确认 / 上轮引入
- **位置**：
  - `core/application_cleanup.py:1127-1129`：先用缓存状态 `match_application_rule`，结果为 `None` 就直接放行。
  - 被复用的缓存，例如 `core/npm_cleanup.py:441-476` 的 `@lru_cache _effective_npm_config`（`npm config get`）；同类还有 pnpm/yarn/uv/pip/conda/go/NuGet 的配置查询，以及浏览器/编辑器的 `_running_override_roots`（PowerShell 读取运行中进程的 `--user-data-dir`）。
- **触发条件**：某个文件在扫描时不属于任何应用，并被用户或 AI 批准删除。扫描之后、点击清理之前，归属发生变化，例如 `npm config set cache` 指向该目录，或用自定义 `--user-data-dir` 启动了浏览器。
- **最小复现**（临时进程，假 `subprocess.run`，只回答 `npm config get`，不运行真实 npm）：
  1. 扫描时 npm cache = `D:\old-npm-cache`，`match_application_rule(r"E:\shared\npm-cache\_cacache\content-v2\sha512\aa\blob")` 为 `None`。
  2. 把 npm cache 改为 `E:\shared\npm-cache`。
  3. 调用 `process_guard_allows(同一路径)`。
  - 结果：工作树 `guard_allows=True`；`git archive` 基线 `guard_allows=False`；两边新鲜发现都给出 `npm-content-cache / KEEP`。
- **影响**：永久删除会作用于一个此刻已被应用规则判为 KEEP 的文件，删除不进回收站。触发需要“扫描后改配置或启动进程”这一窗口，概率不高，但它削弱了基线明确提供的安全复核。`tests/test_claude_cleanup.py:238` 只覆盖读取 settings 文件的那一类归属，而那一类上轮已处理。
- **建议**：在每次清理执行开始时统一刷新一次全部发现（一次性代价），之后逐项检查都使用这份新结果，而不是信任扫描时的“无归属”。也可以先完成本轮清理计划内的全部归属复核，再开始删除。
- **验证缺口**：浏览器 `--user-data-dir` 这类经 PowerShell 推导的归属只能在 Windows 上复现。

### M-2 “删除普通文件不启动任何程序”只部分成立；守卫的说明夸大

- **级别 / 状态 / 来源**：中 / 已确认 / 上轮修复不完整
- **位置**：
  - `core/application_cleanup.py:1109-1135`：说明文字称 “A path no application owns … is therefore allowed without starting any program”。
  - 应用归属分支调用 `clear_process_cache()`（`core/application_cleanup.py:1068`）清空所有应用的发现缓存，随后的 `match_application_rule`（`:511` 起）在第一个命中的应用处就返回。
- **触发条件**：同一次清理中，一个属于链条前段应用的项（例如 `:529` 的 JetBrains Toolbox 下载缓存）之后紧跟一个普通文件。
- **最小复现**（同样是假 `subprocess.run`）：扫描预热后依次调用 `process_guard_allows(ordinary)`、`process_guard_allows(toolbox_download_item)`、`process_guard_allows(ordinary)`。工作树第三次调用启动 11 个程序：`conda`、`dotnet`、`go`、`npm`、`pip`、`pnpm`、`python`、`python3`、`uv`、`yarn`。若应用项属于链条末端的 npm，再匹配会预热全部缓存，第三次调用为 0。基线每次都启动 11 个。
- **影响**：上轮修复只消除了“每个普通文件都重启 CLI”，没有消除“应用项之后的第一个普通文件重启 CLI”。与 H-1 叠加时仍存在删除阶段启动外部程序的路径。上轮对用户的结论也过于绝对。
- **建议**：与 M-1 一起处理，在清理开始时刷新一次、逐项不再整体清空；应用项只刷新该应用的进程存活状态。修正说明文字。
- **验证缺口**：Windows 上额外的 PowerShell 进程探测未计入。

### M-3 单个文件时间戳超出 Python 可表示范围即导致整次扫描失败

- **级别 / 状态 / 来源**：中 / 待验证（机制已确认，Windows 真实文件触发未实测） / 既有
- **位置**：
  - `core/triage.py:695`（`_application_classification`）、`:944`（`_is_older_than`）：`datetime.fromtimestamp(...)` 无保护。
  - `ui/product_app.py:472-474`：整个遍历只有一个 `try`，捕获 `(OSError, RuntimeError, ValueError)` 后发送 `scan_error` 并 `return`。
- **触发条件**：扫描范围内任意一个文件的修改时间超出平台或 Python 的 `datetime` 范围。NTFS 时间可早于 1970 年，也可晚到 30828 年。
- **证据 / 最小复现**：
  - 用合成 `ScanRecord` 替换遍历器，3 个正常文件 + 1 个 `last_write_time_ns = 400_000_000_000 * 10**9` 的文件 + 3 个正常文件，跑真实 `ProductDevCleanWindow._scan_worker`。事件只有 `['scan_error']`，内容为 `year 14645 is out of range`，没有 `scan_done`，全部结果丢弃。
  - Python 3.13 文档原文：“fromtimestamp() may raise OverflowError … and OSError on localtime() or gmtime() failure. It’s common for this to be restricted to years in 1970 through 2038.”
  - 用真实文件复现失败：Linux 文件系统把时间截断到约 2446 年。
- **影响**：一个异常时间戳让用户得不到任何扫描结果，界面只显示“扫描失败：year … is out of range”。另外，若平台抛出 `OverflowError`（不在捕获列表中），工作线程会静默结束，界面停在“正在扫描”。
- **建议**：逐文件把不可表示的时间当作“未知”（`None`）继续分类；单条记录的异常不应终止整次扫描。
- **验证缺口**：未在 Windows 上确认 1970 年前、3000 年后的时间戳实际抛出哪种异常，以及这类文件在用户目录中的出现频率。

### M-4 README 与重审文档承诺“未知对象不进入删除/AI 队列”，实际路由会把它们送进可删除的 AI 通道

- **级别 / 状态 / 来源**：中 / 已确认 / 既有
- **位置**：
  - `README.md:12-15`：“真正无法识别的对象，默认保护并且不进入删除/AI 队列”。
  - `docs/generic-review-routing-reaudit.md` 第 3 条：“generic name/suffix/category/unknown semantics -> REPORT_ONLY / protected”，并列出 “an otherwise unknown file” 与 “inferred installer/download … categories” 不再产生 USER_REVIEW/AI_REVIEW。
  - 实际路由：`ui/product_app.py:452` 对每个文件调用 `route_unresolved_file_to_ai`，`core/review_routing.py:26-37`、`:48` 把带 `unknown`/`byproduct`/`cache_directory`/`path_heuristic` 标签的文件改为 `AI_REVIEW` + `USER_CHOICE_DELETE`。
  - `core/triage.py:590-605`：临时目录文件的理由写着“单独的 mtime/年龄不能证明当前未使用，工具直接保护”，但其标签含 `unknown`。
- **证据**：
  - 临时夹具中 `%TEMP%\setup-extract\payload.dat.part` 经 triage + 路由后为 `category=USER_TEMP lane=AI_REVIEW exec=USER_CHOICE_DELETE`。
  - Downloads 中的 `tool.zip`、`setup.7z`、`pkg.nupkg` 同样进入 AI 通道。
  - `tests/test_review_routing.py:49` 明确断言 unknown 文件成为 AI 候选，而 `tests/test_generic_review_routing.py` 断言 triage 层保护它们。两套测试各自通过，产品行为取后者经路由后的结果。
- **影响**：用户和维护者对删除范围的认知与实际不符。临时目录中可能正被安装程序使用的文件，经 AI 判断或用户在“需要判断”列表中勾选后，可被永久删除；界面理由同时写着“工具直接保护”和“进入 AI 复核候选”。
- **建议**：先确定产品策略。若保留现有路由，就更新 README、重审文档和理由文案；若以文档为准，就把 `temp_root` 等标签列为硬保护，或从可复核标签中移除 `unknown`。
- **验证缺口**：Windows 上正被占用的临时文件，删除时是否因共享冲突失败，未实测。

### L-1 下载的普通安装包被归为“程序本体”，与已安装程序混为一类

- **级别 / 状态 / 来源**：低 / 已确认 / 既有
- **位置**：
  - `config/keep-rules.json:49`、`:52`、`:63-65`：`program_payload_suffixes` 含 `.iso`、`.exe`、`.msi`、`.msix`、`.appx`。
  - `core/triage.py:503-514`：理由文字为“程序本体、其载入的库，或虚拟磁盘；不是垃圾，释放这类空间要用它自己的工具”。
- **证据**：临时夹具 `Downloads\` 下的 `ChromeSetup.exe`、`node-v18.20.0-x64.msi`、`app-1.0.msix`、`foo.iso`、`unins000.exe` 等，全部为 `cat=INSTALLERS_DOWNLOADS lane=REPORT_ONLY exec=NONE tags=('program_payload', …)`。
- **影响**：没有误删风险（属于保护性失败）。但下载的旧安装包永远无法通过 DevClean 清理，内部理由还把用户引向“用它自己的工具”，即卸载已安装的应用，混淆了“安装包”与“已安装应用”。展示类别和保护标签互相矛盾。
- **建议**：区分“Downloads 等用户下载位置中的安装包文件”（可进入用户判断）与“Program Files 等位置的程序本体”（保持保护），并修正理由文案。
- **验证缺口**：无。分类逻辑只依赖后缀和路径段。

### L-2 扫描失败后“扫描耗时”一直显示“计时中…”

- **级别 / 状态 / 来源**：低 / 已确认（代码路径；未驱动 Tk 界面） / 既有
- **位置**：`ui/product_app.py:245` 在开始扫描时设为“计时中…”，只有 `_publish`（`:477-489`）会复位；`ui/app.py:1917-1924` 处理 `scan_error` 时不触碰 `_scan_duration`。
- **触发条件**：任何 `scan_error`，包括 M-3。
- **影响**：界面状态失真，显示“扫描失败”的同时仍显示“计时中…”，直到下一次扫描。
- **建议**：在 `scan_error` 分支复位或写入失败耗时。
- **验证缺口**：未在 Tk 中实际观察。

### L-3 现有测试启动宿主机上的真实包管理器，并写入临时目录以外的位置

- **级别 / 状态 / 来源**：低 / 已确认（Linux） / 既有
- **位置**：
  - `tests/test_npm_rule_authority.py:10-17`：`USERPROFILE=C:\Users\person`、`NPM_CONFIG_CACHE=E:\npm-cache` 等。
  - `tests/test_pnpm_rule_authority.py:16-20`：`PNPM_HOME=D:\pnpm-home` 等。
- **证据**：
  - 用阻断并记录的 `Popen` 插件跑全量测试：18 个测试共尝试 178 次真实进程启动（`pnpm` 32 次，`go`、`dotnet`、`conda`、`uv`、`python3`、`pip`、`yarn`、`npm` 各 16 次，`python` 18 次），集中在各 `*_rule_authority`、`test_static_vendor_root_fallback`、`test_rules`、`test_codex_history`。
  - 正常运行后，pytest 的当前目录出现 `E:\npm-cache\_logs\*-debug-0.log`、`D:\pnpm-home`、`C:\Users\person\AppData\Local\Temp\node-compile-cache\…`。上轮在仓库根目录也出现过这些目录，已清理。
- **影响**：测试结果依赖宿主机装了哪些工具。在 Windows 开发机上，这些变量会让真实 npm/pnpm 写到真实的 `E:\`、`D:\` 或不存在的用户目录。
- **建议**：这些测试显式注入假的 `subprocess.run`，或在测试前后调用 `clear_process_cache()` 并清理这些环境变量。
- **验证缺口**：Windows CI 上真实写入了哪些位置未知。

## 4. 测试统计与失败分类

### 统计

| 运行 | 收集 | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- | --- |
| 工作树（Linux，临时目录作为 CWD） | 1113 | 854 | 231 | 28 |
| 基线 `git archive 2089fa6`（Linux） | 1105 | 846 | 231 | 28 |
| 真实 Windows CI run 39540236448（`f2dbfd3`，源码/测试与基线相同） | 1105 | 1105 | 0 | 0 |

- 工作树与基线的 231 个失败用例 ID 完全相同，报错消息逐条一致；上轮新增的 8 个用例在 Linux 全部通过。
- 28 个跳过全部是测试中写明的 Windows 专属 `skipif`：
  - `test_npm_mutation_scope.py` 18 个（“npm maintenance targets Windows paths”）；
  - `test_npm_scan_pruning.py` 6 个；
  - `test_pnpm_store_scan_pruning.py`、`test_docker_maintenance.py` 各 1 个；
  - `test_exact_cleanup_tree_pinning.py` 2 个（需要真实 Windows 共享语义和 junction）。
- `ruff check src scripts tests` 通过；`mypy --platform win32`（strict，346 个文件）通过（上轮末次运行；本次未改代码）。

### 231 个 Linux 失败的归因

方法：

1. 解析每个失败的 traceback（异常行、最深源码帧、测试行）。
2. 在临时进程中加载只用于诊断的 pytest 插件，只重跑这 231 个用例：
   - `posix_abs_diag`：让 `PureWindowsPath` 把 `/…` 当作绝对路径；
   - `posix_io_diag`：在前者基础上，把 `pathlib.Path` 收到的 `\tmp\…` 形式还原为 POSIX 路径。
3. 对诊断后仍失败的用例逐条读 traceback 与对应代码。

插件不进入仓库，也不代表修复方案。

| 机制 | 数量 | 证据 | 代表位置 |
| --- | --- | --- | --- |
| M1：生产代码用 `PureWindowsPath(值).is_absolute()` 校验环境变量与根，POSIX 的 `tmp_path`（`/tmp/…`）被判为非绝对，根被丢弃 | 70 | 加载 `posix_abs_diag` 后这 70 个全部通过 | `*_cleanup.py` 中 44 处 `is_absolute()`；NuGet/Go/Conda/uv/pip 维护模块的“不是已审计的 … 路径”拒绝 |
| M3-io：生产代码用 `Path(str(PureWindowsPath(...)))` 做目录枚举/读文件，POSIX 路径被渲染成 `\tmp\…` 的相对名，什么也找不到 | 97 | 加载 `posix_io_diag` 后又有 97 个通过；先前 70 个无一回退 | `jetbrains_cleanup.py:539`、`android_studio_cleanup.py:371`、`visual_studio_cleanup.py:136`、`android_avd_cleanup.py:464`、`firefox_cleanup.py:537`、`claude_plugin_cache_cleanup.py:427` |
| M3-str：同一渲染问题，但走 `os.path` 或字符串比较，`Path` 映射覆盖不到 | 11 | 断言文本为 `'\\tmp\\…'` 对 `'/tmp/…'`；`yarn_cleanup.py:427` 用 `os.path.isdir(str(PureWindowsPath))`；`scan-rules.json:379` 的模式使用 `\\` 分隔 | Android SDK 1、Claude native 1、Claude 插件缓存 1、Cursor updater 2、Trae 1、Windsurf 1、Yarn 4 |
| M2：测试直接用 Windows 盘符字面量，交给宿主机的 `pathlib`/`os.path` | 30 | 测试源码中的字面量 | `test_scan_and_triage.py:50` `Path(r"G:\work\cache").parents[1]` → `IndexError`（7）；`test_ai_review.py:93` `r"G:\scan\a.bin"` → “paths must be absolute”（6）；`test_windows_recycle_bin_maintenance.py` `C:\`（6）；`test_cleanup_execution.py` `G:\scan`（3）；`test_npm_cleanup.py:302`、`test_pnpm_cleanup.py`（2）；规则类测试 `C:\Users\person…`（5，`normalise_path` 用宿主 `os.path.normcase`，`_user_rules_impl.py:1175-1176`）；`test_drive_ordering.py`（1） |
| M4：Windows 专属 API 或 `os.name` 分支 | 23 | 代码中的分支或 API | 崩溃转储 8（`windows_crash_dump_maintenance.py:28`、`:92`、`:114` 的 `_WINDOWS` 守卫与 `winreg`）；任务管理器转储 5（Known Folder API，`_WINDOWS`）；Unreal 5（`unreal_maintenance.py:227`、`:253` 非 Windows 时找不带 `.exe` 的 `UnrealEditor-Cmd`，夹具是 `.exe`）；Docker buildx 3（`docker_buildx_maintenance.py:441-448` 非 Windows 只接受 `unix:///`，夹具是 `npipe://`）；`os.startfile` 1；`exact_cleanup` “requires Windows” 1 |
| 合计 | 231 | 没有未归因项 | |

结论与限制：

- 这些失败在 Linux 上都有可追溯的平台机制，与上轮改动无关。基线代码在真实 Windows CI 上 1105 个全部通过，这是第三方 CI 日志证据，不是本次实测。
- 这些机制意味着 Linux 环境**无法**为约 200 个 Windows 语义用例提供保护。其中包含与上轮改动直接相关的守卫用例（如 `tests/test_claude_cleanup.py:238`、各 `*_process_guard_*`）。工作树版本在 Windows 上的结果仍是缺口。
- 阻断真实进程启动时会额外失败 2 个（`test_codex_history.py`），说明它们依赖真实进程创建，见 L-3。

## 5. 观察（不计入缺陷）

- **线程安全**：扫描与删除互斥，`ui/app.py:999-1025` 以 `_busy` 统一禁用所有动作。扫描快照用 `ContextVar`，只在扫描线程内生效；`lru_cache` 线程安全；34 个 `*RootSet` 全部是 `frozen=True` 且无 list/dict/set 字段，快照共享不会被调用方篡改。
- **缓存生命周期**：进程存活与包管理器配置的 `lru_cache` 在扫描开始时从不失效（UI 中没有任何 `clear_process_cache` 调用）。一个 GUI 会话中的第二次扫描会沿用第一次的进程状态与配置。基线中每次删除都会顺带刷新，上轮后只有应用归属项会刷新，这使 M-1 的窗口跨越多次扫描。删除阶段对应用项的复核仍然是新鲜的。
- **扫描快照范围**：只覆盖产品入口 `product_app._scan_worker`；`ui/app.py`、`ui/modern_app.py` 的旧扫描线程未覆盖，`ui/launcher.py` 不使用它们，只影响性能。快照内的 `assess_application_whole_tree_policy` 与执行期的 `require_application_whole_tree_policy` 分别使用扫描期和新鲜发现，行为与设计一致。
- **系统 Installer 缓存**：
  - `%SystemRoot%\Installer` 不在扫描根下，且跳过名 `installer` 会剪掉它，新测试已固定；
  - VS Installer 包缓存为 KEEP，守卫返回 `False`，新测试已固定；注册表查询只经过快照化的构建函数；
  - 注册表结果不确定时 `visual_studio_installer_roots` 返回空，但该目录同样不会被扫描，且 `ProgramData` 在系统锚点拒绝名单中，不构成放行。
  - 副作用：跳过名 `installer` 会剪掉任何名为 installer 的用户目录（例如 `Downloads\installer\`），属既有设计。
- **路径别名（8.3 短名等）**：扫描、分类、目录登记都只做 `normcase`/`normpath` 字符串包含判断，没有长短名规范化。用符号链接模拟别名：
  - 临时目录文件只改变类别（`USER_TEMP` → `OTHER`），通道不变，均为 AI 复核，见 M-4；
  - Cursor 最新恢复安装包仍为 KEEP，因为 `_temp_roots` 同时使用 `%LOCALAPPDATA%\Temp`。
  - 因此不列为缺陷。短名 TEMP 是否导致同一目录被扫描两次，需要在 Windows 上验证。
- **删除原语**：`platform/windows/exact_cleanup.py`、`core/postscan_cleanup.py` 未被上轮改动触及。复核了严格后代检查、重叠候选拒绝、执行前身份与边界复核、`OPEN_REPARSE_POINT` 打开与最终路径校验，没有新发现。
- **性能**：上轮后剩余热点约一半在六个浏览器模块的 `PureWindowsPath.relative_to`。每个会话首次发现仍要启动约 11 个 CLI（Windows 上另有 PowerShell 探测），是固定开销。

## 6. 未验证范围

- 未在真实 Windows 上运行工作树。上轮新增的 8 个测试，以及受上轮改动影响、在 Linux 上因 M1/M3 无法运行的守卫用例（如 `tests/test_claude_cleanup.py:238`），只在 Linux 用复刻脚本验证过。
- H-1 的实际劫持、PyInstaller onefile 子进程的程序目录语义、`.cmd` 解析，均未实测。
- M-1 中由运行进程推导的归属（浏览器、编辑器 `--user-data-dir`）、M-2 中 Windows 额外的 PowerShell 探测，未实测。
- M-3 在 Windows 上 1970 年前、3000 年后时间戳的实际异常类型与出现频率，未实测。
- 用户报告的“删除普通安装包调用原应用卸载器”未复现；代码中没有直接调用卸载器的路径。
- 界面（Tk）没有被实际驱动，L-2 与界面相关结论来自代码阅读。
- 未在 NTFS/Windows 上测量扫描吞吐；性能数据来自 Linux 临时夹具。

## 7. 复现方式说明

复现脚本都在会话临时目录中运行，没有放进仓库。要点如下：

- 所有脚本把 `subprocess.Popen` 或 `subprocess.run` 替换为只记录 argv 并抛出 `FileNotFoundError` 的桩；M-1 的桩只对 `npm config get` 返回伪造的 `cache=…` 输出。
- 删除路径一律注入假的 purger，`read_file_metadata` 与 `is_local_fixed_path` 用测试同款替身，没有真实删除任何文件。
- 基线对比使用 `git archive 2089fa6` 导出的副本，并设置同样的 `PYTHONPATH`。
- 全量测试从临时目录作为 CWD 运行（`--rootdir`、`-c` 指向仓库），避免 L-3 中的测试在仓库根目录留下产物。
