# GitHub 上传清单

仓库公开源码、测试、运行方法和结果摘要。本地运行数据与公开源码分别保存；忽略规则不改变已经进入 Git 历史的内容。

## 适合上传

| 文件或目录 | 说明 |
| --- | --- |
| `app/`、`mcp_servers/`、`oncall_ops/`、`oncall_logging/` | 产品实现与运维工具 |
| `static/`、`migrations/`、`monitoring/`、`tools/`、`scripts/` | 界面、迁移、监控和可复用脚本；历史续跑脚本保留以支持现有回归 |
| `tests/` | 自动化回归；合成密钥字符串、假 PEM 和假模型响应是测试夹具，不是真实凭据 |
| `evals/`、`aiops-docs/` | 项目内置合成案例、演示期望和示例手册；它们不是独立盲测集 |
| `pyproject.toml`、`uv.lock`、`requirements-rcaeval.txt`、Docker 文件、`.github/` | 依赖、构建、质量检查与 CI |
| `.env.example`、`config/windows-operations.example.json` | 仅包含占位值的配置模板 |
| `README.md`、`docs/`、`LICENSE` | 当前使用说明、公开评测摘要、必要历史归档和许可证 |

`static/vendor/` 中的第三方文件与其许可证应一起保留。

## 脱敏或精简后上传

| 材料 | 公开形式 |
| --- | --- |
| 服务目录、日志映射 | `config/service_catalog.json` 保留结构和示例，云日志资源 ID 使用占位 UUID；真实映射保存本地 |
| 完整验收与实验报告 | 保留版本、方法、分母、失败、跳过项和限制；去掉机器路径、账号、逐条原始遥测、聊天审批过程与内部运行细节 |
| 截图与导出文件 | 先检查用户名、邮箱、密钥、内网地址、日志正文及答案信息；原始截图不直接放进源码仓库 |
| 旧协议记录 | 放在 `docs/archive/evaluation/`，标注历史范围，当前入口统一从评测导航进入 |

本次公开结果为[证据与分组留出摘要](evaluation/evidence-and-holdout.md)，明确保留35/36通过、1例包体超限的失败结果，不将预检当作模型准确率。

## 仅本地保留

| 材料 | 位置或规则 |
| --- | --- |
| 模型与云服务凭据、真实连接串、私钥 | `.env`、`.env.*`（`.env.example` 除外）、密钥文件 |
| 原始遥测、冻结证据包、模型原始响应、请求日志和 SQLite/备份 | `.runtime/`、`rca_experiments/`、`backups/`、`output/`；Parquet/Arrow 默认忽略 |
| 留出答案、真实源案例名与匿名映射 | `sealed/`、`*-sealed/`，包括公开数据下载后形成的本地测试划分 |
| 额度占用、退役状态和请求收据 | 用户目录 `~/.oncall-evaluation/`；作为本地恢复材料备份，不提交 |
| 开发计划、AI 执行过程、审批与完整验收记录 | `.local/development-notes/`、`.superpowers/`；旧 `docs/superpowers/` 入口也被忽略 |
| 真实服务目录 | `.local/config/` 或 `config/*.local.json`；通过现有 `SERVICE_CATALOG_PATH` 指定，容器部署另同步挂载 |
| 面试、简历及个人项目笔记 | 顶层个人 Markdown；已有面试/简历命名和项目笔记已加入共享忽略规则 |
| 虚拟环境、日志、上传文件、数据卷、覆盖率和隔离工作区 | `.venv/`、`logs/`、`uploads/`、`volumes/`、`htmlcov/`、`.worktrees/` |

这些文件不上传通常是为了保护运行状态、测试独立性、隐私或避免仓库膨胀；不能简单把所有 JSON、测试期望或匿名案例都当作敏感文件。

## 提交时怎么检查

```powershell
git status --short
git diff --stat
git diff --cached --name-only
git diff --cached --check
git check-ignore .env.production .runtime/example.json .local/config/service_catalog.json
```

以 Git 中实际跟踪和暂存的文件为准，不要把整个本地项目目录打包上传。`.gitignore` 不会自动取消已跟踪文件，也不会清理历史提交。

本次整理只处理当前分支的公开文件范围，没有推送或重写历史。云资源 ID 已替换为占位值；若历史曾包含真实凭据，应另行撤销或轮换并处理历史，不能仅修改当前文件。

如果希望只发布整理后的文件、完全不携带旧提交，可从当前提交生成 `git archive` 源码快照，解压后在新的空仓库中初始化提交。正常推送现有分支仍会携带其可达历史，不能将源码快照与历史清理混为一谈。
