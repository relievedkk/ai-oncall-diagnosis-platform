# 开发、测试与故障排查

[返回项目首页](../README.md) · [接口与安全](api-and-security.md) · [Windows 运行手册](windows-operations.md)

本文保存开发环境的操作细节。Windows 托管部署、独立探测、维护窗口、备份恢复和发布回滚以运行手册为准。

## 启动方式

需要 Python 3.11–3.13、Docker、uv 和 DashScope API Key。官方腾讯云 CLS 接入另需 Node.js/npm 和云凭证；默认本地 CLS 仅提供示例日志。

### Windows 托管运行

首次配置、凭据生成和启动命令见 [README](../README.md#快速开始)。托管入口会启用跨进程共享模型额度；状态和日志位于 `.runtime/operations/`。

```powershell
.\scripts\oncall-windows.ps1 status
.\scripts\oncall-windows.ps1 probe -Once
.\scripts\oncall-windows.ps1 stop
```

`stop` 停止托管的 API/MCP，保留 Docker 数据服务与 Worker。Docker 栈的维护停写由备份与发布流程协调，详见运行手册。

### Windows 开发运行

首次复制 `.env.example` 为 `.env`，填写 `DASHSCOPE_API_KEY` 后执行：

```powershell
.\start-windows.bat
.\stop-windows.bat
```

开发脚本同步依赖、生成缺失的本地认证凭据、启动数据服务和监控栈、迁移数据库、启动 Worker/MCP/API，并上传示例知识文档。开发停止脚本保留 Docker 数据卷。批处理的 `--managed` 参数可转入托管模式；托管模式须先按 README 完成环境准备。

### Linux/macOS 开发运行

```bash
git clone https://github.com/relievedkk/ai-oncall-diagnosis-platform.git
cd ai-oncall-diagnosis-platform
python3 -m pip install uv
uv sync --frozen
cp .env.example .env
# 编辑 .env，填写 DASHSCOPE_API_KEY
uv run python app/local_env.py --env-file .env
make init
```

`make init` 启动组件、等待就绪并上传示例文档。后续使用 `make start`、`make stop`、`make restart`、`make check` 和 `make status-mcp`。Windows 托管与演练验收结果不代表上述平台已完成同等部署验收。

### 单独启动组件

先准备 `.env` 与认证凭据。开发排查时，在不同终端运行原生组件，避免与现有托管实例同时占用端口：

```powershell
docker compose -f vector-database.yml up -d --build
docker compose -p oncallagent-monitoring -f monitoring.yml up -d --build

# 本地示例 CLS；官方 CLS 接入请使用 MCP 手册中的认证网关
.\.venv\Scripts\python.exe mcp_servers\cls_server.py
.\.venv\Scripts\python.exe mcp_servers\monitor_server.py
.\.venv\Scripts\python.exe -m app.server
```

`app.server` 处理 Windows 事件循环兼容性。登录 Web 后可上传 Markdown、TXT、PDF、DOCX；服务指标与日志映射配置在 `config/service_catalog.json`，接入方法见 [MCP 说明](../mcp_servers/README.md)。

## 组件与访问入口

| 组件 | 本地入口 | 说明 |
| --- | --- | --- |
| Web / 登录 | `http://localhost:9900`、`/login` | 浏览器会话认证 |
| API 文档 | `http://localhost:9900/docs` | 默认关闭，本地调试可设置 `EXPOSE_API_DOCS=true` |
| 健康与指标 | `/live`、`/ready`、`/health`、`/metrics` | 仅 `/live` 公开；其余需要认证 |
| Prometheus / Alertmanager | `localhost:9090` / `localhost:9093` | 查询指标、采集状态与告警 |
| RabbitMQ | `localhost:5672`、管理页 `http://localhost:15672` | 本地开发凭据默认为 `oncall/oncall` |
| PostgreSQL | `localhost:5432` | 本地开发库、账号及密码默认为 `oncall` |
| Milvus / Attu | `localhost:19530` / `http://localhost:8001` | 向量服务与管理界面 |
| CLS / Monitor MCP | `localhost:8003/mcp` / `localhost:8004/mcp` | 本地示例 CLS；官方 CLS 网关使用 3000 端口 |

数据库、消息队列和管理端口使用本地开发配置。完整栈通过 `host.docker.internal` 访问宿主机 API/MCP；外部部署需重新规划访问入口与凭据。

## 测试与质量检查

### 常规回归

```bash
uv sync --frozen --extra dev
uv run pytest -q
node --test tests/*.test.cjs
```

未配置外部依赖时，相关测试会跳过。报告测试结果时应同时给出跳过数量与启用的集成环境。

### PostgreSQL 集成测试

使用专用测试终端和独立测试数据库。以下示例使用本地 Compose PostgreSQL 默认开发账号；如修改过凭据，请同步调整连接地址。首次创建测试库后先执行迁移，再启动套件：

```powershell
docker exec oncall-postgres createdb -U oncall oncall_test
$env:DATABASE_URL = 'postgresql+asyncpg://oncall:oncall@127.0.0.1:5432/oncall_test'
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run alembic upgrade head
uv run pytest -q
```

已有测试库无需再次 `createdb`；不要将测试地址指向业务数据库。`TEST_DATABASE_URL` 开启持久化与并发测试，`DATABASE_URL` 则供迁移及部分组件使用，两者在本测试终端中保持一致。

### 显式外部集成

在测试终端配置已迁移的测试库和 Milvus 连接后，可额外启用真实知识库依赖验证：

```powershell
$env:ONCALL_KNOWLEDGE_LIVE_TEST = '1'
uv run pytest tests/test_knowledge_live_dependencies.py -q
```

Docker Desktop 可用时，可验证隔离冷备恢复与 Worker 日志挂载权限：

```powershell
$env:ONCALL_DOCKER_TESTS = '1'
uv run pytest tests/test_ops_drill_integration.py tests/test_worker_log_permissions.py -q
```

知识库集成使用随机测试 collection；冷备测试创建独立合成数据栈，不读取业务数据库。日志权限测试使用实际 Compose 挂载，需要已构建的 Worker 镜像。演练内容和恢复验收范围见 [验证记录](windows-operations-verification.md)。

### 配置、迁移与诊断质量

```bash
uv run ruff check app oncall_ops mcp_servers tests --select E9,F63,F7,F82
uv run alembic check
docker compose -f vector-database.yml config --quiet
docker compose -f monitoring.yml config --quiet

uv run python -m app.evaluation.cli \
  --cases evals/diagnosis_cases.json \
  --results path/to/diagnosis_results.json
```

`alembic check` 需要连接已升级到当前迁移头的数据库。诊断结果文件为包含 `case_id`、`report`、`evidence` 的数组；评测是关键词和证据存在性门禁，不验证语义上的真实根因。真实使用反馈与评测结果分别统计，见 [统计口径](usage-feedback.md)。完整 CI 步骤以 [工作流](../.github/workflows/ci.yml) 为准。

## 故障排查

| 现象 | 优先检查 |
| --- | --- |
| 401 / 403 | 浏览器是否登录、会话是否过期、写请求是否携带 CSRF；机器请求使用独立 API Key |
| 聊天或上传 503 | 返回的公开错误、模型/数据库/Milvus 可用性、共享额度服务；配置错误和依赖故障分别处理 |
| API 启动失败 | `.env` 中的密钥和管理员密码、端口、数据库迁移、Milvus 向量维度；API 文档默认关闭不代表服务失败 |
| 任务长期 queued / retrying | Outbox Publisher、RabbitMQ、critical/default Worker 是否运行及队列是否有消费者 |
| 处置等待审批 | 动作状态与审批期限；机器 API Key 无审批权限；确认 `remediation.control` 有消费者 |
| Prometheus 无数据 | 目标是否 UP、认证 `/metrics` 是否可访问、容器能否经 `host.docker.internal` 访问宿主机 |
| CLS 返回零条 | 区分示例与真实日志、成功空结果与查询失败；主题索引、服务映射、上传链路是否完整 |
| 托管启动超时 / degraded | `status`、探针及 `.runtime/operations/runtime/logs/` 中的原生服务日志 |

常用诊断命令：

```powershell
docker compose -f vector-database.yml ps
docker compose -f vector-database.yml logs --tail=100 rabbitmq diagnosis-critical-worker diagnosis-default-worker outbox-publisher
docker compose -f vector-database.yml logs --tail=100 remediation-worker
netstat -ano | findstr :9900
```

先确认占用端口的进程身份，再通过其启动方式停止，避免终止其他项目的服务。Milvus schema / 向量维度不匹配时不会自动删库；需要新建 collection、重建索引并显式切换。

### 死信重放

确认任务失败原因已处理，Publisher 与 Worker 正在运行后，在部署对应的数据库配置下执行：

```bash
uv run python -m app.tasks.replay_dead_letter DIAGNOSIS_ID
```

重放在一个事务内重置任务并新增 Outbox 事件，重复请求不会重复入队；普通 broker 重投不会自动重新打开死信任务。处置动作仍受原有幂等键保护。

### 本地告警链路演练

在隔离开发环境设置 `FAULT_INJECTION_ENABLED=true` 并重启 API，用测试凭据执行：

```powershell
$headers = @{ 'X-API-Key' = $env:ONCALL_TEST_API_KEY }
Invoke-RestMethod -Method Post -Headers $headers http://localhost:9900/api/debug/fault/active
# 等待 Prometheus 规则计算及 Alertmanager 转发后检查任务
Invoke-RestMethod -Headers $headers http://localhost:9900/api/aiops/jobs
Invoke-RestMethod -Method Post -Headers $headers http://localhost:9900/api/debug/fault/resolved
```

`ONCALL_TEST_API_KEY` 应由本机环境安全注入，并与 `.env` 的 `API_KEY` 一致。测试后恢复 `FAULT_INJECTION_ENABLED=false` 并重启 API；告警触发时间取决于规则窗口和转发配置。
