# DevClean 工作树清理安全审计提示词

用途：对 DevClean 当前工作树（含未提交改动）做一次只读的清理安全审计。可直接作为
提示词交给审计者（人或 AI），每次审计复用同一套规程，结果另存为带日期的报告。

## 1. 输入与记录

审计开始前记录，并写入报告头部：

- 基线 commit：`git rev-parse HEAD` 及其标题。
- 审计 diff 范围：`git diff <基线>`（已跟踪改动）加 `git status --short` 中的未跟踪文件；
  明确写出“包含 / 不包含未提交改动”。
- 审计时间：UTC 与本地时间各一次。
- 运行环境：操作系统、Python 版本、是否在真实 Windows 上运行。

## 2. 硬性约束

1. 只审计、只产出文档。不修改业务代码，不提交，不推送，不 force-push。
2. 保留工作树：禁止 `git stash`、`git checkout -- <path>`、`git reset`、`git clean`。
   需要对比基线时，用 `git archive <基线> | tar -x -C <临时目录>` 导出副本。
   审计前可把 `git diff` 和未跟踪文件备份到临时目录并记录校验和。
3. 不真实删除用户文件，不调用真实卸载器，不执行厂商清理命令
   （`pip cache purge`、`npm cache clean`、`go clean`、`dotnet nuget locals --clear` 等）。
4. 必要的测试或复现只用临时夹具。注入假的 purger/recycler，并把 `subprocess.Popen`
   替换为“只记录 argv、抛出 FileNotFoundError”的桩。pytest 在临时目录作为 CWD 运行，
   避免测试在仓库根目录留下产物。
5. 不得虚构 Windows 实测。非 Windows 环境得到的结论必须标明“Linux 代码/桩验证”，
   涉及真实 Windows 语义的结论标为“待验证”，除非引用了可核查的官方文档或 CI 结果。
6. 不得未经核查就把失败测试称为“预期”或“Windows 路径问题”，必须按实际失败证据分类。
7. 不凑数量：没有证据的猜测不写成发现；可写入“观察”或“未验证范围”。

## 3. 架构地图（审计时按层定位）

| 层 | 关键文件 | 审计要点 |
| --- | --- | --- |
| 扫描 | `src/devclean/scanner/filesystem.py`、`tree_summary.py` | 只读遍历；重解析点/云占位符作为边界不下钻；跳过名/跳过路径 |
| 分类 | `core/triage.py`、`core/review_routing.py`、`core/cleanup_catalog.py`、`core/user_rules.py` | 通道（确定/用户/AI/仅报告）、`execution_policy`、硬保护标签 |
| 应用规则 | `core/application_cleanup.py`（门面）、`core/_application_cleanup_impl.py`、各 `core/*_cleanup.py` | TOOL/USER/KEEP 归属、根发现、进程守卫、缓存 |
| 删除执行 | `core/postscan_cleanup.py`、`core/cleanup_journal.py`、`core/whole_tree_policy.py` | 封印候选、执行前复核、日志状态机 |
| 平台 | `platform/windows/exact_cleanup.py`、`filesystem.py`、`volumes.py`、`subprocess_policy.py` | 句柄绑定删除、边界、重解析点、子进程策略 |
| 厂商动作 | `core/vendor_cleanup_actions.py`、`core/*_maintenance.py` | 只通过封印候选调用官方命令 |
| 界面 | `ui/product_vendor_app.py`、`ui/product_app.py`、`ui/modern_app.py`、`ui/app.py` | 线程、事件、取消、结果展示 |

产品入口：`ui/launcher.py` → `product_vendor_app.ProductDevCleanWindow`。

## 4. 审计维度

每一维都要先读代码，再找能证伪的最小复现。

1. **正确性**：改动是否改变分类结果、规则优先级（KEEP 优先）、候选资格；用同一夹具
   对比基线与工作树的分类输出。
2. **删除范围与不可逆误删**：候选只能来自完成的扫描；文件/整目录资格在执行前复核；
   永久删除不可恢复，任何放宽都按高风险处理。
