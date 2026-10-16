# 全功能验证与缺陷修复 Implementation Plan

> **For agentic workers:** Use systematic-debugging and test-driven-development for each repair. Independent subsystem repairs are delegated under dispatching-parallel-agents; integrate and review the complete result before delivery.

**Goal:** 验证当前项目的全部已有功能入口，修复测试与故障复现暴露的问题，记录真实环境验证的边界。

**Architecture:** 保留现有 FastAPI、LangGraph、PostgreSQL Outbox、RabbitMQ/Celery 与 dry_run Harness。对执行身份、恢复状态与数据契约做最小修正，新增回归测试；外部集成使用独立测试数据库，不删除用户数据。

**Tech Stack:** Python 3.13、pytest、FastAPI TestClient、LangGraph MemorySaver/PostgreSQL、Docker Compose、RabbitMQ、Milvus、Prometheus。

**Spec:** 用户请求“全部功能进行测试，观察有什么错误然后进行修复”；参考 `D:/项目/智能OnCall Agent系统/项目分析-2026-10-15.md` 的复现问题。

## Global Constraints

- 保留现有 23 项 tracked 修改与 12 个 untracked 条目，不 reset、不整体覆盖用户代码。
- 当前工作目录保留已有未提交功能，基线源码备份在 `D:/项目/智能OnCall Agent系统/tmp/functional-test-baseline-20261015.zip`。
- 所有真实处置仍限定 dry_run；不引入生产基础设施写权限。
- 不输出 `.env` 或凭证，不删除卷、数据库或用户上传文件。
- 每个功能缺陷先通过失败的回归测试证明，再修改代码。
- 不创建提交，避免混入用户此前尚未提交的改动；修复结果在当前目录可直接审查。

## Review Focus

- 相同会话反复运行、同一任务重试与聊天/诊断检查点隔离。
- 旧执行租约失效后继续心跳、写证据、写报告及进入重试。
- 处置在验证或回滚中断时不能重复执行，也不能永久占用预算。
- 工具错误、MCP 文本块、空数据和模拟日志不能冒充真实成功证据。
- 索引失败、重复上传、并发重建时保留有效知识。

## Task 1: 基线与外部依赖

Files: 本文档、测试结果记录；不修改业务逻辑。

- [x] 保存源码基线并执行完整 pytest。
- [x] 基线：172 passed、6 skipped，覆盖率 57.98%；外部端口均未监听。
- [x] 启动并检查 Docker，准备隔离 PostgreSQL/RabbitMQ/Milvus 测试。
- [x] 对模型、CLS、Web、上传、SSE、告警与处置执行可用的真实入口验证。

## Task 2: 诊断图隔离与工具执行（workflow_fixes）

Files: aiops_service.py、agent/aiops/、test_aiops_workflow.py。

- [x] RED：同会话先空告警后 firing、最新扫描覆盖旧扫描、后续工具调用、显式续跑。
- [x] 修复状态重置/图执行命名空间、最新证据判断、受限工具循环。
- [x] GREEN：相关测试通过，保持原有接口兼容。

## Task 3: 任务执行所有权与证据归一化（主执行者）

Files: diagnosis repository/runner/task、DB 模型与迁移、新增回归测试。

- [x] RED：重投递相同 Celery task_id 后，旧执行不能续约或写入；失去租约后停止；失败 MCP 结果不满足证据条件。
- [x] 引入每次领取独立 execution_token，持久化写入与重试必须验证 token。
- [x] 使用独立 diagnosis_id 作为图身份，重试显式恢复。
- [x] GREEN：内存与 PostgreSQL 集成验证，并验证迁移。

## Task 4: 处置恢复（remediation_fixes）

Files: remediation repository/service/task、Harness gateway 与相关测试。

- [x] RED：verifying 可重新观察，rolling_back 中断进入明确受控状态，旧 Worker 无法推进新执行。
- [x] 修复所有中间状态的恢复与执行身份校验，防止重复 execute/rollback。
- [x] GREEN：正常审批、拒绝、过期、重复投递和失败回滚测试通过。

