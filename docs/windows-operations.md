# Windows 本机部署、维护与演练

本手册适用于 Windows 原生 API/MCP + Docker Desktop 数据服务/Worker 的托管运行方式。七项运行保障已接入代码；实际验证范围与尚未验证的部署条件见 [验证记录](windows-operations-verification.md)。处置执行器仍为 Dry Run。单机部署会受宿主机、磁盘与 Docker Desktop 故障影响。

## 1. 首次部署与进程守护

在项目根目录打开 PowerShell，先安装 Python 3.11–3.13、uv、Docker Desktop。官方 CLS 还需要 Node.js/npm。执行 `uv sync --frozen`，从 `.env.example` 创建 `.env`，按 README 配置模型、数据库、RabbitMQ、Milvus 和首次管理员账号。将 `AUTH_ENABLED=true`，为 `API_KEY`、`ALERTMANAGER_WEBHOOK_TOKEN`、`MCP_CLS_TOKEN` 各生成独立且至少 32 字符的随机值；不要提交 `.env`。

```powershell
uv sync --frozen
Copy-Item .env.example .env  # 仅在 .env 尚不存在时执行
notepad .env
# 确认 Docker Desktop 已运行，再执行：
.\scripts\oncall-windows.ps1 start
.\scripts\oncall-windows.ps1 status
.\scripts\oncall-windows.ps1 stop
```

默认代码、配置和数据根目录均为当前项目。需要分离时，每次命令使用相同参数，例如 `-DataRoot 'D:\OnCallData' -ConfigRoot 'D:\OnCallConfig'`；将 `.env` 与 `config` 放在配置根目录。数据根目录保存 `uploads`、日志和 `.runtime\operations`，须位于发布目录之外。发布期间保持这些绝对路径不变。可用 `-PythonExe`、`-DockerExe` 指定可执行文件。

托管启动强制 API 与 Worker 使用 PostgreSQL 共享额度，禁用 API 自动建表。首次启动 Compose 执行迁移/队列初始化；已有发布的再次启动沿用发布记录里的固定 Worker 镜像，不从开发目录重新构建应用。生产迁移前先在副本验证；运行入口不会自动执行数据库降级。

守护器只管理自己创建的进程。退出后按 1/2/4/8/16/30 秒退避，每服务 5 分钟内最多重启 5 次，超限进入 `degraded`。API `/live` 连续三次失败才重启；依赖 `/ready` 失败报告降级。状态、退出码、运行 ID 和各服务日志保存在 `.runtime\operations\runtime`。`stop` 等待同一运行 ID 的完成确认和锁释放，不能把失联当作停机成功。Docker 数据服务和长期 Worker 使用 `unless-stopped` 策略；`stop` 只停止托管原生进程，Docker 栈的维护由备份/发布流程分别控制。

原 `start-windows.bat` / `stop-windows.bat` 可传 `--managed` 进入本流程。直接使用旧启动方式不具备这套守护、维护和发布控制。

如需当前用户登录后自动运行，可显式执行以下命令。任务要求用户登录、Docker Desktop 可用；不是开机前 Windows 服务。本次开发未安装该任务。

```powershell
.\scripts\install-oncall-logon-task.ps1 -WhatIf
.\scripts\install-oncall-logon-task.ps1
# 删除此登录任务：
.\scripts\install-oncall-logon-task.ps1 -Remove
```

## 2. 独立监测与积压告警

在另一个 PowerShell 进程运行独立探针：

```powershell
.\scripts\oncall-windows.ps1 probe -Once
.\scripts\oncall-windows.ps1 probe
# 自定义检查目标：
.\scripts\oncall-windows.ps1 probe -Settings 'D:\OnCallConfig\probe.json'
```

模板为 `config/windows-operations.example.json`，检查 API `/live`、认证 `/ready`、Prometheus 和 Alertmanager。单次检查有界；连续失败 3 次记录故障，连续成功 2 次记录恢复，状态变化写入 `.runtime\operations\probe\events.jsonl`，当前状态写入 `probe.json`。单次退出码 0 表示本次全部成功，1 表示降级，2 表示运行配置错误。探针拒绝重定向，凭据不进入命令行或结果文件。

可将运维包与检查配置复制到另一台 Windows 主机并修改目标地址，使用独立探针进程和结果目录。远程 `/ready` 需要机器凭据及受控 HTTPS 入口。同机探针无法通知宿主机整体断电；还须由外部系统检查探针最后运行时间。

