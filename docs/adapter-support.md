# 适配器支持与命令能力基线

## 1. 本文档的约束力

本表定义当前 v0.1 inventory 可以探测的官方接口，并登记未来版本的候选动作。当前仓库已经实现九个内置只读适配器，但没有 cleanup execution code；“未来动作”不是已实现或默认授权。

适配器只支持通过 probe 验证的命令和版本。不得根据训练知识猜参数，也不得为完成扫描自动安装、升级、登录或启动厂商服务。

九适配器的本地实现不代表退出门槛 G2 已通过。当前仍缺 ProcMon/等价写入证明、第二台真机、disposable VM 和部分真实产品 smoke；在这些证据补齐前，所有结果保持 `actionable=false`，不得据本表增加清理执行路径。

## 2. 动作能力模型

每个未来动作必须显式声明：

```text
preview_mode: VENDOR_EXACT | VENDOR_ESTIMATE | INTERNAL_SNAPSHOT | NONE
selection_mode: EXACT_IDS | POLICY_GC | WHOLE_CACHE
output_mode: JSON | STABLE_TEXT | OPAQUE
reclaim_scope: HOST_PHYSICAL | VENDOR_LOGICAL | BOTH | UNKNOWN
undo_capability: VENDOR_ROLLBACK | RECYCLE_BIN | NONE
reconstruction: EXACT | REDOWNLOAD_BEST_EFFORT | REBUILD_BEST_EFFORT | NONE
```

没有 dry-run 的命令不得标为 `VENDOR_EXACT`。可重新下载或重建不是 undo，必须分别展示。

## 3. 支持矩阵

| Adapter | Phase 0/v0.1 inventory | 版本/探测 | 未来最早动作 | Preview / 输出 | 关键限制 |
|---|---|---|---|---|---|
| `huggingface` | `hf cache ls --revisions --format json` | 当前实现仅接受 `huggingface_hub >=1,<2`；1.0–1.10 与 1.11+ 分别绑定不同 JSON shape | v0.2 `hf cache prune/rm --dry-run` 后重新确认 | ls JSON；rm/prune 有 dry-run，但 preview 为文本 | 不使用 `repo@rev` 伪语法；Xet/assets 分开记账；2.x 未复核前拒绝 |
| `ollama` | 对已在线的 `http://localhost:11434` 调 `GET /api/version`、`GET /api/tags`、`GET /api/ps` | endpoint 不通即 unavailable；不运行 `ollama serve` | v0.3 单模型 `DELETE /api/delete` | JSON；无 dry-run、无 conditional digest delete | 删除前 name→digest 复核；运行中或自建模型默认跳过/高风险 |
| `pip` | 每个显式解释器运行 `<python> -m pip cache dir/info/list --format=abspath` | 当前实现接受 pip `>=21,<27`；多个解释器的 cache root 由报告层识别 | v0.3 整缓存 `pip cache purge` | 文本；无 JSON、无 dry-run；list 只枚举 wheel | 设置 `PIP_DISABLE_PIP_VERSION_CHECK=1`；大小由安全扫描器计算 |
| `uv` | `uv cache dir` + `uv cache size`（原始字节） | 当前实现接受 uv `>=0.9.8,<0.12`，并探测 `size` 能力 | v0.3 `uv cache prune` | 文本；无 dry-run、无 JSON；size 为 experimental estimate | 已用 uv 0.11.6 的本地 `cache prune --help` 核实存在 `--force`；未来执行必须显式禁止该旗标并继续按版本门控 |
| `conda` | 分别运行 `conda clean --index-cache/--tarballs/--logfiles/--packages --dry-run --json` | probe `conda clean --help` 并记录版本 | v0.2 仅 index/tarballs/logfiles；v0.3 packages | JSON + dry-run | 不用 `--all` 混淆档位；永久禁止 `--force-pkgs-dirs` |
| `npm` | 直接 `node.exe + npm-cli.js` 运行 `npm config get cache`、`npm cache ls`，随后安全扫描 cache root | 当前实现接受 npm `>=8,<12`，并核验 node/npm 同一安装树 | v0.3 `npm cache verify` 作为执行型 GC；不自动串联 clean | 文本；无 dry-run、无 JSON | verify 会写并清垃圾，绝不能用于 inventory |
| `pnpm` | 只扫描当前用户约定的 v10/v11 store 根；不调用 pnpm CLI | 目录主版本 10/11 可分类；旧/未知根保持 RED/UNKNOWN | v0.3 `pnpm store prune`（需另做 CLI 版本门控） | 当前 inventory 为纯文件元数据；prune 无 dry-run/JSON | `store path` 会做链接能力探测写入，`status` 可能读写 SQLite index，二者均未进入默认 inventory |
| `docker` | daemon 已在线时，经固定本机 named pipe 运行 `docker system df --format json` | 当前实现接受 client/server `>=24,<31`；不得启动 Docker Desktop | v0.2 仅精确 dangling image ID；v0.3 才考虑带保留期的 builder/image policy prune | df 为四条严格 JSONL；prune 无 dry-run | 使用空的 Reclaimer 配置沙箱，属 operational writes；不读取用户 context/credential；不使用 `-a`、volume、force；宿主物理释放未知 |
| `vscode` | 直接只读扫描 stable、Insiders 和安全显式自定义用户扩展根，解析有上限的 `package.json`；不调用 `code` CLI | 未发现扩展根即 unavailable；其他产品与 portable/profile 变体 deferred | v0.1–v0.3 无清理动作 | 文件元数据与严格 JSON；无官方只读清理 API | `code --list-extensions` 初始化扩展管理时可能清理旧 VSIX/签名归档/`.trash`，属于 MAINTENANCE，禁止用于 inventory |
| `windows_maint` | v0.1 只探测 Windows build/卷能力并生成 REPORT_ONLY 指引 | DISM 必须管理员运行，因此不属于普通权限 inventory | v0.5 签名 broker 后才可 Analyze/StartComponentCleanup、DO cleanup | DISM 英文稳定文本；DO 无 WhatIf | 当前只显示命令而不执行；DISM 写日志；永不 ResetBase、IncludePinnedFiles 或裸删系统目录 |

