# 接口与安全约束

[返回项目首页](../README.md) · [开发指南](development.md) · [配置模板](../.env.example)

本文为接口索引与关键约束说明，请求和响应模型以 `app/api/`、`app/models/` 为准。本地需要交互式接口文档时，设置 `EXPOSE_API_DOCS=true`，重启 API 后访问 `/docs`；默认关闭 `/docs`、`/redoc` 与 OpenAPI JSON。

## 认证方式

| 调用方 | 方式 | 范围 |
| --- | --- | --- |
| 浏览器用户 | 登录后使用 HttpOnly Cookie；写请求携带 `X-CSRF-Token` | 会话权限内的业务操作；处置审批仅允许浏览器会话 |
| 自动化脚本 / Prometheus | `X-API-Key` | 机器接口调用；不能批准处置或提交用户反馈 |
| Alertmanager | 独立 `Authorization: Bearer ...` | 仅告警 Webhook，使用 `ALERTMANAGER_WEBHOOK_TOKEN` |
| 官方 CLS HTTP 网关客户端 | 独立 Bearer Token | 服务目录约束下的日志查询，使用 `MCP_CLS_TOKEN` |

API、Webhook、CLS 凭据须分别配置。`app/local_env.py` 会为本地占位符/过短配置生成独立随机值，不输出密钥内容；`.env` 不进入 Git 或 Worker 镜像。

浏览器密码使用 Argon2id 哈希，会话与 CSRF 令牌仅保存摘要。普通会话默认 8 小时，保持登录默认 7 天；连续登录失败 5 次锁定 15 分钟。密码计算使用有界线程池，避免阻塞异步服务。

`ADMIN_PASSWORD` 仅在数据库中不存在该账号时用于创建管理员，修改 `.env` 不会重置已有密码。HTTPS 部署设置 `SESSION_COOKIE_SECURE=true`。只有 API 绑定回环地址时才允许关闭认证；完整本地监控栈需要保留强认证。

## API 索引

下表的“常规认证”指浏览器会话或机器 API Key；浏览器写请求同时要求 CSRF。部分接口另有主体归属和角色校验。

| 功能 | 方法与路径 | 认证 / 约束 |
| --- | --- | --- |
| 首页 / 登录页 | `GET /`、`GET /login` | 首页需要登录或机器认证；登录页公开 |
| 登录 | `POST /api/auth/login` | 公开入口，有登录限流与失败锁定 |
| 当前账号 / 退出 | `GET /api/auth/me`、`POST /api/auth/logout` | 浏览器会话；退出需要 CSRF |
| 存活检查 | `GET /live` | 公开，不依赖业务组件全部就绪 |
| 就绪 / 兼容健康检查 | `GET /ready`、`GET /health` | 常规认证，检查依赖和诊断消费者 |
| Prometheus 指标 | `GET /metrics` | 常规认证；采集凭据通过 Docker Secret 传递 |
| 对话 | `POST /api/chat`、`POST /api/chat_stream` | 常规认证；后者返回 SSE |
| 清空 / 读取会话 | `POST /api/chat/clear`、`GET /api/chat/session/{session_id}` | 常规认证，聊天状态按账户隔离 |
| 上传并索引 | `POST /api/upload` | 常规认证；文件大小、类型和内容边界校验 |
| 索引允许目录 | `POST /api/index_directory` | 常规认证；仅允许 `ALLOWED_INDEX_ROOTS` 范围 |
| 创建持久诊断 | `POST /api/aiops/jobs` | 常规认证；必须提供 `Idempotency-Key`，返回 202 |
| 订阅诊断事件 | `GET /api/aiops/jobs/{diagnosis_id}/events` | 常规认证及订阅归属检查；支持事件游标 |
| 任务列表 / 详情 | `GET /api/aiops/jobs`、`GET /api/aiops/jobs/{diagnosis_id}` | 常规认证；列表支持 `limit`、`offset` |
| 兼容流式诊断 | `POST /api/aiops` | 常规认证；需要可靠重连的新客户端使用创建与订阅分离的接口 |
| 处置列表 / 详情 | `GET /api/remediations`、`GET /api/remediations/{action_id}` | 常规认证；列表支持状态过滤和 1–100 的 `limit` |
| 批准 / 拒绝处置 | `POST /api/remediations/{action_id}/decision` | 仅浏览器会话 + CSRF；校验状态、期限与冻结证据 |
| 告警回调 | `POST /api/alerts/webhook` | 独立 Bearer Token；批次校验通过后可靠入队 |
| 提交使用反馈 | `POST /api/feedback` | 仅回答所属登录用户；写请求需要 CSRF |
| 反馈汇总 | `GET /api/feedback/summary` | 仅管理员浏览器会话 |
| 本地故障注入 | `POST /api/debug/fault/{state}` | 常规认证，且 `FAULT_INJECTION_ENABLED=true`；默认关闭 |

