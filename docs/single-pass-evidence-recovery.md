# 单阶段证据修复：推进与实测记录

本轮按已批准顺序推进：核实 CPU 语义、18 例失败归因、候选与包体修复、零费用验收。保持一次请求额度及未知结果不重发，未访问剩余36例答案，未运行真实模型。

## CPU 采集语义核查

原始3例均有持续 CPU 上升，正确 CPU 候选已经进入 Top5。指标单位与类型为 unknown 是原资格检查的唯一缺口。官方论文第3.2节将 CPU 故障的指标明确为容器 CPU 使用量；公开读取代码没有提供对应 CPU 查询式、物理单位或 CPU quota 分母。因此本轮只增加有来源的 `quantity=cpu_usage` 和 `relative_change_only`，保留 `unit=unknown`、`metric_kind=unknown`，不计算 CPU 饱和度或百分比。

这个档案只适用于已明确标识的 RCAEval RE2 输入；普通输入不自动套用。`*_cpu_seconds_total` 等其他名称不会继承该语义；counter 仍须显式声明。负载增长、重试/GC、资源共变和独立响应对照仍参与资格检查。新增测试同时修复了 unknown counter 单位派生为 `unknown/second` 后被误当成已知单位的漏洞。

| 开发案例 | 原资格 | 接入已核实的指标含义后 | 单位 |
| --- | --- | --- | --- |
| case-013 | 不满足 | 满足当前对照规则 | unknown |
| case-022 | 不满足 | 满足当前对照规则 | unknown |
| case-054 | 不满足 | 满足当前对照规则 | unknown |

这是程序证据资格的变化，真实模型准确率尚未测量，仍需逐例核对因果判断。

默认 v5 CLI 对 RCAEval RE2 使用 `--telemetry-profile rcaeval-re2`；其他来源使用 `--telemetry-profile unknown`。档案传入基础、全部时间扰动、validate 和 live 证据路径，并进入包体/配置指纹。

## 权威来源与适用边界

- [RCAEval 原论文第3.2节](https://arxiv.org/html/2412.17015v1#S3.SS2)：发布的 CPU 使用量指标含义；未给出 CPU 查询式和单位。
- [固定版本的内存换算代码](https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/RCAEval/io/time_series.py)：`*_mem` 除以1e6转换为 MB；保留原始 bytes，具体内存统计口径仍未知。
- [维护者对 dashboard p90 的解释](https://github.com/phamquiluan/RCAEval/issues/80#issuecomment-5870300859)：仅 `ts-ui-dashboard_latency-90` 可核实为秒、尾随60秒、1秒评估/15秒抓取、包含下游耗时；不推广到所有服务或 CPU 指标。
- [固定版本的读取器](https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/RCAEval/utility/__init__.py)：保持原始观测，缺失保留 NaN，不自动补齐。

只读研究缓存位于 `.runtime/rcaeval/source-audit-20261103`；下载的代码未执行。未下载新样本、未获取全量案例索引或盲测答案。
