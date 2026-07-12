# G2 ProcMon 与多机只读 smoke 验收协议

## 门槛声明

G2 是第一行清理执行代码之前的硬门槛。本协议只观测 v0.1 inventory。单个清单 `PASS` 仅表示该环境的证据完整；G2 必须由**同一产品 artifact**在两台不同真机和一台 disposable VM 上分别通过后，才能由矩阵验证器判为通过。

当前仓库没有任何证据自动满足 G2。测试 fixture、合成 CSV、开发机自测或模型审查都不能替代外部三机矩阵。
每份 G2 清单还必须通过 `prerequisites` 引用同一 product binding 的 G1 `PASS` 矩阵结果
（`GATE_RESULT_JSON`）；复制一段“G1 已通过”的文字不构成前置门槛证据。

## 每台机器的前置条件

1. Windows 11，标准完整性测试账户；ProcMon 自身可由管理员运行，但 Reclaimer 及厂商 CLI 必须由标准用户进程树启动。
2. 从同一 wheel/source artifact 安装，记录 artifact SHA-256、源码 revision 和版本。三份清单的三元组必须完全一致；`source_revision` 必须是完整的 40 或 64 位小写 Git object ID，不能使用分支名、标签、缩写 SHA 或未提交工作树标记。
3. 建立本机私有证据目录；PML/CSV 可能含路径、registry 和命令行，默认 `contains_sensitive_data=true`，不得提交公共仓库。
4. 为每个实际受管 cache root、随机用户资产金丝雀和允许的 Reclaimer/vendor operational sandbox 分配不含真实路径的唯一标签。允许写入根不得与受管根或用户资产根重叠，也不得扩大到用户 profile、盘符根或临时目录总根。
5. 已知厂商查询写入必须被重定向到专用 sandbox 或作为最小根逐项声明。没有证据的写入不能事后追加宽泛白名单。

## ProcMon 捕获完整性

使用 Microsoft Process Monitor 的当前正式版本，保留版本号。捕获必须覆盖 File System、Registry、Process/Thread 和 Network 类别：

1. 先运行 [服务状态采集脚本](../../scripts/capture_service_state.ps1)生成 `service-before.json`。
2. 启动 ProcMon，确认 **Drop Filtered Events 关闭**、网络地址反向解析关闭，清空旧事件后开始捕获。不要只按 `python.exe` 名称预过滤；否则同名进程和后续子进程会造成遗漏/混淆。
3. 从标准用户终端运行一次完整 adapter smoke，并记下根 PID、开始/结束时间和固定 argv。不得启动 Docker Desktop/Ollama，不得运行 `npm cache verify`、DISM、PowerShell 维护命令或任何 `apply` 命令。
4. 进程退出后立即停止捕获并保存原始 PML。使用 ProcMon Process Tree 定位该根 PID，将完整 process subtree 加入显示过滤器；确认显示中含根进程生命周期和所有厂商子进程。
5. 保存过滤器/Process Tree 截图，导出“当前显示事件”为英文列名 CSV。原始 PML、截图和 CSV 三者都必须进入 manifest 并绑定 SHA-256。
6. 再运行服务状态脚本生成 `service-after.json`。before/after 的 Docker/Ollama 服务与后台进程数组必须完全相同；这补足 ProcMon 不能单独证明服务未启动的边界。

示例（证据目录须预先存在，脚本拒绝覆盖）：

```powershell
powershell -NoProfile -File scripts/capture_service_state.ps1 -Label before -Output <evidence-dir>\service-before.json
# 在 ProcMon 捕获窗口内，由标准用户运行固定的 reclaimer scan/report smoke。
powershell -NoProfile -File scripts/capture_service_state.ps1 -Label after -Output <evidence-dir>\service-after.json
```

## 保守 CSV 验证

[验证器](../../scripts/validate_procmon_csv.py)采用闭集判定：

- 注册表 mutation 一律失败；
- 受管 cache/user asset 根内的 mutation 一律失败；
- 仅明确允许根内的文件 mutation 可列为 `allowed_writes`；
- 未分类 operation、缺失 `CreateFile Desired Access`、不可解析写入路径一律失败；
- `dism.exe`、`npm cache verify`、Docker Desktop/Ollama 服务启动和常见 `Start-Service`/`sc start`/`net start` 进程事件一律失败；
- 命名管道读写按 IPC 观测，不误写为磁盘写入；允许写入清单仍以根标签和路径 SHA-256 输出，不复制敏感路径。
- TCP/UDP 只允许远端为 `127.0.0.1`/`localhost`/`::1`；任一不可解析或非回环网络事件失败。
- `Desired Access` 按下一个 `Field Name:` 边界解析，不能在权限列表第一个逗号处截断；因此
  `Read Attributes, Write Data` 必须识别为 mutation。

