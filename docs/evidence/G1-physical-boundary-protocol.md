# G1 真机文件系统边界验收协议

## 目的与判定边界

本协议把 G1 中不能仅靠普通单元测试证明的 Windows 行为转成可复核证据。它只验收扫描与报告，不授权任何清理、删除、移动、属性修改、提权或服务控制能力。最终 G1 至少需要一台 Windows 11 x64 真机；VM 预演不能替代真机结论。

完整门槛包含以下 13 个必选检查，名称必须与清单模板一致：

- `symlink_not_descended`
- `junction_not_descended`
- `mount_point_not_descended`
- `junction_loop_bounded`
- `onedrive_placeholder_not_hydrated`
- `unc_no_execution_candidate`
- `network_volume_no_execution_candidate`
- `removable_volume_no_execution_candidate`
- `refs_volume_no_execution_candidate`
- `million_resource_streaming`
- `cancellation_under_two_seconds`
- `access_denied_no_uac`
- `inventory_only_no_delete`

任一必选检查为 `SKIP` 或证据不足时，清单必须保持 `INCOMPLETE`；不得用说明文字把跳过项改写成通过。

## 安全准备

1. 构建一个固定 wheel/source archive，记录 SHA-256 和源码 revision；整个验收期间不重新构建。最终清单的 `source_revision` 必须是完整的 40 或 64 位小写 Git object ID，分支名、标签、缩写 SHA 和 `WORKTREE_UNCOMMITTED` 都不能作为门槛证据。
2. 使用标准完整性、非管理员测试账户运行 DevClean。若创建 mount point、junction、拒绝访问 ACL 需要管理员，只能在夹具准备阶段使用独立管理员终端；准备完立即退出，记录准备过程，DevClean 本身不得触发 UAC。
3. 只使用空的专用测试目录、专用测试共享、可丢弃 U 盘/虚拟测试卷和独立 OneDrive 测试文件。禁止把真实仓库、密钥、个人 OneDrive 文件或系统卷目录当夹具。
4. 在批准根外、每个边界目标内和批准根相邻目录预埋随机命名金丝雀。普通夹具记录内容 SHA-256、卷序列号和 file ID；OneDrive 离线占位文件不得在扫描后读取内容。
5. 将 [G1 清单模板](templates/g1-physical-manifest.template.json)复制到本机私有证据目录。机器标识只保存带项目盐的 SHA-256，不保存 `MachineGuid`、主机名或用户名原文。

## 夹具矩阵

| 夹具 | 准备要求 | 必须观察到的结果 |
|---|---|---|
| 文件 symlink | 链接指向含随机金丝雀的外部目录 | 链接本身报告为边界；目标文件不出现在扫描记录 |
| directory junction | 指向批准根外专用目标 | `REPARSE_POINT` 边界；不下钻 |
| mount point | 只挂载可丢弃测试卷/VHD，不操作真实分区 | mount point 为边界；另卷对象不下钻 |
| junction loop | 两个专用目录构成闭环 | 有界结束，无重复无限遍历、栈溢出或内存增长 |
| OneDrive Files On-Demand | 独立云测试文件，先确认处于未下载/离线占位状态 | 扫描前后 Cloud Files/reparse/recall 属性与 allocation 不显示 hydration；不得读内容或算内容哈希 |
| UNC 与映射网络盘 | 专用测试共享 | 可报告边界/不可访问；`actionable=false`，无执行候选 |
| removable volume | 可丢弃介质 | 可报告边界；`actionable=false`，无执行候选 |
| ReFS volume | 专用可丢弃 ReFS 测试卷，不复用系统/用户数据卷 | 只读盘点或明确边界；所有资源 `actionable=false`，无 direct-FS/厂商执行候选 |
| access denied | 专用子目录对标准测试账户拒绝列举 | 只生成结构化错误，进程保持标准完整性且无 UAC |

mount point 和 ACL 夹具的管理员准备记录属于证据，但不得把管理员终端中的 DevClean 运行结果计入 G1。

## 执行步骤

