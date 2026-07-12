# Reclaimer v2 需求可追踪矩阵

## 判定规则

本矩阵以仓库外的《Reclaimer 可落地实施方案 v2》和《Claude 复核 v2 审定》为
工程权威输入。状态含义如下：

- `IMPLEMENTED`：当前授权范围内的代码、测试或文档已经存在；不等于退出门槛通过。
- `PARTIAL_EVIDENCE`：有本机或合成证据，但证据范围不足以关闭外部门槛。
- `EXTERNAL_BLOCKED`：需要项目 owner、真实 GitHub、额外真机/VM、ProcMon、签名证书或非作者 reviewer。
- `PREREQUISITE_BLOCKED`：方案明确禁止现在编写该执行能力。
- `FORBIDDEN`：属于 v0.x 非目标或永久禁区。

任何一行的实现状态都不能覆盖 [implementation-status.md](implementation-status.md) 中的
门槛账本。尤其是 G0/G1/G2/G5/G6 验证器、模板和合成测试只证明验收合同可运行，
不构成真实 PASS。

## Claude 审定增补

| 审定项 | 当前落点 | 状态与边界 |
|---|---|---|
| P1-1 DIRECT_FS_ACTION 硬编码用户资产 deny-list | [G5 race protocol](evidence/G5-direct-fs-race-protocol.md) 与 G5 static-audit 合同 | `PREREQUISITE_BLOCKED`：已经成为未来执行器硬门槛；G2/G3/G4 未通过前不写删除执行器 |
| P1-2 v0.1 REPORT_ONLY 系统维护价值 | `reclaimer guides` 与 `adapters/windows_maintenance.py` | `IMPLEMENTED`：只打印可复制的官方 DISM 分析命令，不运行、不提权、不 journal、不审计外部结果 |
| P2 SQLite 措辞 | [threat-model.md](threat-model.md) 与状态账本 | 当前仅有只读 SQLite 状态；未来 intent 必须依靠原子状态提交和 reconcile，不能宣称“换 SQLite 自动消除不确定窗口” |
| P2 G5 非作者复核 | G5 manifest/review attestation 验证 | `IMPLEMENTED` 验收合同；作者与 reviewer 重叠会失败，真实复核仍 `EXTERNAL_BLOCKED` |
| P2 broker 使用当前 .NET LTS | [G6 protocol](evidence/gates/G6-broker-verification.md) | 未钉死 .NET 8；broker 尚不存在，仍 `EXTERNAL_BLOCKED` |
| P2 G2 设备数 | [G2 protocol](evidence/G2-procmon-smoke-protocol.md) | 固定为 2 台真机 + 1 台 disposable VM，并要求 x64、en-US/zh-CN 和九适配器 AVAILABLE 并集 |
| P2 uv `--force` 核实 | [adapter-support.md](adapter-support.md) | 已用 uv 0.11.6 的本地 help 核实存在；未来 prune 明确禁止 `--force`，当前无 prune 代码 |
| P2 HF JSON 旗标版本门控 | `adapters/huggingface.py` 与版本绑定 transcript 测试 | `IMPLEMENTED`：1.x 两组 shape 分段；未知/2.x 不进入受支持 inventory 解析 |

## 阶段与退出门槛

