"""向量索引服务模块"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.vector_lock import vector_operation_lock
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import IndexCommitUncertain, vector_store_manager


def upload_document_lock(document_id: str):
    return vector_operation_lock(f"upload:{document_id}")


def is_internal_upload_path(path: str | Path) -> bool:
    return any(part in {".objects", ".documents", ".tmp"} for part in Path(path).resolve().parts)


class IndexingResult:
    """索引结果类"""

    def __init__(self):
        self.success = False
        self.directory_path = ""
        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None
        self.error_message = ""
        self.failed_files: dict[str, str] = {}

    def increment_success_count(self):
        """增加成功计数"""
        self.success_count += 1

    def increment_fail_count(self):
        """增加失败计数"""
        self.fail_count += 1

    def add_failed_file(self, file_path: str, error: str):
        """添加失败文件"""
        self.failed_files[file_path] = error

    def get_duration_ms(self) -> int:
        """获取耗时（毫秒）"""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "failed_files": self.failed_files,
        }


class VectorIndexService:
    """向量索引服务 - 负责读取文件、生成向量、存储到 Milvus"""

    def __init__(self):
        """初始化向量索引服务"""
        self.upload_path = "./uploads"
        logger.info("向量索引服务初始化完成")

    def index_directory(self, directory_path: str | None = None) -> IndexingResult:
        """
        索引指定目录下的所有文件

        Args:
            directory_path: 目录路径（可选，默认使用配置的上传目录）

        Returns:
            IndexingResult: 索引结果
        """
        result = IndexingResult()
        result.start_time = datetime.now()

        try:
            # 使用指定目录或默认上传目录
            target_path = directory_path if directory_path else self.upload_path
            dir_path = Path(target_path).resolve()
            if is_internal_upload_path(dir_path):
                raise ValueError("上传内部存储目录不能直接索引，请索引 uploads 根目录")

            if not dir_path.exists() or not dir_path.is_dir():
                raise ValueError(f"目录不存在或不是有效目录: {target_path}")

            result.directory_path = str(dir_path)

            # 获取所有支持的文件
            files = []
            for ext in ["*.txt", "*.md", "*.pdf", "*.docx"]:
                files.extend(dir_path.glob(ext))
            if dir_path == Path(self.upload_path).resolve() or (dir_path / ".documents").is_dir():
                legacy = [
                    path
                    for path in files
                    if re.fullmatch(r"[0-9a-f]{32}\.(txt|md|pdf|docx)", path.name)
                ]
                if legacy:
                    logger.warning(
                        "跳过 {} 份缺少原始文件名的历史上传对象，请按原文件名重新上传迁移",
                        len(legacy),
                    )
                files = [path for path in files if path not in legacy]
            # Immutable upload objects are below .objects; only selected versions
            # in the durable catalog are rebuilt, never every historical blob.
            files.extend((dir_path / ".documents").glob("*.json"))

            if not files:
                logger.warning(f"目录中没有找到支持的文件: {target_path}")
                result.total_files = 0
                result.success = True
                result.end_time = datetime.now()
                return result

            result.total_files = len(files)
            logger.info(f"开始索引目录: {target_path}, 找到 {len(files)} 个文件")

            # 遍历并索引每个文件
            for file_path in files:
                try:
                    # Resolve each file, including catalog links, before parsing.
                    # A whitelisted directory may contain links to private files.
                    file_path.resolve().relative_to(dir_path)
                    if file_path.parent.name == ".documents":
                        self._reindex_manifest(file_path)
                    else:
                        self.index_single_file(str(file_path))
                    result.increment_success_count()
                    logger.info(f"✓ 文件索引成功: {file_path.name}")
                except Exception as e:
                    result.increment_fail_count()
                    result.add_failed_file(str(file_path), str(e))
                    logger.error(f"✗ 文件索引失败: {file_path.name}, 错误: {e}")

            result.success = result.fail_count == 0
            result.end_time = datetime.now()

            logger.info(
                f"目录索引完成: 总数={result.total_files}, "
                f"成功={result.success_count}, 失败={result.fail_count}"
            )

            return result

        except Exception as e:
            logger.error(f"索引目录失败: {e}")
            result.success = False
            result.error_message = str(e)
            result.end_time = datetime.now()
            return result

    def index_single_file(
        self,
        file_path: str,
        *,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
        coordinator=None,
        after_commit=None,
    ):
        """
        索引单个文件 (使用新的 LangChain 分割器)

        Args:
            file_path: 文件路径

        Raises:
            ValueError: 文件不存在时抛出
            RuntimeError: 索引失败时抛出
        """
        path = Path(file_path).resolve()

        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {file_path}")

        logger.info(f"开始索引文件: {path}")

        try:
            normalized_path = path.as_posix()
            # Parse before touching the existing, searchable version.
            documents = document_splitter_service.split_document(normalized_path)
            logger.info(f"文档分割完成: {file_path} -> {len(documents)} 个分片")

            if not documents or any(not doc.page_content.strip() for doc in documents):
                raise ValueError(f"文件内容为空或无法分割: {file_path}")
            identity = source or normalized_path
            for doc in documents:
                doc.metadata.update(metadata or {})
                doc.metadata["_source"] = identity
            vector_store_manager.replace_documents(
                identity, documents, coordinator=coordinator, after_commit=after_commit
            )
            logger.info(f"文件索引完成: {file_path}, 共 {len(documents)} 个分片")

        except IndexCommitUncertain:
            raise
        except Exception as e:
            logger.error(f"索引文件失败: {file_path}, 错误: {e}")
            raise RuntimeError(f"索引文件失败: {e}") from e

    @staticmethod
    def _write_manifest(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def index_uploaded_file(
        self, file_path: str, original_filename: str, content_hash: str, upload_root: str
    ) -> str:
        # The platform has one shared knowledge base. Within it, the normalized
        # original name is the stable document identity; content hashes version it.
        identity = hashlib.sha256(original_filename.casefold().encode("utf-8")).hexdigest()
        root, path = Path(upload_root).resolve(), Path(file_path).resolve()
        relative = path.relative_to(root).as_posix()
        manifest_dir = root / ".documents"
        manifest_dir.resolve().relative_to(root)
        manifest_dir.mkdir(exist_ok=True)
        manifest_path = manifest_dir / f"{identity}.json"
        manifest_path.resolve().relative_to(root)
        manifest = {
            "attempt": uuid.uuid4().hex,
            "document_id": identity,
            "filename": original_filename,
            "content_hash": content_hash,
            "storage_path": relative,
        }
        selected = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        previous, written, prepared = None, False, False
        after_commit = []
        try:
            with upload_document_lock(identity) as coordinator:
                previous = manifest_path.read_bytes() if manifest_path.exists() else None
                # A durable selection permits reconciliation after a process crash.
                self._write_manifest(manifest_path, selected)
                written = True
                self._index_upload_manifest(
                    root, manifest, coordinator=coordinator, after_commit=after_commit
                )
                prepared = True
        except IndexCommitUncertain:
            raise
        except BaseException as exc:
            if prepared:
                # The outer transaction owns COMMIT, whose reply may be lost.
                raise IndexCommitUncertain("索引提交结果待协调，请重建当前文档索引") from exc
            if written:
                try:
                    # Our original lock connection may have died. Reacquire it
                    # and only undo our unique attempt, never a newer selection.
                    with upload_document_lock(identity):
                        if manifest_path.exists() and manifest_path.read_bytes() == selected:
                            if previous is None:
                                manifest_path.unlink()
                            else:
                                self._write_manifest(manifest_path, previous)
                except Exception as restore_error:
                    raise IndexCommitUncertain(
                        "上传清单需要协调，请重建当前文档索引"
                    ) from restore_error
            raise
        for retire in after_commit:
            retire()
        return identity

    def _index_upload_manifest(
        self, root: Path, manifest: dict[str, Any], *, coordinator=None, after_commit=None
    ) -> None:
        name, identity = manifest["filename"], manifest["document_id"]
        if hashlib.sha256(name.casefold().encode("utf-8")).hexdigest() != identity:
            raise ValueError("Invalid upload document identity")
        path = (root / manifest["storage_path"]).resolve()
        path.relative_to(root)
        with path.open("rb") as content:
            if hashlib.file_digest(content, "sha256").hexdigest() != manifest["content_hash"]:
                raise ValueError("Stored document content does not match its version")
        self.index_single_file(
            str(path),
            source=f"upload:{identity}",
            metadata={
                "_document_id": identity,
                "_file_name": name,
                "_content_hash": manifest["content_hash"],
                "_storage_path": path.as_posix(),
            },
            coordinator=coordinator,
            after_commit=after_commit,
        )

    def _reindex_manifest(self, manifest_path: Path) -> None:
        after_commit = []
        prepared = False
        try:
            with upload_document_lock(manifest_path.stem) as coordinator:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("document_id") != manifest_path.stem:
                    raise ValueError("Upload catalog identity mismatch")
                self._index_upload_manifest(
                    manifest_path.parent.parent,
                    manifest,
                    coordinator=coordinator,
                    after_commit=after_commit,
                )
                prepared = True
        except Exception as exc:
            if prepared:
                raise IndexCommitUncertain("索引提交结果待协调，请重建当前文档索引") from exc
            raise
        for retire in after_commit:
            retire()


# 全局单例
vector_index_service = VectorIndexService()
