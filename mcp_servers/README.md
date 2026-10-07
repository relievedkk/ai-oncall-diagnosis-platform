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
