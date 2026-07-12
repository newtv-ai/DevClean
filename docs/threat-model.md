# Reclaimer v0.1 inventory 与 report-only 威胁模型

## 1. 适用范围

本模型覆盖当前仓库已经实现的 Windows workflow：普通权限下扫描显式本地根、运行九个内置只读适配器（Hugging Face、pip、uv、Conda、npm、pnpm、Docker、Ollama、VS Code）、保存本地状态与证据、导出报告、为已保存候选生成不可执行的 `REPORT_ONLY` review plan，以及将用户逐项明确选择的普通扫描文件送入 Windows Recycle Bin。Windows Component Store 仅提供由用户自行复制执行的官方命令指引；Reclaimer 不运行该命令。

当前范围只有一个可恢复的文件变更：`recycle` 仅接受已完成 scan 中的精确 candidate ID，显示完整路径并要求在 TTY 输入 `RECYCLE <scan-id>`；它拒绝路径参数、目录、glob、`--yes`、hard link、reparse/Cloud Files、敏感用户资产和应用状态目录。每个文件在 Shell 调用前重验固定本地卷、128-bit file ID、size、timestamps、attributes 和 link count，并以完整路径和 `FOF_ALLOWUNDO` 送往回收站。不存在永久删除、厂商 GC、维护、提权、服务启动、运行时插件、BleachBit 调用或 AI 决策。`plan create/show` 仍不是执行能力。

文中对 future executor、direct filesystem action 与 broker 的约束是后续门槛，不是当前能力承诺。

## 2. 保护资产

- 用户源代码、模型、环境、包存储、凭据、会话和应用状态；
- Windows 系统文件、回滚数据、安装缓存和更新状态；
- 扫描报告中的路径、软件版本、目录规模、候选名称和使用时间；
- SQLite 状态、命令/loopback 证据、脱敏 transcript 及其完整性元数据；
- Reclaimer 的规则、Schema、可执行文件、依赖锁、构建产物和未来发布密钥；
- 扫描可用性，避免无界遍历、内存耗尽、厂商 CLI 挂死或遗留后代进程。

## 3. 当前信任边界

```text
用户/CI
  │
  ▼
Reclaimer 普通权限主进程（elevated token 会被拒绝）
  ├── Windows 文件系统（不可信名称、ACL、reparse point、Cloud Files）
  ├── 已显式注册的厂商 CLI（不可信路径、版本、输出和副作用）
  ├── Docker 本机 named pipe（仅 daemon 已在线时查询）
  ├── Ollama 127.0.0.1:11434（固定 GET endpoint，响应不可信）
  ├── SQLite 状态与 scan-scoped 证据目录（本地敏感数据）
  └── 用户选择的报告输出位置（可能不受 Reclaimer 私有 DACL 保护）

不存在：管理员 broker、永久删除或目录递归删除、厂商 cleanup executor、BleachBit 运行路径、
运行时插件、云端服务、远程 Ollama endpoint 或 AI 控制面。
```

状态目录与每个 scan-scoped 证据根是应用管理边界；用户主动导出的报告是一次单向输出，目标目录权限由用户负责。

## 4. 强制安全不变量

1. inventory 不修改被扫描目标，不删除、移动、截断、解压或触发厂商维护。
2. `doctor` 可以在 elevated token 下报告该诊断，但不会在该状态下打开 inventory/state；其余 CLI 检测到 elevated token 或无法确认 elevation 状态时拒绝继续。任何命令都不会请求 UAC。
3. 文件遍历只接受本地固定卷，不跟随任何 reparse point；Cloud Files placeholder 作为边界，不打开数据流召回内容。
4. 不启动 Docker Desktop、Ollama、VS Code、WSL、Windows 服务或其他厂商守护进程；离线产品保持 unavailable。
5. 外部命令仅使用解析后的普通本地 `.exe`、argv 数组、`shell=False` 和最小环境；禁止 `cmd /c`、
   batch 文件与自由字符串拼接。
