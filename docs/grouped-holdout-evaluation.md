# 证据对账与分组留出评测

本工具补充无标签证据库存和新样本验收，不改变在线诊断 API 或严格确诊权限。默认无模型调用；本地排序不是模型准确率。

## 安装与入口

原始 RCAEval 数据、本地历史登记和留出答案不随仓库提供。以下 `.runtime/` 路径适用于已有这些本地材料的环境；首次使用需先准备上游索引及历史暴露记录，参见[材料范围](publication-guide.md)。

在项目根目录安装已有生产/开发依赖及 `requirements-rcaeval.txt`。命令统一从当前源码根目录运行：

```powershell
uv sync --frozen --extra dev
uv pip install -r requirements-rcaeval.txt
uv run python -m app.evaluation.heldout_cli --help
```

`inventory` 只接收任务目录，重新提取证据并报告库存、来源计数和缺口，不读取答案：

```powershell
uv run python -m app.evaluation.heldout_cli inventory --tasks .runtime/rcaeval/heldout-54-v1/tasks --output .runtime/rcaeval/old54-inventory-new.json
```

`not_collected` 仅描述已检查的指标 schema；不能推断日志/Trace 不含信息。没有来源证明的单位仍是 unknown。`available` 表示观测库存可用，不代表机理确诊。

## 准备独立样本

```powershell
uv run python -m app.evaluation.heldout_cli prepare --index .runtime/rcaeval/pilot/cases.parquet --history-answers .runtime/rcaeval/heldout-54-v1-sealed/answers.parquet --history-root .runtime/rcaeval --output .runtime/rcaeval/grouped-heldout-new
```

固定 Hugging Face revision 为 `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e`，索引摘要写在准备模块中。按系统、根因服务和故障类别整组排除历史实验的全部 repetition，再做时间窗口及完整遥测内容查重。固定种子选择36例，三系统各12例、六类型各6例。

准备流程是标签感知流程。它读取索引做分层，但只向 `tasks/` 发布匿名目录、遥测及摘要；映射、源路径和答案进入同级 `sealed/`。未知历史匿名目录或分层不足会停止，不能猜映射、删难例或重复抽样补足。

下载缓存在 `<output>-sealed-download/`，含真实源目录名称，禁止交给诊断进程或作为工具根目录。网络失败后保留现场；当前命令不会覆盖既有目录或自动重新抽样，修复网络后应基于同一冻结选择做本地恢复，不能改变样本。

Windows 若在最后的目录发布步骤发生 `WinError5`，先核验 `.preparing-*` 内全部文件摘要、答案承诺及 `preparation_source_identity`，再在相同父目录下做本地恢复并保留记录；不要重新抽样。具体原因未核实时，不将恢复成功解释为已经修复 OS 根因。

## 零费用预检与冻结

```powershell
uv run python -m app.evaluation.heldout_cli validate --tasks .runtime/rcaeval/grouped-heldout-new/tasks --output .runtime/rcaeval/grouped-heldout-run
```

生成 `run.json`、逐例 `*-packet.json`、`*-inventory.json` 和 `*-baseline.json`。基线不带答案和评分。检查绑定源码、锁文件、评测依赖、运行时版本、数据摘要和16 KiB包体；`run_sha256` 是后续真实请求的审核标识。

一个数据集只绑定一个运行目录。缺 Trace/无日志可以是有效任务，损坏输入/包体失败则记录 error 并阻断 live。预检不读取测试答案；源码或输入变化后不能静默沿用原运行。

发布清单同时绑定下载/解码前的源码身份；预检必须匹配。Parquet 的 schema 和嵌套字段元数据会被清理。退役状态、内容身份和请求记录存放在当前用户的 `~/.oncall-evaluation/grouped-holdout/`，不随任务目录复制；应与实验材料一起备份。执行与评分使用跨进程互斥，同一批次运行中不能解封答案。

## 后续真实模型验收（本次默认不执行）

用户明确选择冻结的批次和预算后才启动。固定 qwen-max、温度0、最多384输出 token、180秒/例、单并发、每例最多一次，共最多36次；先跑匿名顺序前三例检查协议，再续跑剩余案例。供应商模型别名不保证固定权重，费用按实际账单。

```powershell
uv run python -m app.evaluation.heldout_cli live --run .runtime/rcaeval/grouped-heldout-run --allow-live --reviewed-run-sha256 <run_sha256> --phase smoke
uv run python -m app.evaluation.heldout_cli live --run .runtime/rcaeval/grouped-heldout-run --allow-live --reviewed-run-sha256 <run_sha256> --phase full --resume
```

独立子命令、显式输入对象和无答案接口隔离诊断与评分；同一操作系统账号仍可读文件，因此不声称构成防恶意访问的安全沙箱。预测先落盘，整批终态冻结后才解封评分。错误/超时会停止后续请求，剩余案例标为 not_run；已占用但不明的请求不重发。持久化失败直接中止，恢复仅复用本地已保存结果。

```powershell
uv run python -m app.evaluation.heldout_cli score --run .runtime/rcaeval/grouped-heldout-run --answers .runtime/rcaeval/grouped-heldout-new/sealed/answers.parquet
```

评分验证源码、数据、全部预测及答案承诺摘要后才读答案。输出 `scores.json` 和 `report.md`，分别报告本地 Top1/Top5、模型提议、支持候选及严格确诊，并按系统/类型/来源分层。错误、拒答和未运行保留固定36例分母；关键比例附 Wilson 95%区间。

如果只需要零费用排序评测，必须明确以 `score --offline-only` 结束该集合的独立使用；它会解封答案并将数据集标记为 retired，不能随后把同一批样本宣称为新的独立模型验收。实际36例默认保留未评分。

这是项目内分组留出，不是第三方独立审计，也无法排除公开基准进入模型训练。RCAEval 的网络 delay/loss 类别预测与生产机理确诊须分别报告。

本次实际结果见[验收记录](evaluation/evidence-and-holdout.md)：新36例预检为35通过、1例证据包超限，真实模型验收被阻断；未产生准确率结论。