1. 在标准用户终端记录 OS product/version/build、locale、CPU 架构、各测试卷 filesystem/drive type 和 DevClean artifact SHA-256。
2. 对夹具运行现有文件系统集成测试，并把退出码、开始/结束时间、命令 argv、测试条目和结果保存成最小 `TEST_REPORT_JSON`。推荐先执行：

   ```powershell
   uv run --frozen pytest -q tests/fs_integration tests/test_scanner.py tests/test_scanner_resources.py tests/test_volumes.py
   ```

   `TEST_REPORT_JSON` 必须绑定同一 `artifact_sha256`/`source_revision`，使用 argv 数组、
   `exit_code=0`，并为其所支持的每个 manifest check 保存唯一的
   `{check_id,status=PASS,duration_ms,detail}`；一份只有“tests passed”文本的文件不能通过验证。

3. 用发布候选的 `scan`/`report` 命令逐一扫描专用根。保存脱敏报告，并生成 `BOUNDARY_OBSERVATION_JSON`：每项只记录夹具标签、边界原因、是否下钻、候选数、错误类型和相关报告 SHA-256，不保存真实路径。
4. 运行百万资源基准，保存 stdout JSON 为 `BENCHMARK_JSON`；不得用百万个真实文件代替状态存储基准：

   ```powershell
   uv run --frozen python scripts/benchmark_streaming_state.py --count 1000000 --batch-size 512 `
     --work-dir <dedicated-local-fixed-NTFS-directory> `
     --artifact <release-wheel> --source-revision <git-revision>
   ```

   `--work-dir` 必须位于本机固定 NTFS 卷；不要因仓库恰好位于网络盘、可移动盘或慢速备份盘
   就把数据库建在那里。脚本只在该目录下创建并回收自己的随机临时子目录。结果必须
   `stored=1000000`、`integrity=true`，并由 reviewer 结合工作集/`tracemalloc` 峰值确认
   没有随行数线性持有 Python 列表。
5. 对慢元数据夹具发出取消，记录从取消请求到“停止新元数据调用”的单调时钟差；必须小于 2 秒，并验证状态库完成安全收尾。
6. 比对所有普通金丝雀的内容哈希和 file ID。对 OneDrive 占位文件只比较扫描前后 attributes、reparse tag、EOF、allocation、last-write/change time 和同步客户端状态；任何需要打开文件内容的“验证”本身会使 hydration 结论无效。
7. 检查所有资源 `actionable=false`，报告 `safety_boundary.executable=false`，CLI 不存在 `apply`/通用删除入口。全盘结果只能提供报告、打开位置或忽略语义。
8. 为 artifact、测试报告、边界观测、基准结果计算字节数与 SHA-256，填入 G1 manifest；执行：

   ```powershell
   uv run --frozen python scripts/validate_gate_evidence.py <g1-manifest.json> --matrix G1
   ```

## `BOUNDARY_OBSERVATION_JSON` 最小形状

```json
{
  "schema_version": "1.0.0",
  "captured_at": "2026-07-11T00:00:00Z",
  "artifact_sha256": "<64 lowercase hex>",
  "source_revision": "<fixed revision>",
  "observations": [
    {
      "check_id": "junction_not_descended",
      "fixture_label": "junction_external_canary",
      "boundary_reason": "REPARSE_POINT",
      "descended": false,
      "hydrated": false,
      "actionable_resources": 0,
      "before_identity_digest": "<64 lowercase hex>",
      "after_identity_digest": "<64 lowercase hex>"
    }
  ]
}
```

这是证据记录格式，不是产品导入格式；DevClean 不得从该文件生成任何动作。

## 失败即停条件

- 占位文件出现 hydration/下载迹象；
- 边界目标中的文件出现在扫描结果；
- 标准用户运行触发 UAC 或高完整性子进程；
- UNC、网络、可移动卷或任意全盘结果出现可执行候选；
- 金丝雀内容、file ID、属性或时间戳发生无法解释的变化；
- 取消后 2 秒仍有新的遍历/元数据调用；
- manifest 校验器报告路径逃逸、哈希不一致、缺少证据或占位值。

发生以上任一项时结论为 `FAIL`，保留最小复现证据；不得继续到任何执行阶段。