6. 每个 Windows 外部查询使用启用 `KILL_ON_JOB_CLOSE` 的独立 Job Object；Job 创建、配置或分配失败时拒绝该查询。超时、输出超限、异常和正常退出都会收尾整个已分配进程树。
7. 外部可执行文件在查询前后分别观察并比对大小、mtime、可用的卷序列号/file ID/file-ID kind 与 SHA-256；不一致时该观察失败。当前没有 Authenticode/发布者签名验证，且该比对不能消除 `Popen` 到 Job 分配之间的极小启动窗口。
8. 外部输出只作为不可信数据；命令/API 必须有固定作用域、超时、字节上限、严格编码/JSON 规则和失败降级。
9. 原始 CLI stdout/stderr 与 Ollama response 只供当前进程内解析和哈希，不能写盘；持久化内容只能是通过保守脱敏的 UTF-8 或确定性 withholding marker，并分别记录源字节与落盘字节的大小和 SHA-256。
10. SQLite 状态父目录与 scan-scoped 证据根必须位于无 reparse 祖先的本地固定卷。Windows 目录使用受保护 DACL，仅向当前 token SID、LocalSystem 与 Builtin Administrators 授予可继承完全控制；数据库文件另设受保护、不可继承的同 SID DACL，拒绝多硬链接，并在打开前和创建后审计。迁移备份采用同一文件策略；失败即拒绝继续。该保证不自动延伸到用户选择的报告导出目录。
11. 任一空间口径无法证明时使用 `unknown`；不得用逻辑大小或 vendor logical 数值冒充实际可释放的 host physical 空间。
12. 所有当前 Resource 都是 `actionable=false`。存储的 review plan 只能包含不可执行的 `REPORT_ONLY` 动作；报告和计划都不能作为执行授权重新导入。
13. 任一新增删除、维护、broker 或执行能力必须先更新威胁模型与 ADR，并通过对应阶段门槛。

## 5. 威胁与当前控制