## 4. 推荐 argv 与副作用登记

以下是实现时的 argv 形状，不是供 shell 拼接的字符串：

### Hugging Face

```text
[hf.exe, cache, ls, --revisions, --format, json]
[hf.exe, cache, prune, --dry-run]
[hf.exe, cache, prune, --yes]
[hf.exe, cache, rm, <revision_hash>, --dry-run]
[hf.exe, cache, rm, <revision_hash>, --yes]
```

`prune` 是策略 GC，候选可能随缓存变化；execute 前必须重新 preview，发生变化就重新确认。Xet cache 没有高层清理 API，官方仅说明可删除整个目录，因此它不能伪装为 `hf cache` 的 JSON candidate。

### Ollama

```text
GET    http://localhost:11434/api/version
GET    http://localhost:11434/api/tags
GET    http://localhost:11434/api/ps
POST   http://localhost:11434/api/show   {"model": "<name>"}
DELETE http://localhost:11434/api/delete {"model": "<name>"}
```

GET inventory 只在现有 daemon 在线时调用。模型 `size` 不是独占可释放字节；共享 blob 存在时必须标 estimate/unknown。`ollama pull <name>` 可能取得不同 digest，本地 Modelfile/权重也可能已经不存在。

### pip

```text
[python.exe, -m, pip, cache, dir]
[python.exe, -m, pip, cache, info]
[python.exe, -m, pip, cache, list, --format=abspath]
[python.exe, -m, pip, cache, purge]
```

`list` 只列 wheel；HTTP cache 必须通过根目录安全扫描计量。`purge` 是 whole-cache 且无预览，因此未来不得默认勾选。

### uv

```text
[uv.exe, cache, dir]
[uv.exe, cache, size]
[uv.exe, cache, prune]
```

`prune` 会删除 dangling entries 和缓存环境，依赖 uv 自己的锁；禁止以 `--force` 绕过并发检查。

### Conda

```text
[conda.exe, clean, --index-cache, --dry-run, --json]
[conda.exe, clean, --tarballs, --dry-run, --json]
[conda.exe, clean, --logfiles, --dry-run, --json]
[conda.exe, clean, --packages, --dry-run, --json]
```

v0.2 未来执行必须使用与预览相同的类别集合，再把 `--dry-run` 替换为 `--yes`。官方警告 packages 检测不到反向 symlink，不能把 dry-run 等同于“不会破坏任何环境”。

### npm/pnpm

`npm cache verify` 会验证并垃圾回收，因此是 execute；Reclaimer 不把它自动串联到 `npm cache clean --force`。`pnpm store path` 在部分版本会通过临时文件、目录和硬链接探测存储位置，`pnpm store status` 又依赖当前项目且部分版本会读写 store SQLite index；因此当前 pnpm inventory 两者都不调用，只按已核验的用户级约定根做文件系统盘点。`pnpm store prune` 没有 preview。

### Docker

```text
[docker.exe, --config, <RECLAIMER_EMPTY_CONFIG>, --host,
 npipe:////./pipe/docker_engine, system, df, --format, json]
[docker.exe, builder, prune, --filter, until=168h]
[docker.exe, image, prune, --filter, until=168h]
```

