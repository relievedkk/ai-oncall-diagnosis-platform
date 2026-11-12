# V5.1 全量评测与 V4 退役

当前诊断版本为 **V5.1 r5**，CLI 协议编号为 `5`。同一批 54 例的 r5 离线回放中，正确服务和故障类型同时排第一的有 47 例，进入前五的有 51 例，达到确诊门槛的有 8 例。r5 回放没有调用模型，实际模型命中率仍待测。本文末尾保留初版 V5.1 的真实测量记录。

## r2 修复

- 日志时间戳接受真实 Parquet nullable Int64/numpy 数值，排除布尔、非有限值及无法解析的时间戳；明确时间单位。逐例保存原始行、无效时间、窗口内外、实际处理、服务未匹配及控制文本排除计数，并验证行数对账。不可用时间或处理后零事实会阻断调用。
- 模型仅输出 `hypothesis_id`、`decision`（candidate/service_only/unknown）及简短理由。服务端从已冻结候选绑定证据；未知 ID 拒绝，unknown 不回填排名第一。模型理由不提供确诊权限。
- 服务定位与故障机理确认独立。保存模型提议、证据支持的候选、最终确诊及降级原因；`selection_joint_accuracy`、`candidate_joint_accuracy` 和 `joint_accuracy` 分开统计，拒答和错误仍在各自全量分母内。
- 改进持续内存形态、容量与连接失败、完整自身 Span 延迟及独立指标佐证的明确 errno；保留工作量变化、竞争资源与下游变慢等反例。普通超时不等于丢包，未知单位不等于资源饱和。
- 先选最多三个服务，再在这些服务内填充到最多五个服务＋类型候选；所有关键支持、反证与对照仍须装入 16 KiB 总请求预算。
- 错误保留固定错误码和脱敏字段路径，不持久化任意解析原文。历史占用数、本次新增调用、实际观察到的 tokens 分别统计；不重试、不修复重发。

## 已修复的问题

- V5.1 不再依赖 V4 模块定义开发集范围。
- 新增显式 `--evaluation-set all54`，允许评测已确认的 54 例数据。默认仍是 18 例开发集及原有付费门槛。
- 删除 V4 运行器、adaptive coordinator、专用 refinement 和专用测试；共用遥测质量测试移到 `tests/test_rcaeval_coverage.py`。`--protocol-version 4` 在读取数据或创建输出之前报错。显式 V3 历史兼容路径保留。
- 历史文档与原始评测/调用记录保留，并标明历史版本；不将其结果当作 V5.1 成绩。

## 预检查

在项目根目录运行（输出目录必须全新）：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.rcaeval --protocol-version 5 --evaluation-set all54 --mode validate --output-dir .runtime/rcaeval/v51-preflight-new
```

该模式只读取既有 18 例开发答案，重建开发审计并输出 `audit_sha256`；全量 54 例逐例重建日志、候选和证据包，不读取其余案例真值，也不调用模型。`preflight.json` 缓存绑定实际源码、原始输入和答案文件字节哈希（计算文件哈希不解析答案）。全部遥测/包体检查通过才允许真实调用；任何预检失败阻断整批真实请求。

## 全量测量

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.rcaeval --protocol-version 5 --evaluation-set all54 --mode live --allow-live --accept-low-feasibility --reviewed-audit-sha256 <预检查哈希> --preflight-file .runtime/rcaeval/v51-preflight-new/preflight.json --phase smoke --experiment-id v51-r5-all54-new --output-dir .runtime/rcaeval/v51-r5-all54-new --max-calls 1 --timeout-seconds 180
```

默认先测固定 8 例（001、012、013、014、019、024、026、032），覆盖不同源与故障表现。八例必须无执行错误、用量可观察且证据由服务端绑定，才允许将同一命令改为 `--phase full --resume` 继续其余 46 例。是否命中答案不参与协议门槛。已测八例直接复用，不再次调用。完整续跑与小样本共用同一实验清单、源数据和配置。

`--accept-low-feasibility` 是对低可辨识性仍进行测量的明确确认，写入实验配置；不修改原来的 `paid_gate_passed`，也不授予任何额外诊断资格。预检查存在错误、哈希不匹配、非固定数据目录、非 54 例清单均禁止模型调用。

每例最多一次真实请求，无自动重试。模型输入绑定 packet、schema、prompt、源码、模型配置与实验 ID。`case-XXX-prediction.json` 在读取该例真值前原子落盘，随后另存带评分的 `case-XXX-single-pass.json`。`summary.json` 每例更新，包含预期样本数和 `complete`；拒答、超时和失败都计入分母。只有 54/54 的服务和类型均正确，才能声称这批样本联合命中 100%。

中断后仅以同一命令增加 `--resume` 恢复；不要换实验 ID 绕过请求记录。已完成结果复用，失败、已占用或结果不明的请求不重发。失败的历史消耗仍保留。不能根据已看到的答案修改证据策略后拼接为同一实验。

54 例已有开发/历史 V4 暴露，整个集合不再称作独立盲测。当前测量也不等同于线上诊断 API 的准确率。模型费用以提供商账单为准。

## 旧版 V5.1 测量结果（修复前）

修复前的全量实验已完成，模型 `qwen-max`，真实使用 **V5.1初版**。54 条原始预测和 54 条评分记录齐全；运行前后应用源码指纹一致。

| 指标 | 结果 |
| --- | --- |
| 服务命中 | 2/54（3.70%） |
| 类型命中 | 2/54（3.70%） |
| 联合命中 | 2/54（3.70%） |
| 证据不足 | 17/54 |
| 结构化输出校验失败 | 30/54 |
| 证据绑定校验失败 | 5/54 |
| 请求数 / 已观测 tokens | 54 / 244,243 |

2 个联合命中均为 CPU 类型（case-019、case-046）；其余五类故障本轮均未联合命中。没有错误确诊；校验失败与拒答均计错。本轮未达到100%，工程测试通过不能当作诊断准确率。

原始实验产物保存在本地 `.runtime/rcaeval/<experiment>/`（`<experiment>` 为目录占位值），其中 `report.md` 给出逐例结果，`summary.json` 给出完整计数，`experiment.json` 给出冻结配置；SQLite 请求日志保留每例唯一占用及用量。本轮没有自动重试或替换失败结果。

完整工程验证：1,238 passed、45 skipped、0 failed。跳过项依赖 PostgreSQL、Docker 或 POSIX。独立代码复核发现的实验 ID 绑定与审计保留问题均先复现再修复；静态检查、格式检查和差异空白检查通过。

这次旧版结果暴露的输出协议、证据绑定、日志时间戳与诊断资格问题已进入 r2 修复及独立验收。工程测试通过不等于真实模型命中；r2 实测使用新实验保存，禁止覆盖上述历史记录或将这批已暴露数据称作独立盲测。