## Task 5: 知识库、评测、日志契约（knowledge_quality_fixes）

Files: vector services、evaluation、service_observability、CLS 示例与相关测试。

- [x] RED：解析/嵌入/插入失败保留旧索引；失败证据不通过质量门禁；本地和官方 CLS 均有明确数据来源。
- [x] 安全替换索引、加强可判定的证据质量检查、统一日志适配契约。
- [x] GREEN：相关测试和全量 Ruff。

## Task 6: API、安全、健康与监控（主执行者）

Files: chat/remediation/auth/health API、security、queue observer、Compose 与监控规则；相关测试。

- [x] RED：聊天失败返回 HTTP 错误，viewer 不得审批，会话资源不能跨用户访问，断开依赖不可返回 ready。
- [x] 修复最小权限与错误/就绪语义，修正 Worker 指标采集和策略/队列配置一致性。
- [x] GREEN：接口、安全和真实监控入口验证。

## Task 7: 集成、复审与交付

- [x] 读取所有子任务结果并检查改动边界与兼容性。
- [x] 执行完整 pytest、Ruff、Bandit、Alembic/Compose 检查与可运行的端到端测试。
- [x] 对新增修复做独立代码审查，修复重要发现并回归。
- [x] 保存功能测试矩阵、修复清单、实际结果和未验证的环境边界。

## Execution ledger

- 基线已备份；原有完整 pytest 通过但六项外部依赖测试跳过。
- Ruling: 直接在当前工作目录修复，因为已有未提交新增功能不能由干净 worktree 自动继承；风险通过源码备份和独占文件分工控制。
- Ruling: 用户已明确授权测试并修复，直接执行计划，不增加计划审批步骤。

### 2026-10-16 最终补测与复审

- PostgreSQL、RabbitMQ、Milvus、Prometheus、Alertmanager 均在独立测试端口/容器运行；原有用户容器、数据库与 `.env` 保留。
- 真实 HTTP/文件/告警/审批/死信结果保存于 `docs/functional-results-2026-10-16.json`；五组共 60 项检查全部通过。
- 独立复审发现并修复处置提案事务所有权、未知执行状态熔断、证据门禁三项缺口；复审处置事务修复未发现重要问题。
- 新增 pending checkpoint 恢复及 asyncio deadline；失败终态重试重新开始，保留明确超时原因。
- 真正的自动告警诊断暴露 request_rate 工具契约缺口及 Executor 原始服务参数丢失，均完成 RED→GREEN；新任务首轮执行完成，18 条证据和报告落库，恢复通知更新原任务。
- Web 实测修复外部 CDN 依赖、错误的 Node 高亮资源，以及历史消息角色/缓存问题；本地依赖保留固定版本、SRI 和许可文件。
- DOCX/PDF/TXT/Markdown 上传及重复上传、允许目录索引均实测成功；真实 429 限流按 Retry-After 等待后完成，无需修改生产限制。
- 审批流程用独立数据库的明确测试 fixture 验证 API/CSRF/Outbox/Worker；执行结果明确为 Dry Run，未调用生产基础设施执行器。
- 初始告警测试因工具契约失败、后续测试因旧任务排队超出等待窗口；失败记录保留在工作区外的测试运行目录，修复后追加消费者并验证最终成功。未将测试窗口超时伪报为产品成功。
- 迁移在独立数据库完成 0007→0006→0007，alembic check 无差异；最终完整 pytest/覆盖率结果见交付报告。
- 测试结束停止本次创建的隔离服务，保留卷与可复查结果；无提交或 PR。

- Final: 418 passed、0 skipped，app覆盖率67.98%，完整镜像构建成功；测试XML、输出及中文报告已保存。生成的9个上传fixture已按精确内容标记清理，用户上传文件保留。