| ID | 威胁 | 影响 | 当前控制 | 尚需验证/必测场景 |
|---|---|---|---|---|
| T01 | junction/symlink/mount point 指向批准根之外 | 越界读取、未来误删 | `DirEntry.stat/is_dir(follow_symlinks=False)`；检测 reparse 属性后停止下降；根及状态/证据路径拒绝 reparse 祖先 | G1 仍需真机 mount point、循环 junction、OneDrive、ReFS 与 removable fixture |
| T02 | 扫描期间路径被替换（TOCTOU） | 证据错配，未来执行越界 | inventory 在可用时记录卷序列号与 file ID；部分有界读取做前后身份检查；当前没有任何执行路径 | 未来 direct-FS action 必须做句柄级重核验；G5 未通过 |
| T03 | 硬链接、稀疏或压缩文件导致空间虚高 | 误报可释放空间 | file ID 有界去重；逻辑大小、分配大小、vendor logical 与 exclusive host reclaimable 分栏；无法证明即 unknown | 真机 NTFS/ReFS、稀疏、压缩与多硬链接矩阵 |
| T04 | Cloud Files 扫描触发下载 | 网络、空间和隐私副作用 | placeholder 作为边界；metadata handle 使用 no-recall 语义；不打开内容流 | OneDrive Files On-Demand 非 hydration 证据仍属开放 G1 |
| T05 | 恶意文件名/厂商输出注入终端、Markdown 或 JSON | 日志欺骗、解析绕过 | 控制/bidi 字符转义；严格 JSON decoder 拒绝 duplicate key 与 NaN；bounded parser；Schema 校验 | ANSI、换行、超长、非 UTF-8、重复 key、NaN 与输出截断 |
| T06 | PATH 劫持或查询可执行文件被替换 | 运行攻击者代码 | 解析后的普通本地 `.exe`；执行前后身份与 SHA-256 观察；变化则失败 | 当前没有签名验证；启动/分配窗口和真机替换攻击仍需持续审计，不得记为 G6 证据 |
| T07 | 厂商 inventory 命令具有写副作用 | 破坏“只读目标”承诺 | command allowlist 与 effect class；不确定则不用 CLI；pnpm/VS Code 采用文件系统 inventory；Docker operational writes 仅限空配置沙箱 | G2 ProcMon/等价 managed-root 与用户资产 trace 未完成；`npm cache verify` 永不用于 inventory |
| T08 | CLI 挂死、输出洪泛或遗留后代 | DoS、后台副作用 | 超时、stdout/stderr 独立字节上限、reader 收尾；Windows Job Object 进程树终止，失败时 fail closed | 正常退出、超时、输出超限和中断下的后代终止；保留 `Popen`→assignment 残余窗口 |
| T09 | 状态、证据或报告泄露路径/凭据 | 隐私或密钥暴露 | 默认路径/credential 脱敏；源 transcript 不落盘；source/stored hash 分离；状态/证据目录及数据库文件受限 DACL；数据库硬链接拒绝 | 脱敏不是通用 secret scanner；`--full-paths` 与自选报告目录需要用户明确承担风险 |
| T10 | 未授权提权 | 扩大损害面 | 主 CLI 拒绝 elevated token；不请求 UAC；Windows maintenance 只输出用户自行执行的指引 | 普通/管理员 token 与 elevation probe 失败均 fail closed；G6 broker 尚不存在 |
| T11 | 规则、适配器或依赖供应链被篡改 | 任意代码执行/错误分类 | 内置显式注册、锁定依赖、SBOM/校验和构建资产、无运行时插件 | 真实 GitHub CI/CodeQL 与项目 owner 的许可证确认仍属开放 G0 |
| T12 | preview/report 被误当执行授权 | 意外删除 | BleachBit 未集成；报告单向导出；plan schema/model/state retrieval 强制 `REPORT_ONLY`、disabled、non-executable；无 apply 命令 | 任意篡改计划或 actionable resource 必须被拒绝；G3/G4 执行状态机尚未实现 |
| T13 | 文件访问时间被扫描改变 | 用户状态或未来淘汰策略被污染 | 避免打开内容；文件树使用只读元数据 API；不依据未经证明的 atime 产生动作 | 真机不同 last-access policy 下扫描前后核对 |
| T14 | 巨型文件树、ACL 错误或慢 metadata 造成资源耗尽 | 扫描失败 | 迭代遍历、有界 hard-link identity 容量、批量 SQLite、逐项错误、可取消和进度心跳 | 已有百万记录与慢 metadata 本地证据；真实 access-denied/长路径矩阵仍属开放 G1 |
| T15 | loopback 被代理、重定向或伪服务利用 | 凭据泄露、访问远端或解析恶意响应 | Ollama 仅允许 `127.0.0.1:11434`、`GET /api/version|tags|ps`；不继承代理、不跟随重定向；按 endpoint 限长并严格解析 | 已在线真 Ollama 的 G2 smoke 尚未完成；不能自动启动或拉取模型 |
| T16 | scan 后路径替换、目录递归、敏感文件误选或回收站不可用 | 误移用户资产或永久删除 | `recycle` 无路径参数/无 `--yes`；只限最多 32 个精确 candidate；拒绝目录、reparse/Cloud、hard link、状态目录与 deny-list；执行前两次核验 file ID、timestamps、size、attributes、link count；Shell 使用 full path + `FOF_ALLOWUNDO`、nuke warning、no-recursion flags | 文件系统没有跨文件原子事务，第二次核验只缩小 TOCTOU 窗口；用户仍应核对终端展示的完整路径和 Windows Shell 的警告 |

## 6. 外部查询规则

每个适配器必须声明并持久化查询的 `effect_class`、版本支持范围、输出合同和已知 operational writes。`--help` 中不存在的参数不得猜测；未知或未来 major version 必须 unavailable 或 inventory-only，不能扩大能力。

当前允许两类外部观察：

- `PURE_QUERY`：预期不写厂商状态；
- `OBSERVATION_WITH_OPERATIONAL_WRITES`：只允许经过审计、明确限定到 Reclaimer 自有沙箱的查询副作用，例如 Docker 空配置目录。

`MAINTENANCE` 不允许进入 inventory。没有官方 dry-run 的命令不能伪装为 preview；会垃圾回收的 `npm cache verify` 属于未来 execute，而不是 inventory。探测不得自动安装、升级、登录、接受条款或启动服务。

报告导出是当前唯一由用户显式选择的普通文件写入：只允许本地固定卷上的新文件，拒绝
reparse 祖先、symlink/既有目标和覆盖写，并在同目录临时文件完整 flush 后原子发布。
导出文件仍继承目标目录策略；`--full-paths` 的隐私风险不会因原子写入而消失。

