# G5 DIRECT_FS_ACTION 竞态与独立审查验收协议

## 当前状态与授权边界

本文件最初是未来 v0.4 验收规范；当前 ADR-004 已另行授权受控句柄执行器。本文仍不授权新增旁路：回收站桥、`DeleteFileW`、`shutil.rmtree`、Shell 删除或未经 ADR/测试的 `apply` 路径都属于拒绝项；允许的 rename/`FileDispositionInfo` 仅限 `platform/windows/exact_cleanup.py` 的精确隔离后流程。

只有以下前置条件全部具备后，才可另行批准编写专用测试执行器：

1. G2 三机外部矩阵已经由机器证据通过；
2. 阶段 3/4 的计划、意图日志、reconcile/verify 和各自门槛已经通过；
3. 首个批准根有一手证据证明是可再生 cache、无更安全官方 API、普通权限可处理；
4. 测试只在 disposable VM/专用固定 NTFS 测试卷运行，并有快照或物理隔离；
5. 执行模块的作者与最终 reviewer 身份明确。

当前可安全完成的工作仅限：保留本协议、[G5 清单模板](templates/g5-race-manifest.template.json)和 schema/验证器。模板必须保持 `INCOMPLETE`。
未来清单的 `prerequisites` 必须分别引用同一 product binding 的 G2、G3、G4
`GATE_RESULT_JSON` clean PASS；缺一项时验证器拒绝 G5 PASS。竞态只能在
`machine_kind=DISPOSABLE_VM` 的固定 NTFS 测试卷运行。
整组 product binding 中的 `source_revision` 必须是完整的 40 或 64 位小写 Git
object ID；分支名、标签、缩写 SHA 和未提交工作树标记均不能进入 G5 证据。

## 被测执行器不变量

未来实现必须在竞态测试前通过静态审查：

- 计划逐文件列举，执行时绝不递归发现并处理新对象；
- 批准根和每级目录句柄从 preflight 保持至动作结束；
- 目标以 `DELETE | FILE_READ_ATTRIBUTES` 打开，目录使用 backup semantics，对象使用 `OPEN_REPARSE_POINT`；
- 默认不共享 WRITE/DELETE；锁冲突只 `skip`；
- volume serial、128-bit file ID、attributes、reparse tag、EOF/allocation/timestamps、最终 volume GUID path 与计划一致；
- 身份不一致只减少动作数量；没有 fallback delete；
- 只使用普通 `FileDispositionInfo`，不使用 POSIX delete、不清只读位、不强删锁定/映射文件；
- 目录仅在所有已计划子项完成且句柄证明为空时处理；
- 最终路径再经过版本化 deny-list：`.git`、lockfile、`.env*`、`*.key`/`*.pem`、VS Code `globalStorage`、JetBrains Local History、`.codex`、`.claude` 命中即 skip + 审计；该层不得放松批准根和句柄身份校验。

静态审查必须搜索并解释所有 filesystem mutation 调用。出现 `shutil.rmtree`、`os.remove/unlink/rmdir`、`DeleteFileW`、shell/cmd/PowerShell 删除命令或字符串路径 fallback 时，`no_fallback_delete=FAIL`。

## 随机金丝雀

每轮测试用 CSPRNG 生成不可预测名称和内容，至少覆盖：

- 批准根外的同级、父级和另一固定 NTFS 卷；
- junction/mount target；
- 已计划对象相邻但未进入计划的新文件；
- `.git`、多种 lockfile、`.env.<random>`、PEM/key、`globalStorage`、Local History、`.codex`、`.claude`；
- 多硬链接对象，其中至少一个链接永远在动作集合外；
- 同名但不同 file ID 的 replacement。

每轮开始记录 content SHA-256、volume serial、128-bit file ID、link count、attributes、EOF、allocation 和关键时间；结束后重新打开句柄复核。保护区任一对象缺失、内容变化、身份被替换或无法复核，都视为失败而不是“测试噪声”。

## 七类 10,000 次竞态循环

