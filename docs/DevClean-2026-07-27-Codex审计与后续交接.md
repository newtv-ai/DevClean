# DevClean Codex 审计与后续交接

日期：2026-07-27  
适用产物：DevClean 1.0.0 / Windows 11 x64

## 1. 本轮结论

本轮按“磁盘清理工具的实际使用流程”重新审计，并修复了确认存在的功能问题。最终 Windows 单文件 EXE 已重新构建并替换到：

`G:\clean\release\DevClean.exe`

- 文件大小：12,754,298 字节（约 12.16 MiB）
- SHA-256：`c6b21f2bc61ccd9ef2c94f834c417b3a53f7d8d96481bc08a411ef23ebc5212c`
- 用户是否需要安装 Python：不需要
- 构建方式：PyInstaller `--onefile --windowed --uac-admin`
- 发布载荷：一个 EXE 加四份运行时许可证

Git 仓库已经直接恢复在 `G:\clean`，不再有两个同名目录套壳。当前分支为 `main`，远端为 `https://github.com/newtv-ai/DevClean.git`。产品实现提交为 `97b67a7487f393a360b9d46cb9ead4a52b69779e`（`feat: complete configurable cleanup workflow and Windows release`）；Claude 复审后的旧架构清理提交为 `b5855786d5961c2210f6495281fcb35aa4551fc0`（`refactor: remove legacy pipeline and streamline EXE build`）。作者和提交者均为 `newtv-ai <267045021+newtv-ai@users.noreply.github.com>`。当前本地提交尚未 push；不得未经用户明确要求上传到 GitHub。

## 2. 本轮确认并修复的问题

### 2.1 构建成功后仍被发布白名单判失败

原因：旧构建脚本运行成品 `--uac-admin` EXE 做 `--ui-smoke`，会在 `dist` 旁生成 `DevClean-data`。本轮清除 Python wheel 安装后，PyInstaller 的旧 `--collect-data devclean` 又只留下三份 JSON 数据而漏掉 `devclean.config` 包；`importlib.resources` 无法导入资源包，于是成品启动时报 `ModuleNotFoundError`，异常窗口等待关闭，看起来像构建卡住。

修复：GUI 构造冒烟在打包前使用同一 Python 3.13 环境运行真实 Tk 入口；PyInstaller 显式加入 `devclean.config` 隐藏导入和三份 JSON。构建后递归检查成品归档，资源包模块或任何一份规则缺失都会直接失败。本轮还实际启动最终 `release\DevClean.exe --ui-smoke`，退出码为 0，且没有残留 DevClean 进程。

为什么这样改：只确认 JSON 文件名存在并不等于 `importlib.resources` 能使用；资源包代码和资源数据必须同时验证。用户收到的发布包仍只有 EXE 和许可证，三份可编辑规则仍由用户首次正常启动时在 EXE 旁生成。

### 2.2 “整个目录”可以彻底删除，却不能进回收站

原因：左栏可以出现整个缓存目录，但回收站实现只按普通文件打开和核对对象；目录缺少目录句柄语义，操作会被拒绝。事后核对也使用了文件比较函数，可能把仍存在的目录错误理解为路径被替换。

修复：

- 普通文件继续使用文件身份核对。
- 整个目录改用目录句柄、目录身份和最终路径核对。
- Shell 回收后，文件和目录分别使用对应的事后状态判断。
- 永久删除逻辑和用户点击“彻底删除”的语义不变。

为什么这样改：界面既然把“整个目录”放在左栏，两个清理按钮就都必须对它生效，不能出现功能前后矛盾。

### 2.3 AI 回答 UNSURE 后，用户没有最后决定入口

原因：原界面只会自动处理 `KEEP` 和 `RECOMMEND_RECYCLE`。`UNSURE` 留在右栏，但用户不能把它最终判为可删或保留，不符合既定流程。

修复：右栏新增“我来决定…”：

