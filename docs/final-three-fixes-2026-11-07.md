# 最终三项修复记录（2026-11-07）

本轮只处理授权的三项：

- ① 删除前的进程/服务归属复查；
- ② 未识别安装格式的 EXE 统一进入 AI/人工复核；
- ③ Windows 8.3 短路径与各种 UninstallString 写法的保守解析。

旧 `scan-rules.json` 的迁移和“不能删除”列表按要求不做。审计报告 [`cleanup-safety-audit-2026-11-07.md`](cleanup-safety-audit-2026-11-07.md) 正文未改动（SHA-256 与修复前一致），现有工作树保留。上轮记录 [`deletion-workflow-fixes-2026-11-07.md`](deletion-workflow-fixes-2026-11-07.md) §2 中被本轮改变的那条规则已加注。

| 项 | 内容 |
| --- | --- |
| 基线 commit | `2089fa6`；在前几轮未提交改动之上继续 |
| 提交状态 | 未 commit、未 push；全部改动留在工作树，等待核查 |
| 完成时间 | 2026-11-07T22:55Z；补修（§9）2026-11-08T00:25Z |
| 运行环境 | Linux 6.12，uv 托管 CPython 3.13，pytest 8.4.2，mypy 2.2.0（`--platform win32`），ruff 0.15.21；`winreg` 为临时目录中的桩模块。**本轮没有在 Windows 上运行任何东西，也没有构建 Windows EXE（见 §6）** |
| 测试数据 | 只用 `tmp_path` 临时夹具；没有删除真实用户文件，没有调用卸载器，没有清理系统缓存，测试不写真实用户路径、不启动包管理器或 PowerShell |

## 1. 摘要

