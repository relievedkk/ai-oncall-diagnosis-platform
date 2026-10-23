# 安全与业务边界修复记录（2026-10-23）

本轮对应工作区《代码风险复查-2026-10-23.md》中的八项发现。修复归档于 `fix/security-boundaries-20261023` 分支。

## 修复范围

| 原问题 | 修复后的行为 | 主要回归证据 |
| --- | --- | --- |
| 原始 CLS 工具绕过服务目录范围，可能读取不应开放的配置 | Agent 只绑定三个明确允许的 Monitor 工具；日志通过本地目录包装器访问。CLS 网关只公开 `SearchLog`，校验目录中的 Region/TopicId、服务查询式、时间范围和 Limit，拒绝额外参数和其他工具 | `test_mcp_client.py`、`test_cls_gateway_policy.py` |
| 敏感日志、过大证据进入模型，任务重试绕过总预算 | 工具输出在检查点、流和模型复用前脱敏并限制大小；实际模型请求有输入上限；模型调用和累计输入预算保存在数据库，重试不重置，旧执行者不能扣用新执行者预算。关闭 SDK 隐式重试 | `test_model_data_boundary.py`、`test_diagnosis_boundaries_postgres.py` |
| 旧 resolved 通知误关新告警；无 fingerprint 时标签碰撞 | 恢复通知必须匹配告警开始时刻和 fingerprint；无 fingerprint 时匹配完整标签。旧告警周期不能结束新周期 | `test_diagnosis_boundaries.py`、PostgreSQL 对应回归 |
| 非法结束时间导致异常、批次部分生效，字段超过数据库容量 | 整批模型验证在任何入队副作用前完成；要求合法带时区 RFC3339，校验结束时间和字段存储边界；保留 firing 的 Alertmanager 零结束时间兼容 | `test_diagnosis_boundaries.py` |
| 同一 UTC 时刻的不同表示生成重复任务 | 按规范化 UTC 时刻去重，保留纳秒精度；兼容历史任务和已有公开 ID | `test_diagnosis_boundaries.py`、PostgreSQL 对应回归 |
| Argon2 同步计算阻塞异步登录 | 密码计算转移到有界线程池；两个计算线程、最多八个排队任务；饱和返回 503 和 Retry-After。请求取消后仍占用计算容量，直到真实计算完成 | `test_auth_workers.py` |
| 切换账号后旧页面恢复上一用户缓存 | 登录、退出广播身份版本；旧页面清空私有 UI、中止请求和流、拒绝缓存回退；退出清除对应用户缓存 | `frontend_history.test.cjs` |
| 历史任务无界返回，流频繁读取全部证据 | 列表分页返回摘要，默认 50、最多 100；流每批读取最多 100 条增量证据，耗尽已有证据后再发送终态 | `test_diagnosis_boundaries.py`、`test_diagnosis_resume_deadline.py` |

另外，历史任务的公开详情、流证据和终态输出增加读取脱敏；修复了长非 URL 文本导致脱敏表达式重复回溯的问题。

## 模型与证据默认限制

| 配置 | 默认值 |
| --- | --- |
| `MODEL_MAX_INPUT_BYTES` | 65,536 字节 |
| `TOOL_RESULT_MAX_BYTES` | 16,384 字节 |
| `DIAGNOSIS_MAX_MODEL_CALLS` | 每个诊断 24 次 |
| `DIAGNOSIS_MAX_MODEL_INPUT_BYTES` | 每个诊断累计 262,144 字节 |

超大工具结果返回明确的截断标志和不完整预览，不能当作完整授权证据。先对完整字符串脱敏，再截断，覆盖超长 JSON 和未闭合私钥。`*_token` 凭证字段脱敏，`input_tokens` 等用量指标保留。工具和结构化输出调用也经过模型传输边界；Pydantic `json_schema` 按 SDK 展开后的 schema 计量。

## 数据库与兼容性

新增迁移 `20261023_0010`（前置 `20261023_0009`），为任务添加 `model_calls`、`model_input_bytes`，默认均为零。新进程启动前需执行 `alembic upgrade head`；Compose 的迁移服务负责执行。迁移不删除现有任务，也不重写历史任务 ID。

Webhook 对非法时间、超长身份字段采用 422 拒绝。恢复通知必须提供合法 `endsAt >= startsAt`；firing 的 `0001-01-01T00:00:00Z` 结束时间仍可接受。列表接口保留顶层 `jobs`，支持 `limit=1..100`、`offset=0..100000`，返回摘要；完整单任务详情仍由详情接口提供。

## 验证

- 提交前重新运行默认 Python 测试：**632 passed，38 skipped**（22.58 秒）。跳过项不计为通过。
- 真实临时 PostgreSQL 数据库迁移、并发预算、诊断/处置持久化、检查点及隔离 Milvus 集成：**44 passed**。测试数据库和临时集合已清理；13 条警告为现有 PyMilvus ORM API 弃用提示。
- Node 前端回归：**17 passed**；两个 JavaScript 文件语法检查通过。
- Ruff、Bandit、`git diff --check` 通过。
- 独立复核指出的截断脱敏和结构化 schema 计量两项问题均增加失败回归后修复；限定复核 **18 passed**，未发现该范围内的剩余交付阻塞。
- 本地容器重新构建成功，API、CLS 网关已重启；实际数据库版本为 `20261023_0010`，两个预算字段已确认存在；工作进程镜像已加载新模型边界。`/login`、`/live`、`/ready` 均为 200；登录和 CSRF 保护退出成功；分页非法边界及含非法时间的整批 Webhook 返回 422，探测前后任务数量相同。实际 MCP 列表为 CLS 1 个、Monitor 3 个工具。

测试使用合成日志、假模型及离线 HTTP 传输，没有查询真实云日志或调用付费模型。浏览器跨账号竞态通过 Node 环境回归，未进行真实多浏览器、多账号端到端测试。

## 仍需了解的边界

脱敏覆盖常见凭证字段、已配置秘密及可识别文本格式，不能保证识别任意个人信息或未知秘密格式。历史数据增加读取脱敏，没有批量清除既有数据库、检查点、备份或日志文件中的内容。调用次数和输入字节预算限制资源使用，不等同于精确金额预算。提示词注入不能靠关键词脱敏完全解决，仍依赖工具权限、范围校验和既有处置审批边界。