`monitoring/alerts.yml` 的初始阈值：

| 指标 | 触发条件 |
| --- | --- |
| critical 队列 ready 消息 | >10 持续 2 分钟 |
| default 队列 ready 消息 | >50 持续 5 分钟 |
| queued/retrying 最老等待时长 | >120 秒持续 2 分钟 |
| 队列采集最后成功时间 | 超过 60 秒或指标缺失，持续 2 分钟 |

同时保留死信、无消费者、Outbox 滞留等规则。阈值需要按实际流量和容量调整。采集失败不刷新成功时间，避免旧值掩盖故障。运行告警标记 `oncall_route=operations`，不会再次创建诊断任务加重队列。

Alertmanager 的 `operations-local` 接收器目前只保留本地可见告警，**尚未配置异机通知**。上线前为它配置独立邮件/Webhook 接收端，并实际验证故障与恢复送达；不要回调本系统诊断入口。修改规则后使用 `promtool test rules monitoring/operations-rules.test.yml` 验证，再通过既有发布流程重新加载监控配置。

## 3. 冷备份与隔离恢复

备份有停机维护窗口：拒绝新业务写入，等待已接纳 HTTP 请求与持久任务排空，暂停 API/MCP 和 Worker/Publisher，正常停止持久化组件，再归档数据卷和文件。排空超时、非正常停机或归档失败均不能产生成功备份。

```powershell
# 需要本托管守护器正在运行；命令会显示维护窗口提示。
.\scripts\oncall-windows.ps1 backup -Target 'D:\OnCallBackups'
# 检验某份备份，始终恢复到全新的隔离目录/随机 Docker 资源：
.\scripts\oncall-windows.ps1 restore -Backup 'D:\OnCallBackups\备份ID' -Target 'D:\OnCallDrills\restore-001'
# 不读取业务数据库：创建合成数据栈、冷备、恢复并核对内容。
.\scripts\oncall-windows.ps1 drill -Target 'D:\OnCallDrills\synthetic-001'
```

备份覆盖 PostgreSQL、RabbitMQ、Milvus、etcd、MinIO 数据卷、上传文件、配置目录和当前发布标识。Manifest 保存镜像、卷/节点身份、文件数量及 SHA-256；`.env` 不归档，恢复凭据由配置根目录/环境提供。备份内容包含业务数据，应将副本加密保存到独立存储并制定保留周期；本工具未配置异地复制、自动清理或密钥托管。

备份失败也会尝试恢复原来运行的服务，并分别记录重启错误。发现失败时检查阶段日志、原服务状态及维护标记；不要把失败临时目录当作可用备份。目标目录须不存在，归档路径/哈希/重复成员校验失败时拒绝恢复。

恢复仅新建随机且带所有权标记的资源，不引用源卷，不启动业务 Worker。核对数据库记录、RabbitMQ 消息摘要、合成向量查询、上传文件哈希后才返回 `verified`。`drill-report.json` / `restore-report.json` 保存结果和耗时，`ownership.json` 记录本次资源。默认清理本次 Docker 资源并保留报告；`-KeepResources` 仅用于合成演练排错，排查后按所有权清理，禁止全局 `docker system prune`。

合成演练使用最小测试表和合成向量，不等同于全量业务数据库恢复验收。生产上线还应对真实备份做隔离业务校验，包括当前 Alembic 版本、完整业务表、索引版本/权限及实际文档检索。只将实测恢复耗时用于 RTO 记录；RPO 取决于最近成功且可恢复的异地备份时间。

## 4. 共享模型额度

API、诊断 Worker 及其他现有模型工厂调用共享 PostgreSQL 租约、分钟调用次数与输入字节预算。同一供应商账户必须使用相同、不含秘密的 `SHARED_MODEL_QUOTA_SCOPE`。

| 配置 | 默认值 |
| --- | --- |
| `SHARED_MODEL_QUOTA_CONCURRENCY` | 4 |
| `SHARED_MODEL_QUOTA_CALLS_PER_MINUTE` | 60 |
| `SHARED_MODEL_QUOTA_INPUT_BYTES_PER_MINUTE` | 1048576 |
| `SHARED_MODEL_QUOTA_LEASE_SECONDS` | 150 |
| `SHARED_MODEL_QUOTA_RENEW_SECONDS` | 30 |