未来执行时具体保留周期是用户可见策略，不得硬编码为安全事实。不得传 `-a`、`--volumes` 或 `--force` 绕过 Reclaimer 的确认。Docker Desktop/WSL 后端中，daemon 内部释放不保证 VHDX 自动缩小；verify 必须分别记录 vendor logical 与 host physical。

### Windows

v0.5 通过签名 broker 门槛后，64 位 broker 才可使用固定 argv：

```text
[dism.exe, /Online, /Cleanup-Image, /AnalyzeComponentStore, /English,
 /LogPath:<reclaimer-evidence-log>, /LogLevel:1]
[dism.exe, /Online, /Cleanup-Image, /StartComponentCleanup, /English,
 /NoRestart, /LogPath:<reclaimer-evidence-log>, /LogLevel:1]
```

`AnalyzeComponentStore` 是报告，不是列出精确删除文件的 dry-run。`StartComponentCleanup` 立即移除 superseded component versions，必须是不可逆红档。Delivery Optimization 的删除 cmdlet没有 `WhatIf`；不传 `-IncludePinnedFiles`。

## 5. 外部进程统一要求

- probe 时解析绝对路径并记录版本、来源和可执行文件证据，不从当前目录解析；当前
  command observation 在查询前后分别比对大小、mtime、SHA-256 与可用的卷序列号/file ID/
  file-ID kind，任一变化都拒绝发布证据；这属于 replacement detection，不是 Authenticode
  发布者签名验证；
- argv 数组、`shell=False`、隐藏窗口、超时、进程树终止和 stdout/stderr 字节上限；
- Windows 上每次外部查询必须先创建启用 `KILL_ON_JOB_CLOSE` 的独立 Job Object，随后立即把
  `Popen` 返回的进程分配进去；创建、配置或分配失败时 fail closed，`taskkill` 只作 Job API
  失败后的极端后备；正常退出也终止并关闭 Job，以清理仍持有管道或继续运行的后代；
- 尽可能强制无颜色/稳定语言；DISM 使用 `/English`；
- stdout/stderr 中的 ANSI、控制字符、路径与 token 必须转义/脱敏；
- 原始 stdout/stderr 只供内存解析和源哈希，不得写盘；落盘只能是保守脱敏 UTF-8 或
  deterministic withholding marker，并分别记录 source/stored 大小与 SHA-256；scan-scoped
  证据根使用受保护私有 DACL，但用户主动导出的报告不自动继承该保护；
- 非零退出、超时、版本未知、Schema 错误和输出截断都 fail closed；
- inventory 不启动服务、不访问远端 registry、不升级工具、不写厂商 cache；
- golden transcript 只能来自脱敏的真实输出，并记录版本、OS build 和 SHA-256。
当前实现存在 `Popen` 成功到 `AssignProcessToJobObject` 之间的极小启动窗口。没有使用
`CREATE_SUSPENDED`，因为 Python `Popen` 不公开可安全恢复的主线程句柄，贸然挂起会产生无法恢复的
进程。后续若启动窗口进入不可接受的威胁模型，可用原生 `CreateProcessW` + `STARTUPINFOEX` 的
job-list 属性在创建时完成归属，并为该专用启动器单独做句柄继承与跨版本测试。

## 6. 一手来源

- [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/en/package_reference/cli)
- [Hugging Face cache/Xet](https://huggingface.co/docs/huggingface_hub/guides/manage-cache)
- [Ollama API：tags](https://docs.ollama.com/api/tags)、[ps](https://docs.ollama.com/api/ps)、[delete](https://docs.ollama.com/api/delete)
- [pip cache](https://pip.pypa.io/en/stable/cli/pip_cache/)
- [uv cache](https://docs.astral.sh/uv/concepts/cache/)
- [conda clean](https://docs.conda.io/projects/conda/en/stable/commands/clean.html)
- [npm cache](https://docs.npmjs.com/cli/cache/)
- [pnpm store](https://pnpm.io/cli/store)
- [Docker system df](https://docs.docker.com/reference/cli/docker/system/df/)、[builder prune](https://docs.docker.com/reference/cli/docker/builder/prune/)、[pruning](https://docs.docker.com/engine/manage-resources/pruning/)
- [VS Code CLI](https://code.visualstudio.com/docs/configure/command-line)、[Portable mode](https://code.visualstudio.com/docs/setup/portable)
- [DISM component store analysis](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/determine-the-actual-size-of-the-winsxs-folder?view=windows-11)
- [DISM global options](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-global-options-for-command-line-syntax?view=windows-11)
- [Delivery Optimization cmdlet](https://learn.microsoft.com/en-us/powershell/module/deliveryoptimization/delete-deliveryoptimizationcache?view=windowsserver2025-ps)