| 项 | 修复后的行为 | 主要代码 | 回归测试 |
| --- | --- | --- | --- |
| ① 扫描后新进程/服务把文件当应用数据 | 每次清理开始时读取一次全部进程（一次查询）和已登记服务（注册表），拒绝删除被它们当作程序位置或数据位置的目标；列表完全读不到就不删除，命令行不可读的进程按目标判定（§9）；各应用自己的进程推导也改用这份按次刷新的列表 | `core/live_owners.py`（新增）；`platform/windows/processes.py`（新增）；`platform/windows/registry.py:132-208`；`core/postscan_cleanup.py:786`；`core/_application_cleanup_impl.py:163-195`；`core/application_cleanup.py:1065`；13 个应用模块 | `tests/test_deletion_workflow.py` 新增 5 项；`tests/test_command_paths.py` 中进程/服务/目录 5 项 |
| ② 非下载目录的未识别 EXE 被硬保护 | 任意扫描目录中没有安装包特征的 EXE 一律进入“需要判断”（AI/人工），批准前不删；确有已安装或系统用途的仍按明确规则保护 | `core/triage.py:961`、`:1002`；`core/installer_files.py:328` | `test_unrecognised_programs_anywhere_wait_for_a_decision_then_delete`、`test_programs_with_an_installed_or_system_purpose_stay_protected` |
| ③ 8.3 短路径与特殊 UninstallString | 引号、空格、`%变量%`、`\\?\`、8.3 短名、`,0` 图标序号、rundll32 `"dll",入口`、`cmd /c ""…" …"`、`_?=目录`、`/开关=值` 都能还原成同一个长路径；只解析，绝不执行 | `platform/windows/command_paths.py`（新增）；`core/installer_files.py:316-460` | `test_uninstall_entries_in_every_shape_protect_their_files_and_never_run`；`tests/test_command_paths.py` 中解析 19 项（含 1 项仅 Windows 运行） |

## 2. ① 删除前的进程/服务归属复查

### 原因

- 删除前的应用归属检查 `process_guard_refusal`（`core/application_cleanup.py`）只在目标**已经**被某个应用认领时才刷新该应用。扫描时无人认领的文件直接放行。
- 13 个应用（Chrome、Edge、Brave、Opera、Vivaldi、Firefox、VS Code、Trae、Windsurf、JetBrains、Android Studio、Android SDK、Gradle）从运行中进程的命令行推导数据目录，例如 `--user-data-dir`。这些结果用 `lru_cache` 一直保存自扫描时。
- 这 13 处查询失败时都返回空结果，等于静默当作“无归属”。
- 没有任何代码检查 Windows 服务。

所以，扫描后才启动、并把某个位置当作数据目录的程序或服务，删除前查不出来。

### 修复

- **一次列出全部进程。** `platform/windows/processes.py` 用一条 PowerShell CIM 查询取得所有进程的名称、程序路径和命令行，输出 UTF-8 JSON，中文路径不会变成乱码。查询失败、退出码非 0、输出不可读或为空都会抛出错误，不会返回空列表。
- **服务从注册表读取。** `registry.service_entries()` 读取 `HKLM\SYSTEM\CurrentControlSet\Services` 下每个服务的 `ImagePath`、`Environment` 和 `Parameters` 字符串值，覆盖 NSSM/WinSW 包装的服务。服务列表本身打不开时报错；单个无权打开的服务键跳过。
- **按次读取、按次刷新。** `run_cached`（`core/_application_cleanup_impl.py:163`）把这份状态保存在当前扫描或当前清理的快照里，每次清理开始时读取一次，同一次清理内所有文件共用。删除工作线程本来就在快照内运行（上轮改动）。
- **归属判定**（`live_owner_refusal`，`core/live_owners.py:84`）。目标落在以下位置之一就拒绝，原因写明是哪个程序或服务：
  - 正在运行的程序文件本身或它所在的文件夹；
  - 命令行中以文件夹形式出现的位置；
  - 命令行中直接点名的文件；
  - 服务程序的文件夹；
  - 服务环境变量和参数中出现的文件夹或文件。

  目录候选还要检查：里面有没有被使用的路径。用户主目录、下载、桌面、文档、AppData、Program Files、Temp、驱动器根目录这类宽泛位置，以及 Windows 文件夹，不会被当作某个程序的位置，因此资源管理器打开“下载”不会挡住整个下载文件夹。普通文件参数（例如 `/LOG=…\x.log`）只保护这个文件本身，不扩大到整个上级文件夹。
- **无法可靠核查时不当作无归属**（已按 §9 补修改为按目标判定）：
  - 进程列表或服务列表读取失败时，所有目标都拒绝删除，界面写明原因；
  - 某个进程的命令行被 Windows 隐藏时，原先的做法是所有目标都拒绝，并且仅凭 `System32` 中有同名文件就豁免。这两点都在 §9 中改正：只拒绝与该进程有关联证据的目标，以及经复核批准的不确定项；豁免只认可信的路径或会话证据。
- **各应用的推导改用同一份列表。** 13 个 `_running_*` 函数不再各自启动 PowerShell，也不再 `lru_cache`，改为从这份按次列表过滤，解析代码不变。效果有三点：
  - 清理时看到的是清理开始时的进程，而不是扫描时的进程；
  - 一次扫描或一次清理只查询一次进程，原来最多 13 次；
  - 查询失败时，删除前检查会拒绝，不再静默放行。
- **不逐文件重跑 CLI。** 应用归属检查对同一应用在一次清理中只刷新一次（`first_in_run`，`core/application_cleanup.py:1065`），不再每个文件刷新一次。包管理器 CLI 的结果仍按其配置文件签名复用（上轮 M-1 机制），配置变了才重新询问。环境变量和设置文件在每次清理开始时重新读取。
- **界面原因**（`ui/app.py:587-597`）：
  - “正在运行的程序 X 使用它所在的位置，已拒绝删除；关闭该程序或停止该服务后重试”；
  - “已登记的 Windows 服务 X 使用它所在的位置……”；
  - “无法确认正在运行的程序和服务是否在使用它（原因），已拒绝删除；以管理员身份运行 DevClean 或关闭相关程序后重试”。
- **测试隔离。** `tests/conftest.py` 默认“没有进程、没有服务”，避免 Windows CI 上的测试读取宿主机的真实进程和注册表；关心这些的测试自行设置。

## 3. ② 未识别安装格式的 EXE

`core/triage.py:961`：通过所有归属检查、又没有安装包特征的 `.exe`，不论位于哪个扫描文件夹，都进入 `AI_REVIEW`（“需要判断”），执行策略为“批准后删除”。

- 原因写明“没有发现安装包特征，也不属于已安装程序或系统：可能是便携程序、工具或构建产物”；下载文件夹中的会注明“下载的”。
- 分类：下载文件夹中的仍归“安装包/下载”；其他位置按路径推断，例如构建输出、其他。
- 批准前，核心层拒绝删除（上轮的审批闸门）。AI 导入或用户点“删除”后，按普通文件删除。

仍按明确规则保护的（`installer_ownership`，扫描时判断，删除前再判断一次）：

| 规则 | 原因示例 |
| --- | --- |
| 位于 Windows Installer 缓存或 Package Cache | “Windows Installer 缓存：已安装软件修复和卸载要用到其中的安装包，不能删除” |
| **新增**：位于 Windows 文件夹、Program Files、Program Files (x86)、`%LOCALAPPDATA%\Programs`、Microsoft Store 应用目录（`core/installer_files.py:328`）。这些目录默认不扫描，但用户可以把它们加为扫描路径 | “位于Program Files 程序安装目录：这里的程序由 Windows 或安装程序管理，删除会损坏它们” |
| Windows“已安装的程序”列表登记的位置（③ 的解析） | “已安装程序「名称」的文件……” |
| 卸载程序文件名，或同目录有卸载数据 | “已安装程序的卸载程序或安装文件夹……” |
| 正在运行的 DevClean 自身 | “正在运行的 DevClean 程序本身” |

已知清理根目录和临时目录中的 EXE 仍按各自的目录策略处理；应用规则、已安装扩展规则先于本分支生效。DLL、驱动、虚拟磁盘等仍按程序文件保护（本项只针对 EXE）。

## 4. ③ 8.3 短路径与 UninstallString 的保守解析

新增 `platform/windows/command_paths.py`，供已安装程序归属和 ① 的进程/服务归属共用：

| 写法 | 处理 |
| --- | --- |
| `"C:\Program Files\Foo App\uninst.exe" /S` | 取引号中的程序 |
| `C:\Program Files\Bar Tool\uninstall.exe /S`（无引号含空格） | 像 Windows 一样逐段加长，取第一个以 `.exe/.com/.bat/.cmd` 结尾或实际存在的前缀 |
| `%ProgramFiles%\Baz\uninstall.exe`（REG_SZ 中的变量） | 不区分大小写展开；未知变量保留原样 |
| `rundll32.exe "C:\…\qux.dll",Uninstall`、`"…\app.exe",0`、`…\app.exe,-101` | 去掉入口名或图标序号；rundll32 本身属于 Windows，DLL 才是程序位置 |
| `\\?\C:\…`、`\\?\UNC\server\share\…` | 去掉扩展长度前缀 |
| `cmd.exe /c ""C:\Program Files\Foo\uninstall.bat" /S"` | 先去掉外层多包的一对引号 |
| `"…\Un_A.exe" _?=C:\Apps\NsisDir`、`/D=…`、`/LOG:…`、`-Dkey=…`、`KEY=VALUE` | 取等号或冒号后的值 |
| `C:\PROGRA~1\FOOAPP~1\UNINST~1.EXE` | 8.3 短名经 `GetLongPathNameW` 展开（仅 Windows；完整路径不存在时展开最长的已存在上级）；扫描路径、环境变量（例如短形式的 `%TEMP%`）和注册表值使用同一套规范化 |
| `MsiExec.exe /X{GUID}`、`/S`、`/quiet` | 不是位置，跳过 |

归属规则：

- 命令中第一个可用的文件是程序，程序所在文件夹归该程序；
- 以文件夹形式出现的参数归该程序；
- 其他文件参数只保护文件本身；
- Windows 文件夹和宽泛位置（例如把 `InstallLocation` 写成“下载”文件夹）不认领。

**绝不执行：** 解析只做文本处理和文件存在性检查。端到端测试放了一个运行后会写标记文件的“卸载程序”，并记录所有启动进程的尝试：扫描、复核、删除全程都没有启动它，标记文件也不存在。

## 5. 测试与检查

新增 32 项测试，其中 1 项只在 Windows 上运行：

- `tests/test_deletion_workflow.py` 新增 8 项。都走真实链路：真实扫描 worker → 真实分区 → 真实删除 worker（候选、复查、日志）→ 真实精确删除（Linux 分支），只替换进程、服务和已安装程序列表。覆盖：
  - 扫描后启动的程序使用文件夹 → 拒绝；程序退出后的下一次清理 → 删除；
  - 扫描后登记的服务以环境变量指向数据文件夹 → 拒绝；
  - 程序从该文件夹运行 → 拒绝；
  - 进程列表读取失败、服务列表无权读取 → 全部拒绝并写明原因；命令行被隐藏的进程按目标判定（§9）；
  - 每次清理只读一次进程列表，不启动任何程序；
  - 各应用的进程推导在每次运行中共用一份新列表；
  - 任意目录中的未识别 EXE：批准前拒绝，AI 批准和用户批准后删除；
  - Windows、Program Files、`LocalAppData\Programs`、已安装程序、卸载程序中的 EXE 带具体原因保护；
  - 9 种 Uninstall 写法（含 8.3 短名、`%变量%`、rundll32、`_?=`、只登记到“下载”文件夹的宽泛位置）都正确认领，而且从不执行。
- `tests/test_command_paths.py`（新增，24 项）：
  - 15 种 Windows 命令行写法，在 Linux 上用 `ntpath` 按 Windows 规则检查；
  - 8.3 短名；
  - 程序与日志参数的区别；
  - 真实 8.3 展开（仅 Windows，Linux 上跳过）；
  - 解析不启动程序；
  - 进程 JSON 解析与失败；
  - 目录候选；
  - 宽泛位置不认领；
  - 用模拟注册表读取服务的 `ImagePath`、多字符串环境变量、`Parameters` 与 `REG_EXPAND_SZ`。

变异核对：在临时副本中逐一拆掉以下 12 处，每一处都至少让一个测试失败：

- ①：删除前检查、读不全时拒绝、隐藏命令行检查、按次刷新、应用推导不再缓存；
- ②：非下载 EXE 进入复核、程序安装目录保护；
- ③：8.3 展开、变量展开、无引号空格、文件参数不扩大到文件夹、嵌套引号。

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（Linux，`winreg` 桩，临时 CWD） | 231 failed / 920 passed / 29 skipped。231 个失败的测试 ID **和失败信息**与基线逐条一致（无新增、无变化），都是既有的 Windows 路径/API 语义失败；通过数比上轮多 31，跳过数多 1（仅 Windows 的 8.3 测试） |
| `ruff check src scripts tests`（CI 同款） | 通过 |
| `mypy --platform win32 src tests scripts` | 通过（357 个文件） |
| 扫描性能与一致性（3 202 个文件的合成目录） | 4.7–4.8 秒，与上轮相同；1 340 项分类与上轮逐行一致 |
| 删除前检查开销（300 个模拟进程） | 每次清理建立一次索引约 76 ms，之后每个文件约 60 µs |
| 测试后临时 CWD 与仓库根 | 本轮无残留（仓库根已有的 `.pytest_cache/` 创建于 13:22Z，早于本轮，且已被 Git 忽略，未动） |
| 审计报告 SHA-256 | 与修复前一致 |

## 6. Windows EXE 构建与实测：阻塞

- **构建脚本：** `scripts/build_windows_exe.ps1` 是 PowerShell 脚本。它在 Windows 上执行 `uv sync`，从源码构造一次 Tk 应用做冒烟，再用 PyInstaller 生成带 `--uac-admin` 的 `DevClean.exe`，产物在 `artifacts/windows-exe/dist/`。CI 在 GitHub 的 `windows-latest` 上运行它。
- **本机环境：** Linux（`uname`：x86_64 GNU/Linux），没有 `powershell.exe`、`pwsh`、`cmd.exe`、Wine，没有 VirtualBox/libvirt/QEMU/Vagrant/Docker，`~/.ssh/config` 中没有配置任何主机，`/mnt` 与 `/media` 为空。**本机没有可用的 Windows 构建环境。**
- **结论：** 按要求，没有在 Linux 上用 PyInstaller 冒充，没有改后缀，没有使用用户的 Windows 电脑，没有远端构建，也没有上传源码。本轮源码的 Windows EXE 未构建，也未实测；仓库中的 `release/DevClean.exe` 是旧版本，不含本轮改动。**需要主助手协调一台获准使用的 Windows 环境**，在那里运行 `./scripts/build_windows_exe.ps1 -Python 3.13`，然后实测下文第 7 节中依赖 Windows 的部分。

## 7. 未验证范围与已知限制

1. **依赖 Windows 的部分只有单元测试，没有实机验证。**
   - PowerShell CIM 查询的真实输出与编码；
   - 以管理员身份和非管理员身份运行时，被隐藏命令行的进程有多少；
   - `GetLongPathNameW` 的真实展开（测试只能在 Windows 上运行）；
   - 真实注册表的服务枚举。

   非管理员运行时，会话 1 中由系统运行的 `csrss.exe`、`winlogon.exe`、`dwm.exe` 等，其程序路径通常也读不到。按 §9，它们不能仅凭名字豁免，于是经复核批准的不确定项会被拒绝，并提示以管理员身份运行；规则证明的垃圾不受影响。构建脚本给 EXE 加了 `--uac-admin`，正常以管理员身份运行时，这些进程的路径应该可见并按 Windows 文件夹豁免，但没有实测。
2. **偏向多保护。**
   - 程序运行期间，它所在的文件夹和命令行中点名的文件夹都不会被删除。例如 IDE 打开某个项目时，该项目中的文件被拒绝删除，关闭后可以重试。
   - 已登记但未运行的服务，其配置的位置同样受保护。
   - Windows 不提供其他进程的当前工作目录，这部分无法检查。
3. **进程状态是清理开始时的快照。** 清理过程中（通常几秒）新启动的程序要到下一次清理才会被看到。文件正被打开时，Windows 自身的共享限制仍会让删除失败，界面显示“被程序占用”。
4. **包管理器的位置不在每次清理时重新询问。** 只有其配置文件变化时才重新询问（上轮机制），以避免逐文件或逐次运行整套 CLI。不通过配置文件改变的位置，现在由本轮的进程/服务复查兜底，前提是对应程序正在运行或已登记为服务。
5. **按要求未做：** 旧 `scan-rules.json` 的迁移（已有用户仍跳过名为 `Installer` 的文件夹），以及界面中的“不能删除”列表。
6. **本轮不涉及：** 下载文件夹被重定向到扫描范围之外时，需要用户自行加入扫描路径；这一点与之前相同。

## 8. 改动文件（本轮）

- 新增：
  - `src/devclean/core/live_owners.py`；
  - `src/devclean/platform/windows/processes.py`；
  - `src/devclean/platform/windows/command_paths.py`；
  - `tests/test_command_paths.py`；
  - 本文档。
- 修改：
  - `src/devclean/core/_application_cleanup_impl.py`：`run_cached`、`forget_run_cached`、`first_in_run`；
  - `src/devclean/core/application_cleanup.py`：每次清理每个应用只刷新一次；
  - `src/devclean/core/postscan_cleanup.py`：删除前进程/服务复查；
  - `src/devclean/core/installer_files.py`：共用路径解析，新增程序安装目录规则，导出宽泛/系统文件夹；
  - `src/devclean/core/triage.py`：未识别 EXE 进入复核，程序安装目录保护；
  - `src/devclean/platform/windows/registry.py`：`service_entries`；
  - `src/devclean/ui/app.py`：新的拒绝原因；
  - 13 个应用模块 `{android_sdk,android_studio,brave,chrome,edge,firefox,gradle,jetbrains,opera,trae,vivaldi,vscode,windsurf}_cleanup.py`：进程推导改用共享列表；
  - `README.md`；
  - `docs/deletion-workflow-fixes-2026-11-07.md`：§2 加注；
  - 测试：`tests/conftest.py`、`tests/test_deletion_workflow.py`。

## 9. 补修：命令行不可读的进程按目标判定（2026-11-08）

### 复核结论

- 原 `core/live_owners.py:121-132`：任何一个未豁免进程的命令行读不到，就把整份状态标成“读不全”，每个目标都被拒绝。一个与目标毫无关系的提权程序，就能挡住下载文件夹里的旧安装包。
- 原 `:153-159`：程序路径缺失时，只要 `System32`、`Windows` 或 `SysWOW64` 里有同名文件就豁免。用户会话里任何名为 `svchost.exe` 的第三方程序都能因此逃过检查。

### 修复

清理候选新增 `rule_proven` 字段（`core/postscan_cleanup.py:114`）：扫描规则证明可删的项为真，经 AI 或用户批准的不确定项为假。它和其他字段一起计入防篡改签名，事后改不了。删除前检查改为逐个目标判定（`core/live_owners.py:156`）：

| 情况 | 结果 | 界面原因 |
| --- | --- | --- |
| 进程列表或服务列表**完全读不到** | 所有目标拒绝（规则证明的垃圾也拒绝，因为没有任何证据可用） | “无法确认正在运行的程序和服务是否在使用它（原因），已拒绝删除……” |
| 可见的进程或服务使用该位置 | 拒绝 | 同前：“正在运行的程序 X 使用它所在的位置……” |
| 命令行不可读、且与目标**有关联证据**的进程 | 拒绝，不论证据等级 | “无法读取正在运行的程序 X 的启动参数，而〈关联〉，可能正被它使用，已拒绝删除；关闭该程序或以管理员身份运行 DevClean 后重试” |
| 命令行不可读、与目标**无关联**的进程，目标是**规则证明的垃圾** | 照常删除 | — |
| 同上，目标是**经复核批准的不确定项** | 拒绝 | “无法读取正在运行的程序 X、Y 的启动参数；这一项是经 AI 或你批准的不确定项，不能确认它没有被这些程序使用，已拒绝删除……” |

“关联证据”（`_relation`，`core/live_owners.py:279`；`_programs_inside`，`:308`）只使用能取得的信息：

- 进程的程序路径可见：程序文件本身及其程序文件夹（宽泛位置和 Windows 文件夹除外）；要删的目录里含有该程序。
- 程序路径不可见：
  - 目标文件与进程同名，可能就是正在运行的程序本身；
  - 目标的上级文件夹（直到宽泛位置为止）里有同名程序；
  - 要删的目录内部任意深度有同名程序。每次清理每个目录只查一次。目录超过 10 万项或有子文件夹读不到时，按“无法确认”给出具体原因拒绝。
- 路径中含有进程的独特名称（至少 4 个字符；`python`、`node`、`setup`、`svchost` 等通用名称不算），例如隐藏的 `ollama.exe` 对应 `Ollama-cache`。
- `msiexec.exe`（Windows Installer）在运行时，`.msi` 和 `.msp` 安装包视为相关。

“豁免”只认可信证据（`_vouched_for`，`:230`）：

- 程序路径可见，并且位于 Windows 文件夹、不是脚本或命令宿主（`cmd`、PowerShell、`wscript`/`cscript`、`mshta`、`rundll32`、`regsvr32`、`msiexec`、`wsl`/`bash`）；
- 或者程序路径就是某个已登记服务的程序（服务配置已从注册表读入并检查）；
- 或者进程在会话 0（只有 Windows 和服务能在那里启动程序），且名字是已登记服务的程序名或 Windows 程序名；
- 内核伪进程（`System` 等）也要求会话 0。

用户会话里、或会话未知的同名进程，一律不豁免。会话号来自同一次 CIM 查询新增的 `SessionId` 字段，没有增加查询次数。

### 测试与检查

新增或改写的回归测试，都只用临时夹具：

- `test_unreadable_process_lists_refuse_every_target_with_the_reason`：进程查询失败、服务列表无权读取时，规则证明的安装包也被拒绝，原因明确，不启动任何程序。
- `test_unrelated_hidden_program_does_not_block_junk_but_guards_its_own_places`（真实扫描→删除链路）：
  - 三个命令行不可读的提权程序在运行时，与它们无关的旧安装包照常删除；
  - 被拒绝的有：程序文件夹里的、上级文件夹有同名便携程序的、路径含 `Ollama` 的，以及经用户批准的不确定 EXE，各自带具体原因；
  - 程序关闭后的下一次清理，剩余项全部删除。
- `test_a_name_alone_never_vouches_for_a_hidden_process`：
  - 不能豁免：会话 1 或会话未知、无路径的 `svchost.exe`；路径在 `System32` 的 `cmd.exe`；会话 0 的 `cmd.exe`；
  - 可以豁免：会话 0 的 `System`、会话 0 的服务程序 `svchost.exe`、路径在 Windows 文件夹的 `lsass.exe` 和 `dwm.exe`。
- `test_hidden_programs_guard_related_directories_and_installer_packages`：
  - 目录里藏有同名程序，或含有隐藏程序的可见程序文件 → 拒绝；
  - 隐藏的 `msiexec` 运行时，`.msi` 被拒绝；
  - 无关的规则证明目录或文件 → 放行；无关的复核项 → 拒绝。
- `test_directory_that_cannot_be_fully_searched_is_refused_with_the_reason`：目录过大或子文件夹读不到时明确拒绝。
- `test_process_session_is_read_when_windows_gives_it`：会话号的解析。
- `test_a_reviewed_item_cannot_be_relabelled_as_rule_proven`：把复核项改标为“规则证明”会被防篡改签名拒绝。

变异核对：逐一拆掉以下 8 处，每一处都至少让一个测试失败：

- 隐藏进程重新全局阻挡；
- 仅凭名字豁免；
- 宿主程序被豁免；
- 去掉关联判定；
- 放行无关的复核项；
- 不查目录内部；
- 完全读不到时放行垃圾；
- `rule_proven` 不进签名。

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（Linux，`winreg` 桩，临时 CWD） | 231 failed / 926 passed / 29 skipped。231 个失败的测试 ID **和失败信息**与基线逐条一致，都是既有的 Windows 路径/API 语义失败 |
| `ruff check src scripts tests` | 通过 |
| `mypy --platform win32 src tests scripts` | 通过（357 个文件） |
| 删除前检查开销（300 个可见进程 + 20 个不可读进程，1 000 个目标） | 每次清理第一次约 73 ms，之后每个目标约 0.6 ms |

### 仍然存在的限制

- 命令行不可读的程序如果把数据放在既不在其程序文件夹、也不带其名称的位置，而该位置里的文件又是**规则证明的垃圾**，本检查不会拦截。这是“不让无关不可读进程挡住普通垃圾”的代价；文件正被打开时，Windows 自身的占用限制仍会让删除失败。经复核批准的不确定项不受这个限制，会被拒绝。
- 会话 0 中与 Windows 程序或服务程序同名的进程会被豁免。能在会话 0 启动程序的已是管理员或服务权限。
- 以上与 Windows 相关的判定（`SessionId`、各类进程的路径和命令行可见性）只有 Linux 上的单元测试，没有在 Windows 上验证。按安排，Windows 构建与实测由主助手通过 Windows Actions 进行。
- 旧配置迁移和“不能删除”列表按要求未做。