Windows 子进程在创建后立即分配到 Job Object，但当前 `Popen` 路径未使用原生 `CreateProcessW` + job-list attribute，因此仍有一个很小的创建至分配窗口。该残余风险必须保留在文档和测试中，不能用 `KILL_ON_JOB_CLOSE` 的存在掩盖。

## 7. 报告、证据与状态安全

- 解析器只在内存中接触原始 bounded bytes；任何原始 transcript/HTTP body 都不得作为 redaction 失败时的 fallback 写盘。
- 严格 UTF-8、控制字符、大小或 redaction 检查不通过时，落盘的是固定 marker，其中仅含 reason、源大小和源 SHA-256。
- `CommandEvidence` 记录已脱敏 argv、effect class、终止状态、源/落盘 stream 的独立大小与哈希，以及查询可执行文件的大小、mtime、SHA-256 和可用文件身份。
- `LoopbackEvidence` 的模型不能表达任意 hostname、port、method 或 endpoint；失败证据不保存异常消息，避免把环境秘密带入状态。
- Resource 只引用 evidence ID，不内嵌任意长度输出；adapter run、解析错误、截断、超时与编码拒绝必须在状态/报告中可见。`evidence:<id>` 与 `evidence_ids` 在写入时必须属于同一扫描，并在报告输出首字节前做有界流式复核；JSON Schema 不能表达的跨数组外键由这项运行期检查保证。
- evidence metadata 最后原子发布，避免把半写元数据当作完整证据。SQLite 使用事务、`synchronous=FULL`、foreign key 与 integrity check；这些机制不等同于未来 executor 的 durable intent/reconcile 协议。
- 默认报告脱敏路径和 vendor locator；`--full-paths` 是显式隐私降级。报告落点不是私有证据边界的一部分。

## 8. 超出本地回收站范围的未关闭风险

当前回收站路径只处理小批量、精确、可恢复的普通文件；它不解决以下风险：可执行 plan 的 TTL/preflight、持久 intent 与 crash reconcile、按句柄永久删除、厂商 GC 动态候选集、部分成功、不可逆确认、逻辑释放量与宿主物理释放量差异，以及最小权限签名 broker 的安装、IPC、ACL 与 DLL 搜索路径。

G1、G2、G5、G6 均未通过：

- G1 缺少完整真实文件系统/Cloud Files/多卷与 CI/机器矩阵证据；
- G2 缺少 ProcMon 或等价写入证明、第二台真机、disposable VM 与缺失产品的真实 smoke；
- G5 没有 direct-FS executor，也没有 10,000 次竞争矩阵或非作者独立审查；
- G6 没有签名 broker、安装器、IPC、代码签名或支持版本矩阵；现有只读安装验收器仅覆盖未来制品/ACL 子门槛，并固定报告 `g6_gate_passed=false`。

因此当前只允许上述 exact-file Recycle Bin 路径；不得增加永久删除、目录递归、厂商 cleanup、broker action 或 BleachBit `--clean`。任何扩大执行范围的 PR 必须先为对应风险提供设计和失败模式。

## 9. 发布门槛与当前结论

原方案中的 G0/G1/G2 可作为未来公开发布或扩大执行范围的审查资产；它们不阻塞当前本机 scan-and-recycle 工作流：

- G0：项目 owner 完成许可证决定，真实 GitHub Windows 3.11–3.13 CI、依赖审计与 CodeQL 通过；
- G1：完成 mount point、循环 junction、OneDrive placeholder、ReFS、removable、access-denied、取消和百万规模的规定矩阵；
- G2：以 ProcMon 或等价手段证明受管缓存根/用户资产无越权写入，并完成 2 台真机 + 1 台 disposable VM 的九适配器 smoke；
- 所有报告、Schema 与 release note 必须明确 `Preview / Inventory only`，任何 candidate 均不得 actionable。

截至 2026-07-12，已实现单机 scan-and-recycle；本文件不声明 G0、G1、G2、G5 或 G6 已通过，也不授权永久删除、目录递归或厂商清理能力。