配置租约必须覆盖模型调用硬期限和安全余量。进程退出不会重置分钟预算，异常调用也不退还分钟预留；共享数据库不可用时拒绝新模型调用。供应商 429 进入共享冷却。额度耗尽的普通 HTTP 请求返回 429 和 `Retry-After`；共享服务故障返回 503。已开始 SSE 的错误事件保留消息字符串，附带 `error_code`、`retry_after` 供前端识别。

这是有效租约数、调用次数和输入字节限制，未提供精确 Token/金额计费，也未覆盖 embedding。失联进程的上游请求可能暂时继续执行，租约过期不意味着供应商实际请求立即停止。后台任务仍遵循原有重试上限和持久预算。

## 5. 发布与回滚

先完成初始迁移并查询实际 `alembic_version`。以下 `20261024_0011` 仅为本次代码迁移头，必须核对当前环境的实际版本。包声明兼容当前 schema 才允许激活；流程不自动迁移或降级数据库。

```powershell
.\scripts\oncall-windows.ps1 package -Version 'v1.0.0' -Schema '20261024_0011'
# 用 package 输出的绝对路径。首次纳入版本指针必须显式 Bootstrap：
.\scripts\oncall-windows.ps1 deploy -Release 'D:\OnCallData\.runtime\operations\releases\v1.0.0' -Bootstrap
# 后续发布：
.\scripts\oncall-windows.ps1 deploy -Release 'D:\OnCallData\.runtime\operations\releases\v1.0.1'
.\scripts\oncall-windows.ps1 rollback
```

示例路径应替换为实际 `DataRoot`。代码包允许包含当前未提交的前端工作，排除秘密、日志和数据；版本名不能覆盖已有目录。发布时安装各版独立锁定依赖，构建并固定 Worker 镜像 ID，在维护排空后同时切换 API/MCP 和四个 Worker。验证所选 Python、代码目录、运行 ID、健康与业务只读接口，通过后退出维护。开发环境需提前具备依赖源、镜像源和构建缓存。

后续版本健康失败会尝试回到上一兼容版并再次冒烟。切换前排空失败则恢复原先运行的容器与维护状态，成功后允许重试。首次 Bootstrap 没有上一版；回退/恢复失败会保留维护状态并在 `deployment.json` 标记人工处理。检查原/新容器、锁、包哈希和 schema 后处理，不能直接删除 journal 假装发布成功。

应用回滚保留数据库中新增业务数据。破坏性 schema 变更需要独立维护方案；不会自动执行 `alembic downgrade` 或拿旧备份覆盖新数据库。当前/上一版、各版虚拟环境和镜像须保留，暂未提供自动保留期清理。

## 6. 任务断线重连与演示

浏览器先通过 `POST /api/aiops/jobs` 和 `Idempotency-Key` 创建持久任务，再通过 `GET /api/aiops/jobs/{id}/events` 订阅。重试创建使用同一主体、同一幂等键和相同请求；请求冲突返回 409。订阅接受 `Last-Event-ID` 或 `after_id`，恢复订阅不会新建任务。

前端保存当前用户任务 ID、创建键、完整已应用事件的游标和展示进度；刷新页面恢复任务，断线指数退避最多 30 秒，终态事件稳定去重，认证失效停止。账号切换清理旧状态。新手动任务仅创建者可订阅，历史手动任务按管理员兼容规则处理，告警任务保持团队可见。断开浏览器不代表取消后台任务。

可演示：发起诊断 → 浏览器临时断网 → 恢复网络/刷新 → 继续同一任务并显示同一最终报告。生产演示会调用已配置模型并产生费用；自动化回归使用模拟提供者和合成事件。

## 7. 上线验收口径

这些能力让项目具备可维护、可恢复、可观测的 Windows 单机运行基础。面试时可以展示守护器故障恢复、探针故障/恢复记录、冷备恢复内容比对、共享额度并发竞争、失败版本回退和 SSE 游标重连，并明确哪些是自动化实测、哪些只完成离线验证。

企业生产还需要按实际业务补齐容量/压测与 SLO、异机告警送达、异地加密备份与定期真实恢复、数据库/宿主机高可用、凭据轮换和组织权限审计、真实基础设施适配器的独立验收。是否能上线取决于这些验收证据及风险接受，不能仅凭具备脚本就宣称企业级高可用。
