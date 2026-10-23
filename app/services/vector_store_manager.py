"""向量存储管理器 - 封装 Milvus VectorStore 操作"""

import json
import re
import uuid
from contextlib import contextmanager
from threading import RLock

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.core.vector_lock import vector_operation_lock
from app.services.vector_embedding_service import vector_embedding_service

# 统一使用 biz collection
COLLECTION_NAME = "biz"


class KnowledgeRetrievalError(RuntimeError):
    """Retrieval failed; this is distinct from a successful empty result."""


class IndexCommitUncertain(RuntimeError):
    """Vectors are durable but the coordinator commit needs reconciliation."""


class VectorStoreManager:
    """向量存储管理器"""

    # Thread lock plus a PostgreSQL transaction lock shared by every worker.
    _operation_lock = RLock()
    _initialization_lock = RLock()

    def __init__(self):
        """初始化向量存储管理器"""
        self.vector_store: Milvus | None = None
        self.collection_name = COLLECTION_NAME

    def _distributed_lock(self):
        return vector_operation_lock(self.collection_name)

    def _distributed_read_lock(self):
        return vector_operation_lock(self.collection_name, shared=True)

    @contextmanager
    def _read_guard(self):
        with self._distributed_read_lock() as connection:
            yield connection

    @contextmanager
    def _operation_guard(self, connection=None):
        lock = (
            self._distributed_lock()
            if connection is None
            else vector_operation_lock(self.collection_name, connection=connection)
        )
        with self._operation_lock, lock as connection:
            yield connection

    def _initialize_vector_store(self):
        with self._initialization_lock:
            return self._create_vector_store()

    def _create_vector_store(self):
        """初始化 Milvus VectorStore"""
        if self.vector_store is not None:
            return self.vector_store
        try:
            # 必须在 PyMilvus / langchain_milvus 访问 Collection 之前建立连接，
            # 否则会出现 ConnectionNotExistException: should create connection first.
            # （模块导入时就会执行此处，早于 FastAPI lifespan 中的 milvus_manager.connect）
            _ = milvus_manager.connect()

            connection_args = {
                # Newer pymilvus MilvusClient versions use ``uri``; passing only
                # host/port can silently fall back to localhost inside containers.
                "uri": f"http://{config.milvus_host}:{config.milvus_port}",
            }

            # 创建 LangChain Milvus VectorStore
            # 使用 biz collection，字段映射：text_field -> content, vector_field -> vector
            self.vector_store = Milvus(
                embedding_function=vector_embedding_service,
                collection_name=self.collection_name,
                connection_args=connection_args,
                auto_id=False,  # 使用自定义 id
                drop_old=False,
                text_field="content",  # 文本内容存储到 content 字段
                vector_field="vector",  # 向量存储到 vector 字段
                primary_field="id",  # 主键字段
                metadata_field="metadata",  # 元数据字段
            )

            logger.info(
                f"VectorStore 初始化成功: {config.milvus_host}:{config.milvus_port}, "
                f"collection: {self.collection_name}"
            )
            return self.vector_store

        except Exception as e:
            logger.error(f"VectorStore 初始化失败: {e}")
            raise

    def add_documents(self, documents: list[Document]) -> list[str]:
        if not documents:
            return []
        embeddings = vector_embedding_service.embed_documents(
            [doc.page_content for doc in documents]
        )
        with self._operation_guard():
            return self._add_documents(documents, embeddings)

    def _add_documents(self, documents: list[Document], embeddings: list[list[float]]) -> list[str]:
        """
        批量添加文档到向量存储（自动批量向量化）

        Args:
            documents: 文档列表

        Returns:
            List[str]: 文档 ID 列表
        """
        if not documents:
            return []
        try:
            import time
            import uuid

            start_time = time.time()

            # 为每个文档生成唯一 id（因为 auto_id=False）
            ids = [str(uuid.uuid4()) for _ in documents]

            # LangChain Milvus 的 add_documents 会自动调用 embedding_function
            # 并进行批量处理，性能更好
            vector_store = self._initialize_vector_store()
            result_ids = vector_store.add_embeddings(
                texts=[doc.page_content for doc in documents],
                embeddings=embeddings,
                metadatas=[doc.metadata for doc in documents],
                ids=ids,
                timeout=config.milvus_timeout / 1000,
            )

            elapsed = time.time() - start_time
            logger.info(
                f"批量添加 {len(documents)} 个文档到 VectorStore 完成, "
                f"耗时: {elapsed:.2f}秒, 平均: {elapsed / len(documents):.2f}秒/个"
            )
            return result_ids
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def replace_documents(
        self, file_path: str, documents: list[Document], *, coordinator=None, after_commit=None
    ) -> list[str]:
        """Persist a complete version before retiring captured old primary keys.

        Failed embedding or a partial insert removes only the new version's IDs.
        Parsing happens in the caller before this operation. A flush failure also
        leaves the old version intact. This is recoverable replacement, not a
        distributed Milvus transaction; readers outside this manager can briefly
        observe both versions during a successful replacement.
        """
        externally_owned = coordinator is not None
        if externally_owned and after_commit is None:
            raise ValueError("An external transaction requires deferred retirement")
        if not documents or any(not doc.page_content.strip() for doc in documents):
            raise ValueError("Cannot replace an index with empty document content")
        # Remote model calls happen before the collection lock. Existing readers
        # remain available throughout vector preparation, even for large uploads.
        embeddings = vector_embedding_service.embed_documents(
            [doc.page_content for doc in documents]
        )
        if len(embeddings) != len(documents):
            raise ValueError("Incomplete document embeddings")
        prepared = False
        try:
            with self._operation_guard(coordinator) as coordinator:
                store = self._initialize_vector_store()
                collection = milvus_manager.get_collection()
                source_expr = f'metadata["_source"] == {json.dumps(file_path, ensure_ascii=False)}'
                old_ids = [
                    row["id"]
                    for row in collection.query(
                        expr=source_expr,
                        output_fields=["id"],
                        consistency_level="Strong",
                    )
                ]
                version = str(uuid.uuid4())
                ids = [str(uuid.uuid4()) for _ in documents]
                staged = [
                    Document(
                        page_content=doc.page_content,
                        metadata={
                            **doc.metadata,
                            "_source": file_path,
                            "_index_version": version,
                            "_version_coordinated": True,
                        },
                    )
                    for doc in documents
                ]
                try:
                    inserted = store.add_embeddings(
                        texts=[doc.page_content for doc in staged],
                        embeddings=embeddings,
                        metadatas=[doc.metadata for doc in staged],
                        ids=ids,
                        timeout=config.milvus_timeout / 1000,
                    )
                    if set(inserted) != set(ids):
                        raise RuntimeError("Vector store did not acknowledge every new document")
                    collection.flush()
                    if coordinator is not None:
                        coordinator.execute(
                            "INSERT INTO knowledge_document_versions (collection_name, source, version) "
                            "VALUES (%s, %s, %s) ON CONFLICT (collection_name, source) "
                            "DO UPDATE SET version = EXCLUDED.version",
                            (self.collection_name, file_path, version),
                        )
                    prepared = True
                except Exception:
                    try:
                        collection.delete(f"id in {json.dumps(ids)}")
                        collection.flush()
                    except Exception:
                        logger.exception("Failed to clean staged index version {}", version)
                    raise
        except Exception as exc:
            if prepared:
                # COMMIT may have reached PostgreSQL even if its reply was lost.
                # Keep both versions and the selected upload manifest for recovery.
                raise IndexCommitUncertain("索引提交结果待协调，请重建当前文档索引") from exc
            raise

        # The new version is now committed. Cleanup is not a rollback boundary;
        # readers consult PostgreSQL and cannot serve retired/orphaned chunks.
        def retire():
            if old_ids:
                try:
                    collection.delete(f"id in {json.dumps(old_ids)}")
                    collection.flush()
                except Exception:
                    logger.exception("Committed index {}; retired chunks need cleanup", version)

        if externally_owned:
            after_commit.append(retire)
        else:
            retire()
        return ids

    def _current_search(self, store, embedding, k, coordinator, expr=None):
        limit = k
        while True:
            raw = store.similarity_search_by_vector(
                embedding,
                k=limit,
                timeout=config.milvus_timeout / 1000,
                consistency_level="Strong",
                **({"expr": expr} if expr else {}),
            )
            sources = list(
                {doc.metadata.get("_source") for doc in raw if doc.metadata.get("_source")}
            )
            versions = {}
            if coordinator is not None and sources:
                versions = dict(
                    coordinator.execute(
                        "SELECT source, version FROM knowledge_document_versions "
                        "WHERE collection_name = %s AND source = ANY(%s)",
                        (self.collection_name, sources),
                    ).fetchall()
                )
            current = []
            for doc in raw:
                metadata = doc.metadata
                # Old uploads irreversibly lost their original names. Quarantine
                # those vectors without deleting files; reupload restores identity.
                if not metadata.get("_document_id") and re.fullmatch(
                    r"[0-9a-f]{32}\.(txt|md|pdf|docx)",
                    str(metadata.get("_file_name", "")),
                ):
                    continue
                active = versions.get(metadata.get("_source"))
                if active and metadata.get("_index_version") != active:
                    continue
                # Uploaded chunks with no commit record are uncommitted orphans.
                if (
                    coordinator is not None
                    and (metadata.get("_document_id") or metadata.get("_version_coordinated"))
                    and not active
                ):
                    continue
                current.append(doc)
            if len(current) >= k or len(raw) < limit:
                return current[:k]
            if limit >= 512:
                raise KnowledgeRetrievalError("历史索引需要整理，无法可靠获取当前版本")
            limit = min(limit * 4, 512)

    def delete_by_source(self, file_path: str) -> int:
        with self._operation_guard():
            return self._delete_by_source(file_path)

    def _delete_by_source(self, file_path: str) -> int:
        """
        删除指定文件的所有文档

        Args:
            file_path: 文件路径

        Returns:
            int: 删除的文档数量
        """
        try:
            # 使用 milvus_manager 获取已连接的 collection
            collection = milvus_manager.get_collection()

            # metadata 是 JSON 字段，使用 JSON 路径查询语法
            # _source 是文档的来源文件路径
            expr = f'metadata["_source"] == {json.dumps(file_path, ensure_ascii=False)}'

            result = collection.delete(expr)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(f"删除文件旧数据: {file_path}, 删除数量: {deleted_count}")
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧数据失败 (可能是首次索引): {e}")
            return 0

    def get_vector_store(self) -> Milvus:
        """
        获取 VectorStore 实例

        Returns:
            Milvus: VectorStore 实例
        """
        return self._initialize_vector_store()

    def similarity_search(self, query: str, k: int = 3) -> list[Document]:
        """
        相似度搜索

        Args:
            query: 查询文本
            k: 返回结果数量

        Returns:
            List[Document]: 相关文档列表
        """
        try:
            embedding = vector_embedding_service.embed_query(query)
            with self._read_guard() as coordinator:
                vector_store = self._initialize_vector_store()
                services = set(
                    re.findall(
                        r"(?<![a-z0-9-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*-api(?![a-z0-9-])",
                        query.lower(),
                    )
                )
                if len(services) == 1:
                    service = next(iter(services))
                    try:
                        scoped = self._current_search(
                            vector_store,
                            embedding,
                            k,
                            coordinator,
                            expr=f'metadata["_file_name"] like "%{service}%"',
                        )
                        scoped = [
                            doc
                            for doc in scoped
                            if re.search(
                                rf"(?<![a-z0-9-]){re.escape(service)}(?![a-z0-9-])",
                                str(doc.metadata.get("_file_name", "")).lower(),
                            )
                        ]
                        if scoped:
                            return scoped
                    except Exception:
                        logger.warning("来源范围检索失败")
                        raise
                docs = self._current_search(vector_store, embedding, k, coordinator)
            logger.debug("相似度搜索完成: 查询长度={}, 结果数={}", len(query), len(docs))
            return docs
        except Exception as e:
            logger.error(f"相似度搜索失败: {e}")
            raise KnowledgeRetrievalError("知识检索服务暂不可用，请稍后重试") from e


# 全局单例
vector_store_manager = VectorStoreManager()
