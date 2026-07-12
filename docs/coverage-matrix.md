# Phase 0 覆盖矩阵

## 目的

本矩阵是范围控制与证据索引，不是“可删除清单”。只有 `ADAPTER_INVENTORY` 和 `REPORT_ONLY` 会进入 v0.1；所有执行能力仍未实现。状态含义如下：

- `ADAPTER_INVENTORY`：通过受支持的厂商接口或只读扫描生成结构化资源；
- `REPORT_ONLY`：只报告占用，不能生成动作；
- `PROTECTED`：硬编码保护，不进入候选集；
- `FORBIDDEN`：v0.1–v0.3 明确禁止；
- `DEFERRED`：证据或安全设计不足，后续 ADR 决定；
- `UPSTREAM_PREVIEW`：未来仅可引用外部 BleachBit preview 证据。

## 覆盖表

| 对象 | 语义类型 | 一手发现接口/证据 | BleachBit 6.0.2 证据 | v0.1 | v0.2–v0.3 方向 | 状态 |
|---|---|---|---|---|---|---|
| Hugging Face Hub repo/revision cache | `INSTALLED_MODEL` / `PACKAGE_STORE` | `hf cache ls --revisions --format json` | 无需依赖 | 版本门控后 inventory | detached revision 可走官方 dry-run；指定 revision 黄档 | `ADAPTER_INVENTORY` |
| Hugging Face Xet cache | `REBUILDABLE_CACHE` | `HF_XET_CACHE`；官方说明允许整目录移除 | 未核验 | 目录量测，exclusive unknown | 进程关闭后的独立固定动作 | `ADAPTER_INVENTORY` |
| HF downstream assets cache | `APP_STATE` / 未知 | `HF_ASSETS_CACHE` | 未核验 | 只报告 | 没有通用清理动作 | `REPORT_ONLY` |
| Ollama models | `INSTALLED_MODEL` | 已运行服务的 `/api/version`、`/api/tags`、`/api/ps` | 无需依赖 | 不启动服务；在线时 inventory | 逐模型黄档；自建模型可能不可恢复 | `ADAPTER_INVENTORY` |
| pip wheel/HTTP cache | `REBUILDABLE_CACHE` | `python -m pip cache dir/info/list` | 无需依赖 | 根目录去重后量测 | `purge` 无 dry-run，只能整缓存显式动作 | `ADAPTER_INVENTORY` |
| uv cache | `REBUILDABLE_CACHE` | `uv cache dir`、支持时 `uv cache size` | 无需依赖 | inventory | `prune` 无 dry-run；禁止 `--force` | `ADAPTER_INVENTORY` |
| Conda index/tarball/log cache | `REBUILDABLE_CACHE` | `conda clean <category> --dry-run --json` | 无需依赖 | 按类别 inventory | 绿档官方动作 | `ADAPTER_INVENTORY` |
| Conda package cache | `PACKAGE_STORE` | `conda clean --packages --dry-run --json` | 无需依赖 | 黄档报告 | 显式确认；永久禁用 `--force-pkgs-dirs` | `ADAPTER_INVENTORY` |
| npm cache | `REBUILDABLE_CACHE` | `npm config get cache`、`npm cache ls` | 无需依赖 | inventory；禁止 verify | verify 是执行型 GC；完整 clean 需 `--force` | `ADAPTER_INVENTORY` |
| pnpm store | `PACKAGE_STORE` | 当前仅按用户级约定的 v10/v11 store 根做元数据扫描；`store path/status` 的写副作用已登记但默认不调用 | 无需依赖 | 安全扫描总量；reclaimable unknown | `store prune` 黄档策略 GC，另做版本门控 | `ADAPTER_INVENTORY` |
| Docker images/build cache | `INSTALLED_MODEL` / `BUILD_OUTPUT` | 在线 daemon 的 `docker system df --format json` 与 Engine API | 无需依赖 | 不启动 Docker Desktop | 只考虑有年龄过滤的 builder/dangling prune；不碰 volume | `ADAPTER_INVENTORY` |
| VS Code extensions | `APP_STATE` | 官方扩展根 + `package.json` 只读解析；禁用 `code --list-extensions` | 发布说明称已有 VS Code cleaner；本机枚举待做 | 当前目录清单；疑似旧目录只报告 | v0.1–v0.3 不删扩展目录 | `REPORT_ONLY` |
| VS Code Chromium caches | `REBUILDABLE_CACHE` / `APP_STATE` | 无官方清理 API | 发布说明称覆盖 VS Code；具体项待 `--list`/preview 核验 | 只量测且标 heuristic | 仅可能由未来 upstream preview 展示 | `UPSTREAM_PREVIEW` |
| Cursor/Windsurf/VSCodium | `APP_STATE` | 必须各自核验 CLI 与数据根 | BleachBit 6.0.2 发布说明称覆盖部分产品 | 不复用 VS Code 假设 | 分产品证据齐备后再接 | `DEFERRED` |
| TorchInductor 等固定构建缓存 | `BUILD_OUTPUT` | 工具版本与进程状态尚待证据矩阵 | 上游具体覆盖待枚举 | 只读扫描可报告 | 固定规则必须另有来源、版本与金丝雀测试 | `DEFERRED` |
| Windows Component Store | `SYSTEM_ROLLBACK` | 提权 `DISM /AnalyzeComponentStore` | 不适用 | OS 能力探测 + 用户自行执行的 REPORT_ONLY 指引 | v0.5 签名 broker；永不 ResetBase | `REPORT_ONLY` |
| Delivery Optimization cache | `REBUILDABLE_CACHE` | Windows DeliveryOptimization cmdlet | 不适用 | 不承诺完整大小 | v0.5 固定 broker action；不含 pinned files | `DEFERRED` |
| `C:\Windows\Installer` | `SYSTEM_ROLLBACK` | 微软明确不可手工删除 | 不适用 | 不扫描为垃圾 | v0.1–v0.3 无动作 | `FORBIDDEN` |
| `SoftwareDistribution`、裸 WinSxS 文件 | `SYSTEM_ROLLBACK` | 仅允许微软维护接口 | 不适用 | 不扫描为候选 | 禁止文件级删除 | `FORBIDDEN` |
| WSL/Docker VHD/VHDX | `SYSTEM_ROLLBACK` / 容器资产 | 需要挂载状态、磁盘类型和宿主空间模型 | 不适用 | 只可报告文件占用 | v0.4+ 新 ADR | `FORBIDDEN` |
| JetBrains Local History | `USER_DATA` | 用户恢复资产 | 不适用 | 硬编码保护 | 永无通用删除动作 | `PROTECTED` |
| `.codex`、`.claude`、编辑器 globalStorage | `USER_DATA` / `APP_STATE` | 没有稳定官方可删除子路径 | 上游发布说明不构成数据安全证明 | 整根保护 | 仅官方、版本化证据可新开规则 | `PROTECTED` |
| `.git`、lockfile、env/key 文件 | `USER_DATA` | 项目资产 | 不适用 | 硬编码保护 | 无动作 | `PROTECTED` |

## BleachBit 覆盖核验方法

发布说明只能证明产品名称被提及，不能证明具体 cleaner option、路径与当前机器一致。未来进行本机覆盖核验时必须：

1. 记录 BleachBit 精确版本，最低不得低于 6.0.2；
2. 运行经该版本 `--help` 确认的 list/preview 命令；
3. 保存脱敏 transcript 和 SHA-256；
4. 将每个 option 标为“上游已有 / Reclaimer adapter / protected / forbidden”；
5. 不调用 clean，不从 preview 自动生成执行计划。

来源：[BleachBit 6.0.2 发布说明](https://www.bleachbit.org/news/bleachbit-602)。

## 完成定义

- 每个新增对象先进入本矩阵，且具有唯一状态；
- `PROTECTED`/`FORBIDDEN` 对象不能同时出现在可选动作范围；
- 没有一手证据的具体路径不得从 `DEFERRED` 升级；
- adapter 的实际命令、输出和副作用以 [adapter-support.md](adapter-support.md) 为准。