### 创建与恢复诊断

在配置好机器认证的终端中，创建任务：

```bash
curl -X POST http://localhost:9900/api/aiops/jobs \
  -H "X-API-Key: ${ONCALL_TEST_API_KEY}" \
  -H "Idempotency-Key: incident-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"incident-demo"}'
```

响应包含 `diagnosis_id` 和 `events_url`。随后订阅响应给出的地址：

```bash
curl -N http://localhost:9900/api/aiops/jobs/DIAGNOSIS_ID/events \
  -H "X-API-Key: ${ONCALL_TEST_API_KEY}"
```

恢复订阅时传入最后一个**完整应用**事件的 `Last-Event-ID`，或使用 `after_id` 查询参数。同一主体、同一幂等键和同一请求复用原任务，请求冲突返回 409；仅恢复订阅不会创建新任务。

浏览器按账户保存任务 ID、创建键和事件游标，断线采用最长 30 秒的指数退避；终态事件去重，认证失效停止重连，切换账户清理旧状态。新手动任务仅创建者可订阅；旧手动任务保留管理员兼容规则，告警任务保持团队可见。该订阅约束不能解释为全系统具备多租户隔离。

普通对话示例：

```bash
curl -X POST http://localhost:9900/api/chat \
  -H "X-API-Key: ${ONCALL_TEST_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"Id":"session-demo","Question":"当前系统有哪些告警？"}'
```

`ONCALL_TEST_API_KEY` 为示例环境变量，应在本机注入与部署 `API_KEY` 一致的值。不要把真实凭据写入请求示例、脚本仓库或截图。

## 模型、证据与处置边界

Agent 使用服务目录限制可查询对象，不直接持有任意命令执行器。官方 CLS 由私有 stdio 子进程接入认证网关，仅开放范围受限的 `SearchLog`；Monitor 查询已登记服务的指标。工具输出在进入模型、检查点或公开结果前经过脱敏与大小限制。

处置需要成功、非示例且归属于目标服务的观测；重启还要求准确实例，聚合指标不能授权实例重启。采集/观测时间需带时区且在允许范围内，证据缺失或冲突时拒绝授权。审批最晚在配置期限与证据有效期中的较早时间过期；审批和首次执行领取时再次校验。旧快照缺少校验元数据不能用于新执行，已启动动作保留恢复路径。

处置模式与执行 adapter 分开配置：

| `REMEDIATION_MODE` | 行为 |
| --- | --- |
| `off` | 禁止创建处置 |
| `dry_run` | 运行控制链并模拟动作 |
| `assisted` | 允许的动作需要人工审批 |
| `guarded_auto` | 仅 Policy 明确放行的低风险动作可自动执行 |

**当前唯一支持的 `REMEDIATION_ADAPTER` 是 `dry_run`**，其他值使应用拒绝启动。更改模式不会获得真实基础设施写入能力。动作 allowlist、证据、预算、冷却期、已有活动动作及全局上限都由非模型代码校验。

处置 API 不提供公开创建、执行、重试或回滚入口。审批请求为 `{"decision":"approve"}` 或 `{"decision":"reject"}`；过期、缺失和状态冲突分别返回 410、404 和 409。公开详情仅返回有长度上限的冻结证据与审计结果，不公开 prepared payload、rollback token、Worker 标识、幂等键或敏感执行器字段。

