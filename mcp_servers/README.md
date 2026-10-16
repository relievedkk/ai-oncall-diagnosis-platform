# MCP Servers

为 AIOps Agent 提供日志和真实 Prometheus 指标查询工具。

## 服务

### CLS

默认使用官方 `cls-mcp-server`，由 `start-windows.bat` 在 `3000` 端口启动，读取 `.env` 中的腾讯云凭据。未配置 Node.js 或凭据时，启动脚本会回退到本地 `cls_server.py` 演示服务。

### Monitor

`monitor_server.py` 在 `8004` 端口提供：

- `query_service_metric`：查询 `cpu`、`memory`、`error_rate`、`latency_p95` 或 `up`。
- `query_cpu_metrics`：CPU 查询快捷工具。
- `query_memory_metrics`：常驻内存查询快捷工具。

所有 Monitor 结果均来自 Prometheus `query_range` API，并返回实际 PromQL、时间范围、数据点和统计值。服务名到 Prometheus job/PromQL 的映射位于 `config/service_catalog.json`。

主动巡检先使用 Agent 本地工具 `list_monitored_services` 获取已登记应用服务，再逐个查询；`all` 不是服务名或通配符。Monitor 会拒绝未登记服务，避免把错误的标签选择器误报为监控没有数据。“所有服务”的检查范围不包含未登记应用及没有配置对应指标的基础设施。

明确请求“主动检查所有服务的存活状态、CPU、内存、延迟、错误率和近期异常日志”时，对话服务使用确定性巡检：逐个登记服务并行查询五项指标及异常日志，固定最近 15 分钟窗口，直接从工具证据生成六项报告，不经过模型改写。指标空数据报告为无法判断；未配置阈值不会产生整体正常结论。普通对话和其他排障问题仍使用 Agent。

应用异常日志使用本地工具 `query_service_logs`，由代码解析服务的 CLS 地域、主题和最近时间窗口，调用官方 `SearchLog`。`GetAlarmLog` 是 CLS 告警执行日志，不是应用日志。查询失败与查询成功但返回零条必须分别报告；返回数量达到限制时不能将其当作完整异常总数。上传凭证和持续日志采集需要单独配置。

异常日志按结构化 `level` 字段筛选 `ERROR`、`FATAL`、`CRITICAL`，不使用正文全文关键词。因此正文提到 ERROR 的 INFO 日志不会被计为异常。主题需要支持 `service` 和 `level` 字段检索；索引或查询失败时报告证据缺失，不回退到全文匹配。缺少级别字段的日志不属于本项检查覆盖范围。

## 启动

```powershell
python mcp_servers/monitor_server.py
```

完整服务建议使用：

```powershell
.\start-windows.bat
```

## 配置新服务

在 `config/service_catalog.json` 中增加：

```json
{
  "services": {
    "order-service": {
      "aliases": ["orders"],
      "prometheus_job": "order-service",
      "metrics": {
        "cpu": "oncall_process_cpu_percent{job=\"order-service\"}",
        "memory": "oncall_process_resident_memory_bytes{job=\"order-service\"}"
      },
      "cls": {
        "region": "ap-shanghai",
        "logset_id": "replace-me",
        "topic_id": "replace-me",
        "query_hint": "service:order-service"
      },
      "owner": "order-oncall"
    }
  }
}
```

CLS 映射为空时，诊断报告必须将日志标记为证据缺失，不得伪造日志结论。

## 持续上传应用日志

在项目根目录 `.env` 中设置以下字段，上传凭证必须单独创建并仅授权目标主题上传，不要复用诊断查询账号或提交到 Git：

```dotenv
CLS_LOG_SPOOL_ENABLED=true
CLS_UPLOAD_ENABLED=true
CLS_UPLOAD_SECRET_ID=填写独立上传凭证
CLS_UPLOAD_SECRET_KEY=填写独立上传凭证
LOG_SERVICE_NAME=ai-oncall-agent
```

Windows 启动脚本会创建独立的 Python 3.11 SDK 环境，避免 SDK 的 protobuf 版本影响主应用。也可以在项目根目录手动验证一次上传：

```powershell
uv sync --project tools/cls-uploader --python 3.11
.\tools\cls-uploader\.venv\Scripts\python.exe -m oncall_logging.uploader --once
```

仅上传启用后生成的 `logs/cls-events/*.jsonl`，不会读取历史原始日志。消息在本地写入及上传前脱敏，异常不记录局部变量；仍应避免业务代码主动记录个人信息或其他敏感内容。API 和诊断 Worker 修改配置后需要重启。

上传成功后才推进本地检查点，失败退避重试；进程崩溃可能造成重复上传，不保证恰好一次。缓冲文件保留 7 天，超出保留期的上传故障可能丢失未上传日志，需要关注上传进程日志及磁盘空间。上传进程退出或主题未完成采集时，检索零条不能作为应用无异常的证据。

使用 `scripts/start-cls-uploader.ps1` 后台启动；查看 `logs/cls-uploader-*.stdout.log` 确认上传结果，使用 `python -m oncall_logging.uploader --stop` 请求停止。`CLS_UPLOAD_ENABLED` 控制启动脚本是否自动启动上传，手动运行上传命令本身即表示执行上传。

Worker 继续以非 root 用户运行；镜像预先创建可写的 `/app/logs`，避免 Docker 自动创建挂载父目录导致 Worker 启动失败。共享目录必须与本机上传器读取的目录一致且允许 UID 10001 写入。若 Docker Desktop 将 Windows 路径映射到虚拟机中的同名空目录，可在本机 `.env` 设置 `CLS_LOG_SPOOL_DOCKER_PATH` 为已验证的 Docker 侧实际路径；本机此次 D 盘对应 `/run/desktop/mnt/d/`，不要未经验证便假设其他安装也使用同一路径。该覆盖只用于卷挂载，不改变本机上传器的读取目录。

`start-windows.bat` 对 API 依赖健康检查最多尝试 12 次，每次请求限时 5 秒，失败之间等待 5 秒。检查或文档上传失败时返回非零退出码，已启动服务保留供排查，不会清理数据卷。成功提示只确认 API 健康检查通过，不代表 CLS 上传或每个 Worker 已验证；Worker 日志权限可在 Docker 就绪后设置 `ONCALL_DOCKER_TESTS=1` 运行对应集成测试。
