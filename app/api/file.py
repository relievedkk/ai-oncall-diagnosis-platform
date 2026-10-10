"""文件上传接口模块"""

import asyncio
import hashlib
import os
import unicodedata
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.auth import is_path_allowed
from app.services.vector_index_service import vector_index_service

router = APIRouter()

# 文件上传后存储的路径
UPLOAD_DIR = Path("./uploads")
# 支持的文件类型
ALLOWED_EXTENSIONS = ("txt", "md", "pdf", "docx")
UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件并自动创建向量索引

    Args:
        file: 上传的文件

    Returns:
        JSONResponse: 上传结果
    """
    try:
        # 1. 验证文件
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        if len(file.filename) > 255:
            raise HTTPException(status_code=400, detail="文件名过长")

        # 2. 规范化文件名（仅用于显示；磁盘文件名由内容摘要生成）
        safe_filename = _sanitize_filename(file.filename)

        # 3. 验证文件扩展名
        file_extension = _get_file_extension(safe_filename)
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        # 4. 创建上传目录和隔离的临时目录
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        temp_dir = UPLOAD_DIR / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{uuid.uuid4().hex}.upload"

        # 5. 分块写入并计算摘要，避免将整个文件一次性读入内存。
        size = 0
        digest = hashlib.sha256()
        try:
            with temp_path.open("xb") as output:
                while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > config.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件大小超过限制（最大 {config.max_upload_bytes} 字节）",
                        )
                    digest.update(chunk)
                    output.write(chunk)

            if size == 0:
                raise HTTPException(status_code=400, detail="文件内容不能为空")

            _validate_file_content(temp_path, file_extension)
            storage_filename = f"{digest.hexdigest()[:32]}.{file_extension}"
            file_path = UPLOAD_DIR / storage_filename
            created_new = not file_path.exists()
            if created_new:
                os.replace(temp_path, file_path)
            else:
                temp_path.unlink(missing_ok=True)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        logger.info("文件上传并校验成功: {} ({} bytes)", storage_filename, size)

        # 6. 自动创建向量索引。该操作是同步且可能较慢，放入线程避免阻塞事件循环。
        try:
            logger.info(f"开始为上传文件创建向量索引: {file_path}")
            await asyncio.to_thread(
                vector_index_service.index_single_file,
                str(file_path),
            )
            logger.info(f"向量索引创建成功: {file_path}")
        except Exception as e:
            logger.error("向量索引创建失败: {}, 错误: {}", storage_filename, e)
            if created_new:
                file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "文件校验成功，但向量索引创建失败",
                    "filename": safe_filename,
                    "index_status": "failed",
                },
            ) from e

        # 7. 只有文件保存和向量索引都成功才返回成功。
        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success",
                "data": {
                    "filename": safe_filename,
                    "storage_id": storage_filename,
                    "size": size,
                },
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail="文件上传失败") from e


@router.post("/index_directory")
async def index_directory(
    directory_path: str | None = Query(default=None, min_length=1, max_length=512),
):
    """
    索引指定目录下的所有文件

    安全限制：仅允许索引配置项 ALLOWED_INDEX_ROOTS 中的目录子树，
    防止任意路径读取。

    Args:
        directory_path: 目录路径（可选，默认使用 uploads 目录）

    Returns:
        JSONResponse: 索引结果
    """
    try:
        target = directory_path if directory_path else "uploads"

        if not is_path_allowed(target):
            logger.warning(f"索引目录被拒绝（不在白名单内）: {target}")
            raise HTTPException(
                status_code=403,
                detail="目标目录不在允许的索引范围内",
            )

        logger.info(f"开始索引目录: {target}")

        # 执行索引（传校验过的 target，保证「校验什么就执行什么」）
        result = await asyncio.to_thread(vector_index_service.index_directory, target)

        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success" if result.success else "partial_success",
                "data": result.to_dict(),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"索引目录失败: {e}")
        raise HTTPException(status_code=500, detail="索引目录失败") from e


def _get_file_extension(filename: str) -> str:
    """
    获取文件扩展名

    Args:
        filename: 文件名

    Returns:
        str: 扩展名（小写，不含点）
    """
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _sanitize_filename(filename: str) -> str:
    """
    规范化文件名，去除空格和特殊字符

    Args:
        filename: 原始文件名

    Returns:
        str: 规范化后的文件名
    """
    sanitized = unicodedata.normalize("NFKC", Path(filename).name).strip().replace(" ", "_")
    # 去除其他可能导致问题的字符
    for char in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        sanitized = sanitized.replace(char, "_")
    sanitized = "".join(char for char in sanitized if char.isprintable())
    if not sanitized or sanitized in {".", ".."}:
        raise HTTPException(status_code=400, detail="文件名无效")
    return sanitized[:255]


def _validate_file_content(path: Path, extension: str) -> None:
    """Validate content signatures and bound archive expansion before parsing."""
    with path.open("rb") as source:
        header = source.read(8)
    if extension == "pdf":
        if not header.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容不是有效的 PDF")
        return

    if extension == "docx":
        if not zipfile.is_zipfile(path):
            raise HTTPException(status_code=400, detail="文件内容不是有效的 DOCX")
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > 1_000:
                    raise HTTPException(status_code=400, detail="DOCX 文件条目过多")
                total_size = sum(member.file_size for member in members)
                if total_size > config.max_archive_uncompressed_bytes:
                    raise HTTPException(status_code=413, detail="DOCX 解压后大小超过限制")
                names = {member.filename for member in members}
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise HTTPException(status_code=400, detail="DOCX 结构无效")
                if any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
                    raise HTTPException(status_code=400, detail="DOCX 包含不安全路径")
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="DOCX 压缩包损坏") from exc
        return

    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="文本文件必须使用 UTF-8 编码") from exc