## 持久化与一致性约束

- **诊断所有权**：步骤、证据、报告、预算及检查点写入均校验当前领取身份；旧 Worker 不能覆盖新执行者结果。浏览器断开不会取消已入队任务。
- **检查点数据库**：诊断检查点和业务数据必须连接同一实际 PostgreSQL 数据库并解析同一 `diagnosis_jobs` 表，写入在同一事务中锁定任务。不同数据库或影子表配置拒绝运行；聊天检查点可单独配置数据库。
- **知识库版本**：参与读写的进程共享 PostgreSQL 协调锁与版本记录。替换遵循新向量写入、flush、版本切换与旧数据清理；不宣称 PostgreSQL 与 Milvus 间存在分布式原子事务，外部写入者也必须遵循协议。
- **账户与历史**：聊天检查点按账户隔离，诊断检查点按任务隔离；旧的未命名空间聊天记录不会自动并入新账户。浏览器历史保存在当前浏览器的用户命名空间，退出或切换账户时阻止旧请求恢复界面。

## 关键配置

完整默认值以 [.env.example](../.env.example) 和 [配置模型](../app/config.py) 为准，修改后重启对应 API/MCP/Worker。

| 配置组 | 关键字段 / 默认值 | 作用 |
| --- | --- | --- |
| 模型 | `DASHSCOPE_API_KEY`、`DASHSCOPE_MODEL`、`LLM_REQUEST_TIMEOUT` | 模型凭据、型号和请求期限 |
| 模型数据 | `MODEL_MAX_INPUT_BYTES=65536`、`TOOL_RESULT_MAX_BYTES=16384` | 输入与工具证据大小上限 |
| 单次诊断预算 | `DIAGNOSIS_MAX_MODEL_CALLS=24`、`DIAGNOSIS_MAX_MODEL_INPUT_BYTES=262144` | 跨重试持久计量 |
| 跨进程配额 | `SHARED_MODEL_QUOTA_*` | 同供应商账户共享并发、分钟调用数与输入字节预算；托管入口强制启用 |
| 证据时效 | `REMEDIATION_EVIDENCE_MAX_AGE_SECONDS=600`、`REMEDIATION_EVIDENCE_CLOCK_SKEW_SECONDS=30` | API 与所有 Worker 保持一致 |
| 处置控制 | `REMEDIATION_APPROVAL_EXPIRY_SECONDS=900`、`REMEDIATION_DEFAULT_COOLDOWN_SECONDS=900` | 审批期限与冷却时间 |
| 持久化与队列 | `DATABASE_URL`、`CELERY_BROKER_URL`、`LANGGRAPH_CHECKPOINT_*` | 数据库、消息队列与恢复状态 |
| 知识库 | `MILVUS_*`、`SERVICE_CATALOG_PATH`、`ALLOWED_INDEX_ROOTS` | 向量连接、服务映射和目录范围 |
| 上传 | `MAX_UPLOAD_BYTES=10485760`、`MAX_ARCHIVE_UNCOMPRESSED_BYTES=52428800` | 文件与解压后内容上限 |
| 请求边界 | `MAX_REQUEST_BODY_BYTES=12582912`、`REQUEST_BODY_TIMEOUT_SECONDS=15` | 实际请求体大小与总读取期限 |

共享额度不可用时拒绝新模型调用；额度耗尽返回 429 和 `Retry-After`，服务故障返回 503。已启动 SSE 通过错误事件传递 `error_code`、`retry_after`。共享额度限制不覆盖 embedding，不是供应商精确 Token/金额计费；租约到期也不代表远端请求已立即停止。HTTP 入口限流仍包含单进程实现，多实例部署需结合网关控制。

生产迁移应设置 `DATABASE_AUTO_CREATE=false`，在专用发布步骤执行 `uv run alembic upgrade head`。应用回滚不执行 schema 降级，不用旧备份覆盖新增业务数据；版本兼容检查和维护顺序见 [运行手册](windows-operations.md)。
