# Real provider checks — 2026-10-15

These checks use configured credentials without exposing keys, resource identifiers, log content or configured URLs. Chat and embedding each use one request with SDK retries disabled, a 25-second request timeout and a 30-second overall bound. CLS uses one authenticated read-only `DescribeTopics` request through the cached official Tencent SDK. The initial direct-provider checks started no services and uploaded no logs.

```json
{
  "checked_at_local": "2026-10-15T22:32:55",
  "chat": {
    "status": "passed",
    "reply_matches": true,
    "reply_characters": 4,
    "model": "qwen-max",
    "seconds": 1.13
  },
  "embedding": {
    "status": "passed",
    "dimensions": 1024,
    "all_finite": true,
    "model": "text-embedding-v4",
    "seconds": 0.58
  },
  "cls": {
    "credentials_present": true,
    "read_only_action": "DescribeTopics",
    "sdk": "cached official tencentcloud-sdk-nodejs-cls 4.1.319",
    "status": "passed",
    "authentication": "accepted",
    "topics_returned": 1,
    "seconds": 3.21
  },
  "startup_inspection": {
    "documented_official_package": "cls-mcp-server@1.2.1",
    "node_version": "24.19.0",
    "official_mcp_port": 3000,
    "fallback_demo_port": 8003,
    "listeners_at_initial_inspection": [],
    "started_services": false,
    "uploaded_logs": false
  }
}
```

The documented startup chooses official CLS MCP when Node.js and both Tencent credentials are available; otherwise it falls back to the local demonstration server. Presence of credentials alone does not prove permission to SearchLog, access to the configured topic, or correct topic indexing. The read-only API check is separate from the MCP service readiness check.

## Official CLS MCP verification

Official cached `cls-mcp-server@1.2.1` was started as a hidden Node process with credentials inherited through its environment. Console output is reduced to sanitized lifecycle event names and error codes; it does not print request parameters, credentials or log contents. The service remains running for subsequent API diagnostics. PID metadata is stored in `docs/cls-mcp-runtime-2026-10-15.json`; sanitized log: `logs/cls-mcp-verified-20261015.sanitized.log`.

```json
{
  "checked_at_local": "2026-10-15T22:42:20",
  "endpoint": "http://127.0.0.1:3000/mcp",
  "pid": 18552,
  "operation": "SearchLog",
  "query": "*",
  "lookback_minutes": 5,
  "limit": 1,
  "read_only": true,
  "raw_log_content_disclosed": false,
  "tools_loaded": 20,
  "status": "passed",
  "response_shape": "MCP text blocks containing JSON record list",
  "returned_count": 0,
  "empty": true,
  "records_are_objects": true,
  "permission": "SearchLog accepted for configured topic",
  "schema_matches_query_service_logs": true,
  "seconds": 1.05
}
```
