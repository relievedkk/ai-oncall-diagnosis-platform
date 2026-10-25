# 合成故障重放与对照实验

本演示展示诊断流程、结构化结果与证据引用。首批 12 个案例均为人工构造的合成数据，尚未进行人工语义复核；脚本重放的规则通过数不是根因准确率，也不能证明 Agent 优于固定流程。

## 安装与启动

在项目根目录使用 Python 3.11–3.13 安装项目环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app.demo --port 9911
```

macOS/Linux 将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`。也可以使用现有项目虚拟环境，无需重复安装。

浏览器打开 [本机重放演示](http://127.0.0.1:9911)。选择案例和 `Agent` 或 `Baseline` 策略，点击“开始重放”。页面展示结果状态、观测引用、假设的支持与反证、工具轨迹、缺失证据和限制。诊断结果中的 `diagnosed` 显示为“诊断结论待复核”，任务结束并不等于根因已被证明。展开原始结果可检查每次运行的字段。

启动演示不要求 `.env`、DashScope/CLS 密钥、数据库、Docker 或向量服务。不要为此复制真实业务凭据。演示不使用生产服务目录或持久任务仓储，也没有处置提交入口。

退出：在启动服务的终端按 **Ctrl+C**，再关闭浏览器页。端口占用时指定 `--port 9912` 并访问对应地址。可以用 `--host ::1` 绑定 IPv6 回环；仅支持回环 IP 字面地址，`0.0.0.0`、主机名和局域网地址会被拒绝。

## 案例涵盖的边界

案例包括 CPU 流量负载与持续占用、内存压力、下游超时、连接池耗尽、配置错误、发布回归，以及无告警、缺失日志、指标日志冲突、旧手册误导和工具依赖失败。输入中的时间、服务、实例和工具观测可检查；“已知原因”来自构造条件，未由外部运维专家标注。

输入位于 `evals/scenarios/v1/inputs.json`。私有评测期望位于独立 `expectations.json`，只供运行后评分，不能进入模型、工具输入或工作流决策。脚本模型响应保存在独立 `scripts.json`。工具响应标为 `synthetic_fixture`，只能在显式 replay/evaluation 上下文参与案例结论，不能支持生产诊断。生产降级示例数据也不能替代真实证据。

## 在终端运行对照

默认只运行 replay，比较 Agent 与固定流程（目标指标 → 目标日志 → 同一结构化总结与校验）。以下命令仅选择两个案例：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.compare_cli --case cpu_load --case missing_logs --repeats 1 --output-dir artifacts/scenario-comparison
```

省略 `--case` 会运行全部 12 例。输出目录保存每次运行 JSON 和 `summary.json`，包括模式、输入摘要、工具轨迹、调用数、结构化结果、规则评分、错误和限制。人工语义评分未完成时为 `null`；没有观测到的 usage 保持 `null`，不推算真实 Token 或费用。脚本调用数与本机耗时仅描述重放，不代表真实模型性能。

可使用 `--max-calls 24 --timeout-seconds 60` 限制单例预算，`--repeats` 为 1–10。整个运行另有总预算。中断 CLI 使用 Ctrl+C；已经写出的单次运行产物保留，未完成运行不能视为成功。

## replay、live 与真实业务试用

| 运行方式 | 实际输入与模型 | 能支持的结论 |
| --- | --- | --- |
| `replay`（默认） | 合成工具 + 脚本化模型 | 控制流、结果契约、引用合法性与展示可重复检查 |
| `live`（显式选择） | 相同合成工具 + 用户配置的真实模型 | 可记录真实 usage 与行为；语义效果仍需人工评分 |
| 真实业务试用 | 经授权的真实观测 + 真实模型 | 需独立业务验证；不属于本演示与合成案例结果 |

真实模型实验需要用户主动配置模型，并同时指定 `--mode live --allow-live`；可能产生模型费用。只有准备实际进行该实验时才执行，例如：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.compare_cli --mode live --allow-live --case cpu_load --max-calls 24 --timeout-seconds 60 --output-dir artifacts/scenario-live
```

本地 HTTP 服务始终只允许 replay；URL 参数不能开启 live。没有执行 live 时，不提供预制的性能提升或准确率百分比。两策略使用同一输入和预算；Agent 额外补查的成本如实计入，即使两者持平或 baseline 更好也应保留结果。

## 本地服务边界

HTTP 只提供内置静态文件、案例元数据和内置案例重放，不接受任意文件路径、外部 URL、执行代码或请求正文。模型与案例文本通过 `textContent` 展示，不解析为 HTML。服务校验本机 Host/Origin，不提供跨域访问。

一次只运行一个重放，重复并发请求返回 429；HTTP worker 最多 8 个，重放最多 20 秒，响应最多 1 MiB。达到超时或错误时页面明确显示失败。页面超时不能作为新运行已经结束的证据，可稍后重试。服务用于个人本机检查，无部署或远程访问入口。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest --no-cov tests/test_offline_demo.py tests/test_scenario_cases.py tests/test_scenario_comparison.py
```

测试包含真实 HTTP 重放、路径与参数拒绝、回环绑定、Host/Origin、正文限制、并发、超时，以及无凭据子进程对生产初始化导入的护栏。它们证明这些工程边界，不等同于故障语义准确率评估。
