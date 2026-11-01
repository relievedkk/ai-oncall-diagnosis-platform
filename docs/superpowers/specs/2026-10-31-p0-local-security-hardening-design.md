# P0 本地安全加固设计

> 合并审查更新：原在线轮换方案未达到掉电恢复和全消费者重启要求，已从交付中移除；当前安全边界与后续迁移范围以 `2026-11-01-p0-security-merge-review-design.md` 为准。

## 目标

在不引入 K8s/线上部署范围的前提下，消除当前单机运行环境的三个 P0 风险：可被同机普通账户读取或修改的 `.env`、PostgreSQL/RabbitMQ/MinIO 默认凭据、已确认存在安全公告的 Python 依赖。

## 约束与非目标

- 保留现有 PostgreSQL、RabbitMQ、Milvus/MinIO 数据卷，不重建或清空数据。
- 凭据不得出现在命令输出、日志、Git 或进程命令行中。
- 本次只处理 P0，不扩展到 K8s、SSO、完整多租户或 P1/P2 输入预算。
- 继续支持 Windows 本机 HTTP 开发；不把 `SESSION_COOKIE_SECURE` 强制改为 `true`。

## 方案

采用数据保留、默认拒绝启动的方案：

1. 新配置由 `app.local_env` 生成彼此独立的 PostgreSQL、RabbitMQ、MinIO 强随机凭据，并同步派生 `DATABASE_URL`、`CELERY_BROKER_URL`。
2. 已存在默认凭据和持久卷时拒绝启动；在线轮换方案已被否决，因为它缺少可持久恢复的事务日志和全消费者重启。旧环境必须在停服、冷备后使用另行设计的离线迁移流程。
3. Windows 上把 `.env` ACL 改为当前用户、SYSTEM 与 Administrators 可访问，禁用继承；非 Windows 使用 `0600`。
4. Compose 删除基础服务凭据的默认回退，并把 MinIO 凭据显式传给 Milvus。
5. 将 `pypdf` 提升到 `>=6.21.4`，并为传递依赖增加 `authlib>=1.9.1`、`joserfc>=1.7.6` 安全下限，重新生成锁文件。

## 验收

- 单元测试覆盖弱/默认凭据拒绝、连接串同步、独立随机值、ACL 加固调用和启动 fail-closed。
- Compose 配置在缺少必需凭据时失败，在安全配置下能解析。
- 当前 `.env` 不再含默认/弱凭据，ACL 不再授予 Users/Authenticated Users 权限。
- PostgreSQL、RabbitMQ、MinIO、Milvus 与应用健康检查通过。
- 全量可运行测试、Bandit 与 `pip-audit` 无 P0 回归；任何无法完成的全量测试必须如实记录。
