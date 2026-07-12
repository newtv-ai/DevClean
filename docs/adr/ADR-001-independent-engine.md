# ADR-001: 独立适配器引擎与 BleachBit 预览边界

- 状态：Accepted
- 日期：2026-07-10
- 决策所有者：Reclaimer maintainers

## 背景

Reclaimer 面向 Windows 开发与 AI 工作站，首要任务是生成可解释、可追溯的磁盘占用清单。固定路径清理器与开发工具官方 CLI 的能力模型不同：前者通常是“规则匹配后删除文件”，后者还涉及资源引用、版本、守护进程、共享内容寻址存储和厂商维护事务。

BleachBit 是成熟的通用清理工具，但其进程、规则执行模型和提权边界不等同于 Reclaimer 所需的 `probe → inventory → plan → preflight → execute → verify` 生命周期。直接 fork 或 import 会在尚未验证需求前扩大回归面，也会模糊许可证和安全责任。

## 决策

1. Reclaimer 是独立实现，不 fork、不 import、不复制 BleachBit 源码或内置 cleaner 定义。
2. 当前 Windows GUI 的确定性 TEMP 清理、受限 AI 审查和用户回收站路径均由 Reclaimer 自己的扫描快照、句柄复核与保护规则控制；它们不依赖 BleachBit。
3. 若后续接入 BleachBit，只允许把用户另行安装的 BleachBit 作为可选外部证据源，并且仅调用 `--list` 或 `--preview` 等经目标版本实机确认的只读接口。
4. Reclaimer 永不把 BleachBit preview 输出直接升级为执行计划；输出按不可信外部数据处理，解析失败即降级为“不可用”。
5. v0.x 不调用 `--clean`，不代替用户启动提权 BleachBit，也不安装、升级或下载 BleachBit。
6. 固定路径规则与官方 CLI/API 适配器都属于 Reclaimer 自己的显式注册表；运行时不反射加载第三方 Python 插件。

## 理由

- 把通用清理器与资源感知适配器隔离，能够独立测试权限、错误处理和证据链。
- preview-only 边界保留了成熟规则库的可见性，同时不会让外部工具越过 Reclaimer 的执行控制面。
- 独立引擎可以把 GUI 的文件身份复核、AI 范围约束与未来工具专用适配器置于同一安全模型中，而不继承上游清理器的隐式删除语义。

## 后果

- Reclaimer 需要实现自己的资源模型、扫描器、证据格式和适配器协议。
- BleachBit 的“可预览”不代表 Reclaimer 支持或认可相应清理项。
- 用户可能同时看到 BleachBit 与 Reclaimer 两份报告；报告必须标注来源和口径。
- 任何未来的 BleachBit 执行集成都需要新 ADR、安全测试和用户明确批准。

## 重新评估条件

满足以下任一条件时可提出新 ADR，但本 ADR 在新决策合并前仍有效：

- 产品必须提供单一 GUI 且无法通过进程边界实现；
- 经验证的需求必须修改 BleachBit 核心预览语义；
- 上游提供稳定、结构化、无副作用的正式集成 API；
- 项目许可证或发行方式发生变化。

## 证据

- [BleachBit 6.0.2 发布说明](https://www.bleachbit.org/news/bleachbit-602)
- [BleachBit 命令行文档](https://docs.bleachbit.org/doc/command-line-interface.html)
