# Windows 运行保障验收记录

验收日期：2026-10-24（Asia/Shanghai）

## 结论

Windows 本机部署范围内的七项运行保障已经实现并通过代码回归与隔离集成验证：进程守护、独立监测、备份恢复演练、积压告警、共享额度、发布回滚、任务重连。该结论不等同于异机高可用或生产上线批准。

## 本次验收证据

- Python 全量（完成 Alembic 迁移的独立 PostgreSQL 测试库）：`858 passed, 4 skipped in 84.13s`，覆盖率 `78.39%`。4 个跳过项为显式 Docker/本地依赖门控；临时测试库已在验证后删除。
- 浏览器/前端：Node `28 passed, 0 failed`。
- 最终安全边界定向回归：`53 passed`，覆盖候选 Worker 精确 ID、回滚只读数据库证明、MCP 外层进程超时、RabbitMQ 队列抓取完整性，以及 Milvus 必须到达 `Loaded(3)`。
- 共享额度真实 PostgreSQL 隔离集成：`3 passed`；临时数据库容器随后删除。
- 最终真实 Docker 冷备恢复：`1 passed in 134.71s`。源栈重启后内容一致；恢复栈 PostgreSQL、RabbitMQ、Milvus 检查均为 true；上传文件校验通过；无清理错误、无保留资源。
- 恢复内容：PostgreSQL 5 张合成业务表及 Alembic 版本指纹一致；RabbitMQ 1 条持久消息指纹一致；Milvus 2 行向量且最近邻样本为 `id=1/tag=synthetic`。
- Prometheus：真实 `promtool test rules monitoring/operations-rules.test.yml` 返回 `SUCCESS`。
- 配置/脚本：两份 Compose 配置、两份 PowerShell 脚本解析、Python `compileall`、Ruff、Bandit 高严重度扫描、`git diff --check` 均通过。
- Windows 托管启动复验：标准启动返回 ready，启动命令退出后 API、CLS、Monitor 继续运行且重启次数均为 0；API/MCP 联合就绪检查通过。
- Worker 目录复验：4 个 Worker 均使用镜像内 `/app/config/service_catalog.json`，各加载 1 条目录记录，且不存在 `/app-config/config` Windows bind mount；三条业务队列各有 1 个消费者。
- 启动失败边界：120 秒绝对期限会约束每次 HTTP/MCP 调用并拒绝迟到成功；首次状态发布失败会清理已启动子进程，周期性状态写入冲突则在下一轮重试。

最终恢复报告位于 `.runtime/task5-docker-final2/test_real_isolated_cold_backup0/drill/drill-report.json`。它是本地验收产物，不应作为长期备份保存。

## 已验证能力

1. 稳定状态目录、单实例锁、子进程身份与退避守护；停止必须确认同一运行实例、子进程退出和锁释放。
2. 与业务初始化解耦的探针；先校验本机监听者身份，再发送凭据；MCP initialize/list-tools 由外层子进程硬超时约束。
3. 冷备前关闭准入、排空请求/持久任务、停写并检查卷写入者；恢复只创建带随机所有者标签的新资源并按内容校验。
4. 队列积压、指标陈旧与本地接收路由规则；规则行为由 promtool 验证。
5. PostgreSQL 共享额度租约、并发上限、幂等释放及公开重试协议。
6. 不可变发布指针和 Worker 镜像；失败候选回滚保持准入关闭，绑定原候选 Worker ID，并用上一版本解释器通过数据库强制只读连接证明兼容与零活动持久任务。
7. 持久诊断任务、幂等创建、事件游标重连、刷新恢复、终态防回退和账号隔离。

## 上线边界与仍需现场验收

- 当前是单台 Windows + Docker Desktop 方案，不提供主机故障、高可用、跨机容灾或异地备份。
- 演练使用合成 schema，未在生产数据副本上执行完整迁移/回滚；上线前应使用脱敏生产副本再演练。
- 已验证发布/回滚控制逻辑，但未替用户执行真实业务版本的冻结安装、镜像构建和生产 Worker 切换。
- Alertmanager 仍是本机可见接收器；上线前必须接入独立邮件/Webhook/值班平台并实测故障与恢复通知。
- 登录启动任务脚本已提供并通过解析，但未在 Windows 任务计划程序中安装；需用实际服务账号安装并做重启验收。
- 还需按企业流程补齐密钥托管、最小权限账号、备份介质与保留策略、容量压测、SLO、值班手册、安全扫描和变更审批。

因此，本项目已从“只有业务功能的简单 demo”提升为“具备本机可演练运行保障的上线候选”；完成上述现场验收前，不应宣称为高可用企业生产系统。