每类使用独立随机种子和确定性 barrier，在“计划已持久化、目标句柄尚未取得”“身份读取后、disposition 前”等关键窗口驱动竞争线程。每类至少 10,000 次成功触发竞争窗口；仅循环次数达到 10,000 而没有实际命中 barrier 不算通过。

| check ID | 竞争动作 | 必须结果 |
|---|---|---|
| `junction_swap_race` | 将已计划目录项在普通目录与指向保护区的 junction 间切换 | 句柄/reparse/volume 不一致即 skip；junction target 零变化 |
| `rename_race` | 计划后交换父目录或目标名称 | 通过固定目录句柄解析；身份/最终路径变化即 skip |
| `file_id_replacement_race` | 删除夹具对象并以同名新对象替换 | 新 file ID 永不处理；记录 mismatch |
| `locked_file_race` | 竞争者持有不共享 DELETE/WRITE 的句柄 | 只记录 lock skip，不改共享模式、不重试 fallback |
| `read_only_file_race` | 在 preflight 后设置只读属性 | 不清属性、不强制处理；skip |
| `hardlink_race` | 在计划外创建/保留硬链接或改变 link set | 独占释放量保持 unknown/0；外部链接内容和 ID 不变 |
| `new_file_insertion_race` | 计划完成后向目录插入随机文件 | 新文件不进入动作；目录非空时不处理目录 |

每轮还必须断言：任一解析、ACL、identity、reparse、lock 或 metadata 错误只让 action count 下降；不能扩大批准根或回退到字符串路径删除。

## 证据报告

`RACE_REPORT_JSON` 使用闭集 1.0.0 合同并绑定 artifact/revision。七个唯一 race 项分别包含
`attempts`、`barrier_hits`、`safe_skips`、identity/lock skips、seed commitment SHA-256、
金丝雀 before/after digest、scope expansion、unexpected mutation、耗时和结果；
`attempts/barrier_hits/safe_skips` 均不得少于 10,000，manifest `iterations` 必须等于实际
barrier hits，所有 expansion/mutation 为 0。

`CANARY_ATTESTATION_JSON` 单独保存 manifest 中每个 protected label 的对象数、聚合
Merkle/digest before/after、file-ID 全量复核和 unexpected mutation；不把真实路径或
金丝雀内容写入版本库。原始详细证据保留在隔离环境。

`STATIC_AUDIT_REPORT` 是结构化 JSON，必须覆盖所有 mutation API、批准根句柄生命周期、
fail-closed 分支、deny-list 版本/九类最小模式，并对 `shutil.rmtree`、`os.remove/unlink/rmdir`、
`DeleteFileW`、shell delete 和字符串路径 fallback 分别记录 0 命中。
`REVIEW_ATTESTATION` 记录非作者 reviewer、审查方法、revision/artifact SHA-256、结论和
`open_findings=[]`，且必须与 manifest review 字段逐字一致。

G5 manifest 中七类 race check 的 `iterations` 都必须至少为 `10000`，并直接引用 `RACE_REPORT_JSON`。完成后运行：

```powershell
uv run --frozen python scripts/validate_gate_evidence.py <g5-manifest.json> --matrix G5
```

## 独立审查与停止条件

至少一名非作者 reviewer 完成独立审查；可为人类或明确记录的多模型交叉审计，但 reviewer 不能是实现该模块的同一主体。author/reviewer ID 只能用非个人化稳定代号，且集合必须不相交。

以下任一项永久阻断 G5：

- 保护区出现一次误删/误改；
- 身份不一致仍继续 mutation；
- 出现 fallback delete、路径字符串递归或 shell；
- 非本地固定 NTFS 被接受；
- 任一 race 少于 10,000 次实际 barrier hit；
- 无独立 reviewer、审查 revision 与被测 artifact 不同；
- 为了“凑首个动作”放宽批准根或 cache 准入条件。

若没有满足准入条件的真实 cache 根，可以只交付未来执行器夹具和失败证据，不能为满足版本号开放动作。