| 阶段 | 当前安全交付 | 退出门槛状态 | 权威证据/阻塞 |
|---|---|---|---|
| 0 仓库/ADR/发布基线 | ADR、完整 GPLv3 固定字节契约、第三方边界、Schema、CI/CodeQL、锁文件、SBOM、可复现 wheel、许可文件校验 | G0 `EXTERNAL_BLOCKED` | [G0 protocol](evidence/G0-release-readiness-protocol.md)；缺 owner 最终许可决定、完整 Git revision 上的真实 GitHub CI/CodeQL 与发布授权 |
| 1 只读 Windows 核心 | streaming 扫描、SQLite、文件身份/大小、reparse/Cloud 边界、取消、doctor/scan/report | G1 `PARTIAL_EVIDENCE` | [G1 protocol](evidence/G1-physical-boundary-protocol.md)；缺固定 artifact 的 mount point、loop、OneDrive、ReFS、removable、access-denied 和真机证据 |
| 2 v0.1 九适配器 | HF、pip、uv、Conda、npm、pnpm、Docker、Ollama、VS Code inventory 与证据 containment | G2 `EXTERNAL_BLOCKED` | [G2 protocol](evidence/G2-procmon-smoke-protocol.md)；缺完整 ProcMon/PML/filter/service snapshot 及 2 真机 + 1 VM 产品矩阵 |
| 3 v0.2 精确/可预览厂商动作 | 只有不可执行 `REPORT_ONLY` review plan 的 Schema/存储表面 | G3 `PREREQUISITE_BLOCKED` | G2 未通过；无 action builder、apply、preflight、intent/reconcile、确认 UI 或厂商清理调用 |
| 4 v0.3 无 dry-run 策略动作 | 无执行实现 | G4 `PREREQUISITE_BLOCKED` | G3 尚不存在；pip/uv/npm/pnpm/Docker/Ollama 清理路径均未开放 |
| 5 v0.4 DIRECT_FS_ACTION | 只有未来 race/canary/static-review 验收资产 | G5 `PREREQUISITE_BLOCKED` | [G5 protocol](evidence/G5-direct-fs-race-protocol.md)要求 G2/G3/G4 clean PASS；无 executor、批准根、10,000 次真实竞态或非作者复核 |
| 6 v0.5 signed broker | 只有普通用户只读安装树/签名/ACL 子集验证器 | G6 `EXTERNAL_BLOCKED` | [G6 protocol](evidence/gates/G6-broker-verification.md)；无 broker、installer、证书、IPC、UAC、注入矩阵，验证结果固定 `g6_gate_passed=false` |

## v2 首批 22 个工作包

