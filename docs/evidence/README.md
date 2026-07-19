# 证据目录规范

## 目的

DevClean 的结论必须可追溯到官方文档、不可变上游源码、厂商 CLI/API 输出或本地文件系统观测。证据用于解释“为何发现、为何分级、为何未知”，不能单独授予删除权限。

本目录只保存规范和经脱敏批准进入版本库的最小 fixture。真实扫描的命令输出只在适配器解析期间以有界内存字节存在；进入证据库前必须脱敏，原始 transcript 不得写入磁盘，也不得提交 git。

## 证据等级

| 等级 | 类型 | 可支持的结论 |
|---|---|---|
| E1 | 当前厂商官方文档/API reference | 命令存在、参数语义、厂商警告 |
| E2 | 上游不可变 tag/commit 的源码与许可证 | 具体实现、版本行为、来源边界 |
| E3 | 记录工具版本与 OS build 的真实 smoke transcript | 该组合上的输出形状和副作用 |
| E4 | 本地文件系统观测 | 当前机器的路径、大小、身份和错误 |
| E5 | 二手文章、issue 或模型推断 | 仅作调查线索，不足以启用规则 |

适配器必须优先使用 E1/E2；E3 用于解析器和兼容性，E4 用于本次报告。E5 不能使对象从 deferred/protected 升级。

## 本地目录建议

```text
<data-dir>/evidence/<scan-id>/
  manifest.json
  commands/
    <evidence-id>.stdout.redacted.txt
    <evidence-id>.stderr.redacted.txt
    <evidence-id>.meta.json
  loopback/
    <evidence-id>.response.redacted.txt
    <evidence-id>.meta.json
  filesystem/
    observations.jsonl
  reports/
    scan-report.json
```

解析器消费进程返回的原始有界内存结果，证据持久化走独立边界。v1 只接受严格 UTF-8 且不含危险控制/格式字符的文本，然后执行用户名、凭据形状、认证头、已知 token、邮件地址、远程定位符和所有 URL 的保守脱敏。非 UTF-8、危险文本、超出脱敏边界或脱敏器异常时，只写入固定 marker；marker 仅包含原因、原始字节数和原始 SHA-256，不包含任何原始片段。报告只引用 evidence ID 和摘要，不内嵌 transcript。

## 最小 metadata

每项证据至少记录：

```json
{
  "evidence_id": "evidence_<opaque-id>",
  "scan_id": "scan_<opaque-id>",
  "adapter_id": "huggingface",
  "kind": "VENDOR_CLI",
  "captured_at": "2026-07-10T08:00:00Z",
  "executable_path": "C:\\<REDACTED_PATH>",
  "executable_size": 123456,
  "executable_mtime_ns": 1770000000000000000,
  "executable_volume_serial": "1a2b3c4d",
  "executable_file_id": "00112233445566778899aabbccddeeff",
  "executable_file_id_kind": "file_id_128",
  "executable_sha256": "<64 lowercase hex>",
  "argv_redacted": ["C:\\<REDACTED_PATH>", "cache", "ls", "--revisions", "--format", "json"],
  "effect_class": "PURE_QUERY",
  "returncode": 0,
  "timed_out": false,
  "output_limit_exceeded": false,
  "transcript_redaction_version": "transcript-redaction-v1",
  "stdout_storage": "REDACTED_UTF8",
  "stdout_size": 1234,
  "stdout_sha256": "<original in-memory bytes: 64 lowercase hex>",
  "stdout_stored_size": 987,
  "stdout_stored_sha256": "<persisted redacted bytes: 64 lowercase hex>"
}
```

工具版本、probe 结果和副作用分类同时进入同一扫描的 `adapter_runs`；操作系统 build
由扫描/doctor smoke 记录。Loopback evidence 使用固定 host/port/method/endpoint 的封闭字段，
并以 `response_*` / `response_stored_*` 表达来源与落盘哈希，不保存自由 URL。

不得记录环境变量全集、访问令牌、代理凭据、registry auth、私有 URL 查询参数或未经脱敏的命令行。

## Transcript fixture 进入 git 的门槛

1. 内容已经最小化，用户名、机器名、路径、repo 名、digest 之外的敏感标识已替换；
2. 邻近 `.meta.json` 写明工具版本、OS build、原始来源和脱敏方法；
3. fixture 仅覆盖解析所需字段，不保存完整用户目录树；
4. SHA-256 针对脱敏后的实际 fixture 计算；
5. reviewer 确认许可证/引用允许保留该最小输出；
6. parser 必须同时测试未知字段、字段缺失、超长输出和非零退出。

## 哈希与链路

- `stdout_size` / `stderr_size` 与 `stdout_sha256` / `stderr_sha256` 始终标识进程返回的原始有界内存字节；它们用于来源身份绑定，但对应原始字节不落盘；
- 每次外部命令在启动前捕获 executable SHA-256 与卷/文件 ID，命令完成后强制重新哈希和复核；身份或内容变化时该观测失败，不能发布成功 evidence；
- `stdout_stored_size` / `stderr_stored_size` 与 `stdout_stored_sha256` / `stderr_stored_sha256` 始终标识实际落盘的脱敏 UTF-8 或 marker 字节；审计文件完整性必须校验这组字段；
- `stdout_storage` / `stderr_storage` 明确区分 `REDACTED_UTF8`、非 UTF-8 marker、危险文本 marker、超限 marker 与脱敏异常 marker，不能把 marker 误当原始输出；
- 资源派生证据需要绑定厂商原始响应时使用来源哈希；本地 evidence 文件校验使用 stored 哈希，两类哈希不得混用；
- `Resource.evidence_refs` 只能引用同一报告中存在的 evidence ID；该引用完整性由运行时校验和测试保证，JSON Schema 本身不能表达跨数组外键；
- 派生摘要记录输入 evidence ID 和解析器版本，不能覆盖原始证据；
- 截断输出仍可作为失败证据，但不得生成成功 inventory。

## 脱敏

默认展示使用 `<USER>`、`<REDACTED_TOKEN>`、`<REDACTED_URL>` 等稳定占位符。命令 argv 中的绝对路径在写 metadata 前一律降为广义路径占位符；不能用“报告导出时再脱敏”替代证据入库前脱敏。内部来源 hash 不因脱敏失去身份绑定，但原始字节永不作为恢复通道。无法可靠脱敏的 transcript 只保存 marker，不保存、导出或提交原文。

## 保留与删除

本地证据可能包含敏感资产清单，默认采用最短可用保留期。清理 DevClean 自己的证据属于独立的应用数据管理功能，不能混入扫描目标清理，也不能绕过用户明确选择。

## 外部门槛资产

- [G0 repository/license/CI](G0-release-readiness-protocol.md)
- [G1 physical filesystem boundaries](G1-physical-boundary-protocol.md)
- [G2 ProcMon and 2-physical + 1-VM matrix](G2-procmon-smoke-protocol.md)
- [G5 direct-FS race/review contract](G5-direct-fs-race-protocol.md)
- [G6 signed broker install subset](gates/G6-broker-verification.md)

`scripts/validate_gate_evidence.py` 只接受闭集 G0/G1/G2/G5 manifest，并在首个字节输出前
复核普通文件边界、字节数、SHA-256、交叉引用、产品/前置门槛 binding 和各类证据语义。
模板均故意为 `INCOMPLETE`；合成测试只能证明验证器的正反路径，不能替代真机、GitHub、
项目 owner、签名证书或独立 reviewer。