3. **目录穿越**：严格后代检查、`..`、大小写、`\\?\` 前缀、8.3 短名、驱动器相对路径、UNC。
4. **符号链接/重解析点/硬链接/云占位符**：扫描不下钻；删除以 `OPEN_REPARSE_POINT`
   打开并核对最终路径与边界。
5. **四类对象的区分**：
   - 普通下载安装包（Downloads 中的 `.exe/.msi/.msix/.zip` 等）；
   - 包管理器缓存（pip/uv/pnpm/npm/conda/NuGet/Go 等，只能走厂商命令）；
   - 系统 Windows Installer 缓存（`%SystemRoot%\Installer`）与 Visual Studio Installer
     包缓存（默认 `%ProgramData%\Microsoft\VisualStudio\Packages`，可被 `CachePath` 策略重定向）；
   - 已安装应用（Program Files、`%LOCALAPPDATA%\Programs`、程序本体后缀）。
   删除普通文件绝不能触发卸载、修复或厂商命令；系统 Installer 缓存默认不可删。
6. **进程探测副作用**：列出扫描期与删除期会启动的所有进程（厂商 CLI、PowerShell、
   `tasklist`、被探测的应用自身）；核对按裸名启动时的 Windows 搜索顺序、
   CWD/程序目录劫持风险、超时、失败时是否“按运行中”处理（fail closed）。
7. **扫描性能**：用临时夹具测吞吐（files/s）与 cProfile 热点；确认没有逐文件的
   注册表/目录枚举/配置读取/进程启动。
8. **缓存失效**：`lru_cache` 与扫描快照的生命周期；扫描之间、扫描与删除之间是否会
   使用过期的归属或进程状态；是否存在“缓存命中即放行”的 fail-open。
9. **线程安全**：扫描线程、删除线程、UI 线程是否可能并发；ContextVar、`lru_cache`、
   共享 session 的读写。
10. **真实 Windows 语义**：大小写折叠、短名/长名、重定向文件夹、共享冲突、
    `FILE_DISPOSITION` 语义、回收站行为。只能在 Linux 推断的写成“待验证”。
11. **异常与取消恢复**：扫描/删除线程中未捕获的异常是否会让界面永久卡在忙碌状态；
    取消后的部分结果是否被当作完整结果；日志状态能否重试。
12. **界面结果真实性**：显示的“可清理/已释放”字节、项数与真实执行结果是否一致；
    厂商命令的结果是否被如实报告。

## 5. 证据标准与状态

- **已确认**：给出文件与行号，以及可运行的最小复现、测试、日志或确定的代码路径推理，
  且不依赖未验证的外部条件。
- **待验证**：依赖真实 Windows、特定安装状态或外部工具行为，写明还缺什么证据。
- 每条发现都写：严重级别、状态、文件与行号、触发条件、证据/最小复现、影响、建议、验证缺口。

## 6. 严重级别

| 级别 | 含义 |
| --- | --- |
| 严重 | 默认配置下可直接导致不可逆误删、删除系统/已安装应用数据或执行任意代码 |
| 高 | 特定但现实的条件下可导致上述后果，或安全边界被绕过 |
| 中 | 安全保证被削弱（fail-open 窗口、过期状态），或明显的功能/性能缺陷 |
| 低 | 局部功能问题、误导性文本、测试缺口，无直接数据风险 |
| 信息 | 观察与改进建议，不计入缺陷 |

## 7. 测试失败分类方法

1. 运行全量测试并保存完整输出（含异常类型与消息）。
2. 按异常类型与首个失败断言聚类；每类抽查至少一个具体失败的 traceback。
3. 每类注明：数量、代表用例、实际证据（报错原文）、根因判断、
   是否可能掩盖真实缺陷、在 Windows 上是否需要复核。
4. 与基线（同一环境下 `git archive` 导出的副本）对比：区分新增失败、修复的失败与原有失败。

## 8. 报告存放与格式

- 位置：`docs/`，文件名 `<主题>-audit-YYYY-MM-DD.md`；同名已存在时追加 `-2`、`-3`，不覆盖旧报告。
- 结构：头部记录（第 1 节）→ 结论摘要 → 发现列表（按严重级别排序）→ 测试统计与失败分类
  → 观察 → 未验证范围 → 复现脚本说明。
- 结束时报告：报告路径、提示词路径、各级别数量（区分已确认/待验证）、测试统计、未验证范围。
- 审计结束后不要开始修复。