1. 只有导入结果中明确回答 `UNSURE` 的行可以使用。
2. 用户先在右栏选中一项或多项。
3. 弹窗逐项展示路径和 AI 原因。
4. 选择“是”会写入 `USER_DECISION` DELETE 精确路径规则，移到左栏并默认勾选。
5. 选择“否”会写入 `USER_DECISION` KEEP 精确路径规则并隐藏。
6. 选择“取消”不做任何修改。
7. 最终用户决定只改变分类，不会自行触发删除；可删项仍需用户点击左侧清理按钮。

为什么这样改：流程必须是“程序确定 → AI 解释并判断 → AI 仍不确定才交给用户”，而不是让用户一开始就猜，也不能让 AI 的建议自动执行。

### 2.4 DevClean 可能扫描到自己的数据目录

原因：EXE 若放在 Downloads 等会被扫描的位置，旁边的 `DevClean-data` 没有进入默认剪枝。执行层会保护自身状态，但扫描层仍可能显示自己的临时写入文件，造成“看起来可选、执行时拒绝”的矛盾，并可能浪费 AI 判断次数。

修复：`scan-rules.json.skip_directory_groups.devclean_state` 保留可编辑的 `DevClean-data` 名称剪枝；扫描启动时还会把本次运行实际使用的 `data_dir()` 作为精确排除路径。因此 EXE 旁的 `DevClean-data` 和源码运行使用的 `%LOCALAPPDATA%\DevClean` 都不会进入扫描，同时不会因为一个宽泛的 `DevClean` 名称规则而跳过用户其他同名目录。

为什么这样改：自身状态不应成为垃圾候选；同时继续遵守“所有扫描分类配置统一由三份 JSON 管理”。

### 2.5 残留代码、类型和注释不一致

修复内容：

- 删除整目录旧中转流程残留的未使用辅助函数和导出。
- 修正 SQLite 日志中关于当前回收站状态的过期注释。
- 修正 AI 会话路径计数的类型收窄。
- 修正规则编辑器父窗口类型。
- 整理导入和公开符号顺序。
- 同步更新使用说明和架构文档。

这些修改不改变用户产品决策，只清理已经确认的死代码、错误说明和静态检查问题。

### 2.6 Claude 复审确认的旧架构残留

Claude 复审确认，本地仓库仍同时保留 Reclaimer 时代的 pytest、三版本 Python
矩阵、wheel/SBOM 发布链、broker gate、证据存储、旧 CLI 和不可达的隔离恢复
状态机。这些不是 DevClean GUI 的备用实现，GUI 入口没有运行时调用关系。

本轮按用户明确决定直接删除，不保留“以后可能会用”的并行架构：

- 删除全部旧测试、测试 transcripts 和 pytest/coverage 配置及依赖。
- CI 收敛为单个 Windows / Python 3.13 作业，只做静态检查并构建单文件 EXE。
- 删除 wheel/SBOM、JSON Schema、gate evidence、broker 与旧发布校验脚本。
- 删除旧 CLI、adapters、evidence、state/reporting/doctor/duplicates 等不可达模块。
- 删除旧 quarantine/restore/replay 状态、旧日志迁移和自动永久清理分支。
- 删除构建目录中残留的 Reclaimer wheel、coverage 文件和空目录。
- 新增与当前产品一致的公开仓库 `README.md`，清理 issue/PR 模板中的失效链接和 pytest 要求。

当前删除日志只有 `RECYCLE` 和 `PERMANENT` 两种用户真实选择；永久删除仍由用户
点击“彻底删除”触发，行为没有被收窄。

## 3. 当前实际产品流程

