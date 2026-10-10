# AI 智能运维与 OnCall 诊断平台

一个面向研发与运维场景的 AI 故障诊断原型。系统将运维知识库、Prometheus 指标与告警、腾讯云 CLS 日志查询统一到自然语言入口，并通过 Plan-Execute-Replan 工作流生成可追踪的诊断过程和处置建议。

[![Python](https://img.shields.io/badge/Python-3.11--3.13-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 核心能力

- **智能对话**：基于 LangChain/LangGraph 的工具调用对话，支持普通响应和 SSE 流式响应。
- **RAG 知识库**：上传 Markdown 或文本文件，完成分块、向量化、Milvus 存储和 Top-K 检索。
- **AIOps 诊断**：通过 Planner、Executor、Replanner 自动规划排障步骤并生成诊断报告。
- **真实可观测数据**：FastAPI 暴露请求量、错误率、延迟、CPU、内存及诊断任务指标。
- **自动告警闭环**：Prometheus 触发规则后由 Alertmanager 回调应用，并自动创建诊断任务。
- **独立任务队列**：诊断任务通过 PostgreSQL Transactional Outbox 可靠投递到 RabbitMQ，由隔离的 critical/default Celery Worker 执行，支持重试、心跳、执行租约和死信队列。
- **持久化工作流**：LangGraph 对话和诊断检查点写入 PostgreSQL，API 或 Worker 重启后仍可恢复状态。
- **B 端账号登录**：运维人员使用账号密码登录，密码以 Argon2id 哈希保存，会话可撤销，并带有登录限流、失败锁定与 CSRF 防护。
- **质量评测**：使用固定诊断案例对根因命中、证据完整性、报告完整性和无依据结论进行离线回归评分。
- **MCP 工具接入**：Monitor MCP 查询真实 Prometheus 数据；CLS MCP 可连接腾讯云日志或使用本地示例数据。

## 系统架构

```mermaid
flowchart LR
    User[用户 / OnCall 工程师] --> Web[Web 与 FastAPI]
    Docs[运维文档] --> RAG[RAG 索引服务]
    RAG --> Milvus[(Milvus)]
    Web -->|任务 + Outbox 原子写入| PostgreSQL[(PostgreSQL)]
    Web --> Agent[对话 Agent / AIOps Agent]
    Agent --> Milvus
    Agent --> Monitor[Monitor MCP]
    Agent --> CLS[CLS MCP]
    Web -->|/metrics| Prometheus[Prometheus]
    Prometheus -->|告警规则| Alertmanager[Alertmanager]
    Alertmanager -->|Webhook| Web
    PostgreSQL --> Publisher[Outbox Publisher]
    Publisher --> RabbitMQ[(RabbitMQ)]
    RabbitMQ --> CriticalWorker[Critical Worker]
    RabbitMQ --> DefaultWorker[Default Worker]
    RabbitMQ --> DLQ[Dead-letter Queue]
    CriticalWorker --> Agent
    DefaultWorker --> Agent
    CriticalWorker --> PostgreSQL
    DefaultWorker --> PostgreSQL
    Monitor --> Prometheus
    CLS --> Logs[腾讯云 CLS / 本地示例日志]
```

一次自动诊断的基本流程：

1. FastAPI 持续暴露应用运行指标。
2. Prometheus 每 15 秒采集指标并计算告警规则。
3. 告警进入 `firing` 后，Alertmanager 调用 `/api/alerts/webhook`。
4. FastAPI 在同一数据库事务内创建诊断任务和 Outbox 事件，然后立即响应。
5. Outbox Publisher 将事件可靠发布到 RabbitMQ；严重告警进入高优先级队列。
6. critical/default 独立 Celery Worker 领取任务，通过租约与心跳避免重复执行，再查询 Prometheus、CLS 和 Milvus。
7. Planner、Executor、Replanner 迭代执行，任务状态、步骤输出与证据实时写入 PostgreSQL。
8. 最终 Markdown 报告和 LangGraph 检查点持久化；临时失败自动重试，耗尽重试后任务标记为 `dead_lettered` 并进入死信队列。

## 快速开始

### 环境要求

- Python 3.11、3.12 或 3.13
- Docker Desktop 或兼容的 Docker Engine
- 阿里云 DashScope API Key（[获取地址](https://dashscope.aliyun.com/)）
- Git
- Node.js 18+（可选；只有接入官方腾讯云 CLS MCP 时需要）

### Windows

```powershell
# 1. 克隆项目
git clone https://github.com/relievedkk/ai-oncall-diagnosis-platform.git
cd ai-oncall-diagnosis-platform

# 2. 创建本地配置
Copy-Item .env.example .env
notepad .env

# 3. 至少把 DASHSCOPE_API_KEY 修改为真实值，然后一键启动
.\start-windows.bat
```

启动脚本会依次完成：

- 创建或同步 `.venv`
- 构建诊断 Worker，并启动 PostgreSQL、RabbitMQ、Milvus、MinIO 和 Attu
- 执行数据库迁移，启动 Celery Worker 与 Outbox Publisher
- 启动 Prometheus、Alertmanager、Node Exporter 和 cAdvisor
- 启动 CLS MCP 与 Monitor MCP
- 启动 FastAPI
- 上传 `aiops-docs` 中的示例知识文档

停止全部服务并保留数据卷：

```powershell
.\stop-windows.bat
```

### Linux/macOS

```bash
# 1. 克隆项目
git clone https://github.com/relievedkk/ai-oncall-diagnosis-platform.git
cd ai-oncall-diagnosis-platform

# 2. 安装 uv 并同步依赖
python3 -m pip install uv
uv sync

# 3. 创建本地配置并填写 DASHSCOPE_API_KEY
cp .env.example .env
vim .env

# 4. 启动完整系统、等待就绪并上传示例文档
make init
```

后续可使用：

```bash
make start       # 启动数据库、RabbitMQ、Worker、监控栈、MCP 和 FastAPI
make stop        # 停止全部服务，保留 Docker 数据卷
make restart     # 重启全部服务
make check       # 检查 FastAPI 健康状态
make status-mcp  # 查看 MCP 服务状态
```

## 服务入口

| 服务 | 地址 | 用途 |
|---|---|---|
| Web 应用 | http://localhost:9900 | 对话、上传知识文档、启动 AIOps 诊断 |
| 登录页 | http://localhost:9900/login | 运维人员账号登录；首次启动默认账号为 `admin` |
| API 文档 | http://localhost:9900/docs | 查看并调试 FastAPI 接口 |
| 应用指标 | http://localhost:9900/metrics | 查看 Prometheus 格式的原始指标 |
| Prometheus | http://localhost:9090 | 查询指标、目标状态和告警规则 |
| Alertmanager | http://localhost:9093 | 查看正在触发或已恢复的告警 |
| Attu | http://localhost:8001 | 查看和管理 Milvus 中的向量数据 |
| RabbitMQ 管理界面 | http://localhost:15672 | 查看诊断队列、消息速率和消费者；本地默认 `oncall/oncall` |
| PostgreSQL | `localhost:5432` | 持久化诊断任务、步骤、证据和报告；无 Web 页面 |

## 配置

先复制模板：

```bash
cp .env.example .env
```

至少需要填写模型密钥；本地启动脚本会自动生成独立的机器 API Key、Webhook Token 和管理员初始密码：

```dotenv
DASHSCOPE_API_KEY=你的真实密钥
AUTH_ENABLED=true
API_KEY=一个独立的64位十六进制随机值
ALERTMANAGER_WEBHOOK_TOKEN=另一个独立的64位十六进制随机值
WEB_LOGIN_ENABLED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=管理员初始强密码
```

Windows 一键启动脚本和 Linux/macOS 的 `make start` 会执行 `python -m app.local_env`：如果本地 API Key、Webhook Token 或管理员密码仍为空、过短或是模板占位符，会自动生成随机值，但不会输出密钥内容。生成后可在本机 `.env` 中查看 `ADMIN_PASSWORD`；该文件不会提交到 Git。

常用配置：

```dotenv
# 默认开启认证；完整 Compose 栈需要监听 Docker 可访问的宿主机接口
HOST=0.0.0.0
DEBUG=false
AUTH_ENABLED=true
API_KEY=请替换为强随机值
ALERTMANAGER_WEBHOOK_TOKEN=请替换为另一个强随机值
WEB_LOGIN_ENABLED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为管理员初始强密码
SESSION_COOKIE_SECURE=false

# MCP
MCP_CLS_TRANSPORT=streamable-http
MCP_CLS_URL=http://127.0.0.1:8003/mcp
MCP_MONITOR_TRANSPORT=streamable-http
MCP_MONITOR_URL=http://127.0.0.1:8004/mcp

# Prometheus 与服务映射
PROMETHEUS_BASE_URL=http://127.0.0.1:9090
SERVICE_CATALOG_PATH=config/service_catalog.json

# PostgreSQL 诊断存储
DATABASE_URL=postgresql+asyncpg://oncall:oncall@127.0.0.1:5432/oncall
DATABASE_AUTO_CREATE=true

# RabbitMQ / Celery 独立任务队列
CELERY_BROKER_URL=amqp://oncall:oncall@127.0.0.1:5672//
CELERY_DEFAULT_QUEUE=diagnosis.default
CELERY_CRITICAL_QUEUE=diagnosis.critical
CELERY_DEAD_LETTER_QUEUE=diagnosis.dead-letter
CELERY_TASK_MAX_RETRIES=5
RABBITMQ_MANAGEMENT_URL=http://127.0.0.1:15672

# LangGraph 检查点（留空时从 DATABASE_URL 推导）
LANGGRAPH_CHECKPOINT_ENABLED=true
LANGGRAPH_CHECKPOINT_DATABASE_URL=

# 默认禁止故障注入
FAULT_INJECTION_ENABLED=false
```

完整字段及安全说明见 [.env.example](.env.example)。`.env` 已加入 `.gitignore`，不要把真实密钥提交到仓库。

本地首次启动时会自动执行 Alembic 数据库迁移。生产环境建议设置 `DATABASE_AUTO_CREATE=false`，在发布阶段单独执行：

```bash
uv run alembic upgrade head
```

### 登录与 API 认证

只有服务绑定到回环地址时，才允许临时关闭认证：

```dotenv
HOST=127.0.0.1
AUTH_ENABLED=false
```

完整本地监控栈、局域网或公网环境必须启用认证：

```dotenv
AUTH_ENABLED=true
API_KEY=请替换为足够长的随机值
WEB_LOGIN_ENABLED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为管理员初始强密码
```

启用后：

- 浏览器访问首页时会跳转到 `/login`，登录成功后使用 HttpOnly Cookie 会话，不再要求用户粘贴 API Key。
- 密码使用 Argon2id 哈希存入 PostgreSQL；服务端仅保存会话令牌和 CSRF 令牌的 SHA-256 摘要。
- 普通会话默认 8 小时；勾选“保持登录”后为 7 天。连续失败 5 次会锁定账号 15 分钟。
- `ADMIN_PASSWORD` 只在数据库不存在该账号时用于创建初始管理员；以后修改 `.env` 不会覆盖数据库中的密码。
- Prometheus、命令行和其他机器客户端继续通过 `X-API-Key` 请求头发送机器密钥。
- Windows 启动脚本和 Makefile 上传命令会从 `.env` 读取 API Key。
- HTTPS 部署必须设置 `SESSION_COOKIE_SECURE=true`。

### Alertmanager Webhook 认证

`ALERTMANAGER_WEBHOOK_TOKEN` 是必填项。Compose 会把它作为 Docker Secret 挂载给 Alertmanager，再使用 Bearer Token 调用 Webhook；不要把真实 Token 写进仓库。Token 缺失或过短时应用会拒绝启动。

## 手动启动与排查

一键脚本失败时，可以分别启动组件。以下 PowerShell 命令使用不同终端窗口执行。

```powershell
# 终端 1：Docker 依赖、数据库迁移与诊断 Worker
docker compose -f vector-database.yml up -d --build
docker compose -p oncallagent-monitoring -f monitoring.yml up -d --build

# 终端 2：本地 CLS MCP
.\.venv\Scripts\python.exe mcp_servers\cls_server.py

# 终端 3：Monitor MCP
.\.venv\Scripts\python.exe mcp_servers\monitor_server.py

# 终端 4：FastAPI（该入口会自动处理 Windows 事件循环兼容性）
.\.venv\Scripts\python.exe -m app.server
```

文档可以在登录后的 Web 页面直接上传。

## API

| 功能 | 方法 | 路径 | 认证 |
|---|---|---|---|
| 登录页 | GET | `/login` | 否 |
| 账号登录 | POST | `/api/auth/login` | 否（限流） |
| 当前账户 | GET | `/api/auth/me` | 浏览器会话 |
| 退出登录 | POST | `/api/auth/logout` | 浏览器会话 + CSRF |
| 首页 | GET | `/` | 是 |
| 存活检查 | GET | `/live` | 否 |
| 就绪检查 | GET | `/ready` | 是 |
| 兼容健康检查 | GET | `/health` | 是 |
| 应用指标 | GET | `/metrics` | 是（Prometheus 使用 Docker Secret） |
| 普通对话 | POST | `/api/chat` | 是 |
| 流式对话 | POST | `/api/chat_stream` | 是 |
| 清空会话 | POST | `/api/chat/clear` | 是 |
| 会话历史 | GET | `/api/chat/session/{session_id}` | 是 |
| 上传并索引文件 | POST | `/api/upload` | 是 |
| 索引允许目录 | POST | `/api/index_directory` | 是 |
| 手动 AIOps 诊断 | POST | `/api/aiops` | 是 |
| 自动诊断任务列表 | GET | `/api/aiops/jobs` | 是 |
| 自动诊断任务详情 | GET | `/api/aiops/jobs/{diagnosis_id}` | 是 |
| Alertmanager 回调 | POST | `/api/alerts/webhook` | 独立 Bearer Token |
| 本地故障注入 | POST | `/api/debug/fault/{state}` | 是，且默认关闭 |

API 文档默认关闭；如需本地调试，可临时设置 `EXPOSE_API_DOCS=true`。关闭认证时必须同时使用 `HOST=127.0.0.1`。

任务详情会返回 `steps`、`evidence` 和 `report`：步骤保存执行状态与完整输出，证据保存每个 Planner、Executor、Replanner 事件的来源、时间和原始载荷，报告则作为独立记录保存。手动 `/api/aiops` 与 Alertmanager 自动触发的诊断都会先可靠入队，再由独立 Worker 写入同一套表。浏览器断开或 FastAPI 重启不会中断已经入队的诊断。

### 请求示例

```bash
API_KEY="从 .env 读取，不要提交"

curl -X POST "http://localhost:9900/api/chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"Id":"session-123","Question":"当前系统有哪些告警？"}'

curl -N -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"session_id":"session-123"}'
```

## 本地端到端告警测试

故障注入接口默认关闭，只能在隔离的本地测试环境临时启用。

1. 在 `.env` 中设置 `FAULT_INJECTION_ENABLED=true`。
2. 重启 FastAPI。
3. 触发故障并等待 Prometheus 和 Alertmanager 完成一次规则计算与转发。

```powershell
$headers = @{"X-API-Key" = "your-api-key"}

Invoke-RestMethod -Method Post -Headers $headers http://localhost:9900/api/debug/fault/active
Start-Sleep -Seconds 30
Invoke-RestMethod -Headers $headers http://localhost:9900/api/aiops/jobs

# 测试完成后恢复
Invoke-RestMethod -Method Post -Headers $headers http://localhost:9900/api/debug/fault/resolved
```

测试结束后将 `FAULT_INJECTION_ENABLED=false` 并重启应用。

## 项目结构

```text
ai-oncall-diagnosis-platform/
├── app/
│   ├── agent/aiops/                 # Planner、Executor、Replanner
│   ├── api/                         # 对话、文件、告警、指标和调试接口
│   ├── core/                        # 配置、认证、LLM、Milvus、指标
│   ├── db/                          # PostgreSQL 会话与诊断数据模型
│   ├── evaluation/                  # 诊断质量评分与命令行入口
│   ├── models/                      # API 与告警数据模型
│   ├── repositories/                # 诊断持久化 Repository
│   ├── services/                    # RAG、向量库、诊断编排与服务目录
│   ├── tasks/                       # Celery 诊断任务
│   ├── tools/                       # 知识库、时间和 Prometheus 工具
│   ├── workers/                     # Transactional Outbox 发布进程
│   ├── celery_app.py                # Celery、交换机和队列配置
│   └── main.py                      # FastAPI 入口
├── aiops-docs/                      # 示例运维知识文档
├── config/service_catalog.json      # 服务、Prometheus 和 CLS 映射
├── evals/diagnosis_cases.json        # 固定诊断质量评测案例
├── mcp_servers/                     # CLS MCP 与 Monitor MCP
├── monitoring/                      # Prometheus 与 Alertmanager 配置
├── migrations/                      # Alembic 数据库迁移
├── static/                          # Web 前端
├── tests/                           # 自动化测试
├── .env.example                     # 安全配置模板
├── monitoring.yml                   # 监控栈 Docker Compose
├── Dockerfile.worker                # Celery Worker / Publisher 镜像
├── vector-database.yml              # 数据库、RabbitMQ、Milvus 与 Worker Compose
├── start-windows.bat                # Windows 一键启动
├── stop-windows.bat                 # Windows 一键停止
├── Makefile                         # Linux/macOS 管理命令
└── pyproject.toml                   # Python 项目与依赖配置
```

## 开发与测试

```bash
# 同步开发依赖
uv sync --extra dev

# 运行测试
uv run pytest -q

# 校验迁移和 Compose 配置
uv run alembic check
docker compose -f vector-database.yml config --quiet

# 对保存的真实诊断结果做质量门禁（结果文件为 case_id/report/evidence 数组）
uv run python -m app.evaluation.cli \
  --cases evals/diagnosis_cases.json \
  --results path/to/diagnosis_results.json

```

## 安全与生产部署说明

仓库内的 Compose 文件面向本地开发，不能原样暴露到公网：

- MinIO 使用本地默认凭证，并映射了管理端口。
- PostgreSQL 使用本地开发凭证并映射了端口，生产环境必须更换密码并限制网络访问。
- RabbitMQ 使用本地开发凭证，管理界面和 AMQP 端口仅绑定到回环地址；生产环境仍必须更换密码并启用 TLS。
- Prometheus、Alertmanager、Milvus 和 Attu 默认映射到宿主机端口。
- cAdvisor 使用特权模式并读取 Docker/宿主机相关目录。
- `/metrics`、`/health` 与业务 API 均受认证保护；API 文档默认关闭。
- Milvus collection 的向量维度不匹配时应用会拒绝启动，不会自动删除已有向量；请通过新 collection 重建并显式切换。
- FastAPI 监听 `0.0.0.0`，以便 Docker 内的 Prometheus 通过 `host.docker.internal` 抓取指标；请使用主机防火墙限制外部访问。
- 本地示例 CLS 返回演示数据，不代表真实生产日志。

生产环境至少应完成：网络隔离、TLS、强认证、密钥管理、最小权限、固定镜像版本、数据备份、审计日志和高可用部署。

## 常见问题

### Web 可以打开，但聊天或上传返回 503

机器调用出现 401/503 时，通常是 API Key 未配置或长度不足。浏览器用户无需填写 Key，应访问 `/login`，使用 `.env` 中的 `ADMIN_USERNAME` 和首次启动时生成的 `ADMIN_PASSWORD` 登录。只有 `HOST=127.0.0.1` 时才允许关闭认证。

### Milvus 连接失败

```bash
docker compose -f vector-database.yml ps
docker compose -f vector-database.yml restart standalone
```

### 诊断任务一直处于 queued / retrying

```bash
docker compose -f vector-database.yml ps
docker compose -f vector-database.yml logs --tail=100 rabbitmq diagnosis-critical-worker diagnosis-default-worker outbox-publisher
```

在 RabbitMQ 管理界面检查 `diagnosis.critical` 和 `diagnosis.default` 是否各有消费者。`diagnosis.dead-letter` 中出现消息表示任务已耗尽自动重试，需要结合数据库任务的 `error`、`evidence` 和 `dead_lettered_at` 人工处理。数据库中的 Outbox 事件会在发布失败后退避重试；Worker 异常退出后，执行租约到期的任务可被再次领取。

### Prometheus 没有数据

1. 使用 `X-API-Key` 请求头确认 http://localhost:9900/metrics 可以访问。
2. 在 Prometheus 的 `Status → Targets` 检查 `ai-oncall-agent` 是否为 `UP`。
3. Docker Desktop 环境需支持通过 `host.docker.internal` 访问宿主机。

### 端口被占用

```powershell
netstat -ano | findstr :9900
taskkill /F /PID <PID>
```

主要端口：`9900`、`5432`、`5672`、`15672`、`8001`、`8003`、`8004`、`9090`、`9093`、`19530`。

## 参考资源

- [FastAPI](https://fastapi.tiangolo.com/)
- [LangChain](https://python.langchain.com/)
- [LangGraph](https://langchain-ai.github.io/langgraph/)
- [Milvus](https://milvus.io/)
- [Prometheus](https://prometheus.io/)
- [Celery](https://docs.celeryq.dev/)
- [RabbitMQ](https://www.rabbitmq.com/)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [阿里云 DashScope](https://dashscope.aliyun.com/)

## 许可证

本项目采用 [MIT License](LICENSE)。