| # | 工作包 | 实现映射 | 状态 |
|---:|---|---|---|
| 1 | ADR-001 independent engine | [ADR-001](adr/ADR-001-independent-engine.md) | `IMPLEMENTED` |
| 2 | ADR-002 AI excluded | [ADR-002](adr/ADR-002-ai-excluded.md) | `IMPLEMENTED` |
| 3 | ADR-003 license boundary | [ADR-003](adr/ADR-003-third-party-license-boundary.md)、`LICENSE`、`THIRD_PARTY_NOTICES.md` | `IMPLEMENTED`；owner 决定仍属 G0 |
| 4 | SEC-001 threat model | [threat-model.md](threat-model.md)、`SECURITY.md` | `IMPLEMENTED` |
| 5 | CORE-001 models/Schemas | `core/models.py`、resource/plan/report Schemas | `IMPLEMENTED`；运行时长度、类型、数量和不可执行约束与 Schema 对齐 |
| 6 | CORE-002 SQLite/migrations | `core/state.py` | `IMPLEMENTED`：FULL synchronous、FK、迁移备份 DACL、严格 JSON、索引列/载荷复核；无未来执行 intent |
| 7 | WIN-001 file/volume identity | `platform/windows/filesystem.py`、`volumes.py` | `IMPLEMENTED` |
| 8 | WIN-002 reparse/Cloud boundary | `scanner/filesystem.py` | `IMPLEMENTED`；真实 OneDrive/mount/ReFS 矩阵仍属 G1 |
| 9 | SCAN-001 streaming/cancellation | `scanner/` 与百万行 benchmark | `IMPLEMENTED` + `PARTIAL_EVIDENCE` |
| 10 | CLI-001 doctor/scan/report | `cli/main.py` | `IMPLEMENTED`：console/json/markdown report；所有非 doctor 命令拒绝 elevated/未知 token |
| 11 | ADP-HF-001 | `adapters/huggingface.py` | `IMPLEMENTED` inventory；G2 真版本矩阵未完成 |
| 12 | ADP-PIP-001 | `adapters/pip_cache.py` | `IMPLEMENTED` 多解释器 inventory；无 purge |
| 13 | ADP-UV-001 | `adapters/uv_cache.py` | `IMPLEMENTED` inventory；无 prune |
| 14 | ADP-CONDA-001 | `adapters/conda.py` | `IMPLEMENTED` 分类 dry-run inventory；无 clean |
| 15 | ADP-NPM-001 | `adapters/npm.py` | `IMPLEMENTED` cache ls/量测；明确不运行 verify |
| 16 | ADP-PNPM-001 | `adapters/pnpm.py` | `IMPLEMENTED` 文件系统 inventory；不调用有写副作用的 store path/status |
| 17 | ADP-DOCKER-001 | `adapters/docker.py` | `IMPLEMENTED` 已在线 daemon inventory；不启动 Desktop、不 prune |
| 18 | ADP-OLLAMA-001 | `adapters/ollama.py` | `IMPLEMENTED` 固定回环 GET；不 serve/pull/load/delete |
| 19 | TEST-001 transcript framework | `adapters/json_contract.py`、`tests/transcripts/`、各 adapter parser 测试 | `IMPLEMENTED` 基础设施；真实支持版本覆盖仍属 G2 |
| 20 | TEST-002 filesystem fixtures | `tests/fs_integration/`、scanner/security/volume 测试 | `IMPLEMENTED` 本机安全夹具；外部 G1 矩阵未完成 |
| 21 | REL-001 release engineering | `build_release.ps1`、release/schema validators、CI、CodeQL | `IMPLEMENTED` 机械链；真实 G0 未通过 |
| 22 | DOC-001 adapter coverage/evidence | [coverage-matrix.md](coverage-matrix.md)、[adapter-support.md](adapter-support.md)、[evidence index](evidence/README.md) | `IMPLEMENTED` |

VS Code 是阶段 2 明确要求但未列入 v2 最后 22 项编号的第九个适配器；其实现与只读
边界位于 `adapters/vscode.py` 和对应 parser/文件系统测试中。

## 不可执行纵深

当前以下层次分别拒绝越权状态，任一层通过都不能替代其他层：

1. `Resource` 对 UNKNOWN/LOCAL_ONLY provenance 关闭 actionable；
2. `InventoryResult` 拒绝任意 actionable resource；
3. `StateStore` 拒绝 actionable resource、维护/破坏型 adapter-run 和非闭集 payload；
4. 当前 `Plan` 只接受精确 `REPORT_ONLY` 语义；状态读取再次复核；
5. 报告首字节前流式核对资源/run/evidence 的 SQLite 索引身份、effect class、固定回环端点和 evidence 外键；
6. 发布构建对顶层 CLI help 断言不存在 apply/execute/clean/delete 命令；
7. G2 外部 ProcMon/服务快照/三机合同仍必须独立证明没有未预期写入或服务启动。

## 永久禁区

当前源代码没有通用大文件/重复文件删除、`C:\Windows\Installer` 清理、
SoftwareDistribution/WinSxS 裸删、ResetBase、VHDX 压缩、AI 决策/规则生成、
BleachBit `--clean`、在线规则、运行时第三方插件、注册表优化或主进程提权入口。
重新讨论任何禁区必须满足 v2 的独立 ADR/证据条件；其中标为永久禁止的项目不能因
本矩阵更新而重开。

## 本地复核命令

```powershell
uv lock --check
uv pip check
uv run --frozen pytest --cov=reclaimer
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python scripts/validate_schemas.py
uv run --frozen python scripts/validate_release_artifacts.py --directory artifacts/release
```

外部门槛必须使用各 protocol 的 manifest/matrix 命令，不能把上述本地命令的绿色结果
改写为 G0/G1/G2/G5/G6 PASS。