1. 用户勾选固定盘符并开始扫描。
2. 程序按 `scan-rules.json` 决定扫描位置、已知清理根和剪枝目录。
3. 程序确定可删的文件或目录进入左栏并默认勾选。
4. KEEP 项隐藏，不进入左栏，也不再询问 AI。
5. 程序不确定的普通文件进入右栏。
6. 用户导出 JSON 给 Codex、Claude 或其他模型。
7. 同一进程导入使用完整严格校验；重启后的备用导入保留允许部分答案的既定行为。
8. `RECOMMEND_RECYCLE` 写入 DELETE 规则并进入左栏；`KEEP` 写入 KEEP 规则并隐藏；`UNSURE` 留在右栏。
9. AI-UNSURE 项由用户通过“我来决定…”最终记为可删或保留。
10. 只有用户点击左栏“清理（进回收站）”或“彻底删除”才会执行文件系统变更。
11. 单项失败会记录并跳过，继续处理下一项；失败项仍留在界面，可再次点击重试。

## 4. 三份配置的当前状态

唯一配置文件：

- `src\devclean\config\scan-rules.json`
- `src\devclean\config\delete-rules.json`
- `src\devclean\config\keep-rules.json`

审计解析结果：

- 已知清理根记录：32 条（记录可包含多个路径模板）
- 扫描剪枝人工分组：6 组
- DELETE 分类人工分组：7 组
- KEEP 分类人工分组：3 组
- AI 明确 DELETE/KEEP 规则合计上限：100,000 条
- 支持 `exact_path`、`path_prefix`、`path_glob`、`filename_glob`、`path_regex`、`filename_regex`
- 三份文件都含 `_ai_editing_contract`
- 只接受当前 `schema_version=2`，没有旧规则格式兼容层
- 首次启动生成 `DevClean-default-rules-backup.zip`
- UI 支持编辑、严格校验、重新载入和恢复默认配置

规则优先级：KEEP 始终高于 DELETE。

来源分组：

- AI 明确结论：`source=AI_IMPORT`、`group=ai_import`
- 用户对 AI-UNSURE 项的最终决定：`source=USER_DECISION`、`group=user_decision`
- 清空 AI 判决只删除 `AI_IMPORT`，不会删除用户手工规则或 `USER_DECISION`

## 5. AI 导入语义，后续不得混淆

### 同一次运行

保留完整导出包，导入时要求：

- 会话 ID、nonce、包摘要一致
- 所有候选一一对应
- 不能缺项、重复、编造候选
- 只能使用 KEEP / RECOMMEND_RECYCLE / UNSURE
- 每项必须有有界说明

### 程序重启后

完整内存包已不存在，使用 `DevClean-data\ai-sessions.json` 中的候选 ID/路径映射恢复。这里按用户明确决定，允许只导入模型已经回答的部分；不能擅自改成“必须回答全部才接受”。

无论哪种导入：

- 模型不能改路径或扩大范围。
- 模型不能选择回收站还是永久删除。
- 模型结论本身没有执行权限。
- 实际对象在删除时重新读取并核对。
- 严格完整导入若仍含 `UNSURE`，会话索引暂不删除，便于重启后重新导入并完成用户决定。

## 6. 用户明确决定：后续模型不得擅自违背

下面不是 Codex 的建议，而是项目所有者已经明确作出的产品决定。

