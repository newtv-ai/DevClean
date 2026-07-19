# ADR-003: GPL 与第三方内容边界

- 状态：Accepted for Phase 0；首次公开发布前需所有者复核
- 日期：2026-07-10
- 决策所有者：DevClean maintainers

## 背景

磁盘清理生态包含不同许可证的软件、规则数据库和文档。程序代码、CleanerML/Winapp2 规则、测试夹具和厂商输出可能分别受不同条款约束。无来源复制规则会同时引入法律、供应链和安全风险。

## 决策

1. DevClean 当前采用 GPL-3.0-or-later；`LICENSE` 和构建元数据必须一致。
2. BleachBit 只作为用户另行安装的可选外部程序。仓库不分发其二进制、Python 源码或 cleaner 定义，也不在 v0.x 调用清理动作。
3. Winapp2.ini 及其衍生规则不进入仓库；如未来使用，必须先完成 CC BY-SA 4.0 的归属、ShareAlike 和发行边界评审。
4. Sifty、InstallerClean 及其他项目的代码和规则不会因许可证看似宽松而直接复制；任何引入都要记录精确版本、提交、许可证、来源 URL 和修改说明。
5. 官方文档中的命令名和事实可以作为实现依据，但大段文档文本、示例数据和第三方输出不得无审查复制进产品。
6. 每个发布物生成依赖清单/SBOM，并携带适用的许可证与 notice。依赖只通过受审查的包管理清单进入。
7. 外部工具 stdout/stderr 是运行时证据，不成为 DevClean 源代码；保存前仍需脱敏和保留来源元数据。

## 工程边界

| 对象 | Phase 0 策略 | 允许的交互 |
|---|---|---|
| BleachBit | 不 vendoring、不 import | 未来可选的版本探测、列表和 preview-only 子进程 |
| BleachBit cleaner 定义 | 不复制 | 只记录上游覆盖证据和链接 |
| Winapp2.ini | 不包含 | 无运行时读取 |
| 第三方 Python 包 | 依赖清单管理 | 锁定版本、SBOM、许可证 notice |
| 厂商 CLI/API | 用户本机外部依赖 | 版本探测、显式 argv、结构化输出优先 |
| 测试 transcript | 只保留最小脱敏夹具 | 必须附版本、平台、命令和来源说明 |

本 ADR 是工程风险边界，不构成法律意见。许可证不确定时默认不合并、不发行。

## 合并门槛

任何第三方代码、规则或二进制进入仓库前，PR 必须同时提供：

- 上游仓库与不可变 commit/tag；
- SPDX 标识和许可证全文位置；
- 文件级来源/修改说明；
- 兼容性结论及 reviewer；
- 新增的安全与回归测试；
- `THIRD_PARTY_NOTICES.md` 更新。

## 证据

- [BleachBit 仓库与 GPL-3.0 许可证](https://github.com/bleachbit/bleachbit)
- [Winapp2.ini 许可证](https://github.com/MoscaDotTo/Winapp2/blob/master/License.md)
- [SPDX License List](https://spdx.org/licenses/)