为每个实际根重复参数；不要用示例占位路径运行：

```powershell
uv run --frozen python scripts/validate_procmon_csv.py <filtered.csv> `
  --required-process python.exe `
  --protected-root hf_cache=G:\isolated\hf-cache `
  --protected-root random_canary=G:\isolated\protected-canary `
  --allowed-write-root reclaimer_data=G:\isolated\reclaimer-data `
  --allowed-write-root vendor_sandbox=G:\isolated\vendor-sandbox `
  --output <evidence-dir>\procmon-validation.json
```

退出码 `0` 且 JSON `verdict=PASS` 才能通过相应检查。CSV 验证器不能证明捕获过滤器没有漏事件，因此 PML、Process Tree/过滤器截图和服务状态快照不可省略。

## 单机 manifest

复制 [G2 单机模板](templates/g2-machine-manifest.template.json)，填入真实但脱敏的环境信息。机器 fingerprint 应为稳定机器标识与项目专用盐连接后的 SHA-256；不得落盘原始 MachineGuid、主机名或用户名。

每台机器必须提供以下证据类型：

- `PRODUCT_ARTIFACT`
- `PROCMON_PML`
- `PROCMON_CSV`
- `PROCMON_FILTER_SCREENSHOT`
- `PROCMON_VALIDATION_JSON`
- `SCAN_REPORT_JSON`
- `SERVICE_STATE_BEFORE_JSON`
- `SERVICE_STATE_AFTER_JSON`
- `TEST_REPORT_JSON`
- 同一 product binding 的 G1 `GATE_RESULT_JSON`

`SCAN_REPORT_JSON` 必须满足现有 scan-report schema，且所有资源 `actionable=false`、`safety_boundary.executable=false`、adapter effect class 仅为 `PURE_QUERY` 或 `OBSERVATION_WITH_OPERATIONAL_WRITES`。九个 adapter 都必须实际运行且不能为 `ERROR`；单机允许产品缺失/版本不支持，但 manifest 的 `available_adapters` 必须与报告逐项相符。三机矩阵的 AVAILABLE 并集必须覆盖全部九个 adapter，因此 Conda、已在线 Docker/Ollama 和真实 VS Code extension root 不能一直被跳过。未知版本只能 inventory-only；大小必须保留 logical/allocated/vendor logical/host physical 的不同口径与 confidence。

`TEST_REPORT_JSON` 必须直接证明 unknown/future version 拒绝扩大能力和发行 CLI 无 `apply`/
`execute`/通用删除入口。服务快照必须包含采集脚本 SHA-256、字节数和
`process_elevated=false`；before/after 的 collector hash、服务与进程投影必须一致。
每台机器还必须按该机实际 `AVAILABLE` 的 CLI 适配器，把 `hf.exe`、`python.exe`、
`uv.exe`、`conda.exe`、`node.exe`、`docker.exe` 中对应项列入
`required_process_names`，并由 ProcMon 结果证明全部出现。pnpm/VS Code 的文件系统盘点
和 Ollama 的固定回环 HTTP 不虚构厂商子进程要求。

每个必选 check 的 `evidence_refs` 必须直接引用支持它的 artifact。厂商工具未安装可作为额外 optional check 的 `SKIP` 并解释，但上述 G2 安全检查不能跳过。

单机校验：

```powershell
uv run --frozen python scripts/validate_gate_evidence.py <machine-manifest.json>
```

## 三机矩阵判定

准备两个不同 `machine_fingerprint_sha256` 的 `PHYSICAL` manifest 和一个第三 fingerprint 的 `DISPOSABLE_VM` manifest，然后执行：

```powershell
uv run --frozen python scripts/validate_gate_evidence.py `
  <physical-a.json> <physical-b.json> <disposable-vm.json> --matrix G2
```

矩阵校验器同时要求：

- 三份单机清单均为完整 `PASS`；
- run ID 和机器 fingerprint 唯一；
- 至少两台真机和一台 disposable VM；
- 三台均为 Windows 11 x64，且矩阵至少同时覆盖 `en-US` 与 `zh-CN`；
- product version、source revision、artifact SHA-256 完全一致；
- 三台机器 `available_adapters` 的并集精确覆盖九个内置 adapter；
- 所有 manifest 证据文件仍位于各自 manifest 目录内且字节数/SHA-256 匹配。

缺少外部机器、真实产品、原始 PML 或完整 process-tree 过滤证据时，结论只能是 `INCOMPLETE`，不得据此实现任何清理执行能力。
