# 真实 Milvus 功能验证（2026-10-15）

验证时间：2026-10-15 22:33–22:34，Asia/Shanghai。测试子进程退出码为 `0`。

## 环境与范围

- 使用主任务提供的隔离 Docker Milvus：`127.0.0.1:29530`。
- 实际客户端版本：`pymilvus 3.0.2`、`langchain-milvus 0.4.0`。
- 在子进程导入配置前设置 `MILVUS_HOST=127.0.0.1`、`MILVUS_PORT=29530`。
- 直接执行项目实际的 `VectorStoreManager.replace_documents(file_path, documents)`；没有用内存存储替代 Milvus。
- 向量由测试中的确定性 SHA-256 算法生成，维度为 1024；替换 `vector_store_manager` 模块的 embedding 实例，不调用 DashScope。这项验证证明存储流程可用，不能证明外部 embedding 服务或语义检索质量。
- 唯一测试来源：`__milvus_functional_check__/f8796e90-145d-4c47-afa3-55a95c5aafd1`。
- 没有删除 collection，没有进行全量或其他来源的数据删除。初始化通过项目现有管理器创建、建立索引并加载隔离环境中此前不存在的 `biz` collection。

## 结果

| 场景 | 验证方式 | 结果 |
| --- | --- | --- |
| 首次插入 | 实际替换接口写入两个 Document；按来源 Strong query 比较全部主键 | 通过，2 条 |
| 正常替换 | 同一来源替换为一个 Document；比较新主键、内容并确认旧主键均消失 | 通过，1 条 |
| embedding 失败保留旧索引 | 注入明确的 embedding RuntimeError；再次 Strong query 比较此前主键和内容 | 通过，旧版本完整保留，1 条 |
| 实际向量检索 | 实际 LangChain Milvus `similarity_search`，使用唯一来源表达式过滤 | 通过，返回唯一替换后的 Document，来源与内容一致 |
| 来源限定清理 | `delete_by_source` 仅删除本次来源，flush 后 Strong query 验证 | 通过，本次来源剩余 0 条 |

## 实际接口与边界

本次真实执行了 ORM `Collection.query(..., consistency_level="Strong")`、LangChain `Milvus.add_documents(..., ids=...)`、`Collection.flush()`、限定主键的 `Collection.delete(...)` 以及带来源过滤的 `similarity_search(..., expr=...)`。没有发现客户端、schema、索引或 JSON 字段表达式不兼容，因此没有为本次实测修改生产代码。

运行产生了 PyMilvus ORM API 和 LangChain `metadata_field` 的弃用警告；当前流程成功，后续依赖升级仍需关注这些接口。结束时调用现有 `milvus_manager.close()`，会释放 collection 并断开本子进程连接；随后测试应重新初始化并加载 collection。

替换操作使用当前进程内的共享锁，不提供跨 worker 的事务或互斥。写入新版本与删除旧版本是两次 Milvus 操作，绕过管理器锁的读取者在成功替换期间可能短暂看到两个版本。此次实测覆盖单进程正常替换和 embedding 失败；进程崩溃、网络中断、真实服务端部分写入失败与跨进程竞争不在本次实测范围内。