1. DevClean 是 Windows 11 磁盘扫描和垃圾清理工具，不是云平台、企业后台或核安全系统。
2. 产品必须给普通用户直接使用，首选单文件 EXE；用户不能被要求安装 Python。
3. EXE 必须保持小，当前构建门槛为 50 MB，不能为了架构形式膨胀到几百 MB。
4. 需要实际 UI；当前 Tk GUI 可接受。
5. 程序自己确定可删的垃圾直接进左栏，不要求 AI。
6. 程序不确定的文件先交给 AI，AI 必须逐项解释是什么、为什么可删/不可删/不确定。
7. 只有 AI 仍不确定的项目才交给用户最后决定。
8. AI 导入后立即自动分类，不要求用户再执行一层“应用结果”。
9. AI 或用户确认可删，只是进入可删除规则/左栏，不会自动删除。
10. 用户点击“彻底删除”时，允许永久删除，包括 AI 推荐和用户最终确认的项目。
11. 用户点击删除按钮就是授权，不增加 `RECYCLE <scan-id>` 等口令。
12. 某个文件不能删除时必须自动跳过并继续下一个。
13. 删除中途失败后再次点击，必须能够重试仍存在的项目。
14. Windows Temp、Prefetch、Windows Update 下载、WER、Windows.old 中明确支持的垃圾必须能删除，不能只扫描不执行；放行范围仍由精确已知根控制。
15. AI 明确判决必须持久化，同一路径下次扫描不再花钱询问。
16. AI 明确判决最多保留 100,000 条。
17. 扫描、删除、确定不删统一由三份可编辑 JSON 管理；不要再放一套 Python 内置分类表。
18. 三份配置必须支持人工分组、正则、UI 编辑、严格 AI 编辑合同和默认备份恢复。
19. 不保留旧规则格式兼容层，避免叠床架屋。
20. 重启后的 AI 备用导入允许部分答案，这是用户明确保留的行为。
21. SQLite 只用于有上限的删除意图日志，不保存全量扫描结果；完成批次最多保留 128 批。不要把它改成庞大索引库。
22. 不为此项目增加 GitHub 服务、账号体系、远程后台或其他无关设计。
23. 旧测试集已按用户要求删除；用户明确要求不要运行测试集。后续若想恢复或新建测试，必须先征得用户同意。
24. 对项目行为不确定时，先按当前行为是有意设计处理，逐项解释并询问，不能借“审计”擅自改产品决策。
25. 发布或提交时作者不能写成 Claude、Codex 或其他模型；不得未经要求 push。
26. 旧测试和确认不可达的旧代码直接删除，不为“保底”或假想未来用途继续保留。
27. CI 和开发环境只维护当前 Windows EXE 所需的 Python 3.13，不维护三版本矩阵或 wheel 发布物。

## 7. 本轮验证证据

本轮没有运行 pytest 或任何已删除测试集。

已执行：

- `ruff check src scripts`：通过
- Python `compileall`：通过
- mypy：`src + scripts` 通过，无类型错误
- GUI 入口可达性复核：没有不可达的非初始化运行时模块
- PyInstaller 单文件构建：通过
- 源码入口 `--ui-smoke` GUI 构造与退出码检查：通过
- 成品 EXE 内 `devclean.config` 模块及三份默认规则归档检查：通过
- 最终 `release\DevClean.exe --ui-smoke` 实际启动：退出码 0
- EXE 50 MB 体积门槛：通过
- 发布载荷白名单：通过
- 最终 release EXE SHA-256 复核：通过

构建警告仅为 Windows 上不存在的 POSIX/Java 可选模块，以及 PyInstaller 对 `collections.abc` 的已知分析提示；GUI 冒烟已实际导入并构造应用窗口，因此不是缺失的运行时依赖。

## 8. 已知边界，不应伪装成已解决

- 当前没有代码签名，首次运行可能出现 Windows SmartScreen 提示。
- 回收站模式本身不释放空间；需要清空回收站才释放。
- Windows 可能在对象放不进回收站时静默永久删除；DevClean 会把“回收站条目数没有增加”报告为不可恢复，不会谎报成功进入回收站。
- 整个目录的永久删除不是原子操作；中途遇到占用项时，前面已删部分不会恢复。
- 本轮未进行真实垃圾文件删除演练，因为用户禁止运行测试集；本轮验证覆盖静态边界、打包、GUI 构造和发布产物，不应被后续模型描述成“已做全量真实删除测试”。

## 9. 本轮涉及文件

- 保留的产品源码：`src\devclean\ui\`、`scanner\`、`core\`、`platform\windows\`、`config\`
- 保留的构建入口：`scripts\build_windows_exe.ps1`、`scripts\devclean_gui_entry.py`
- CI：`.github\workflows\ci.yml`
- 公开说明：`README.md`、`docs\使用说明.md`、`docs\架构与部署.md`
- 最终产物：`release\DevClean.exe`、`release\licenses\`

后续审核应先逐项核对本交接中的用户决定，再报告确认存在的 bug。不要把个人偏好、架构洁癖或与磁盘清理无关的风险包装成功能缺陷。
