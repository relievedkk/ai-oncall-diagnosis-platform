"""文档分割服务模块 - 基于 LangChain 的智能文档分割

支持多种文件类型，每种类型有独立的处理器：
- .md:   MarkdownHeaderTextSplitter（按标题）+ RecursiveCharacterTextSplitter
- .txt:  RecursiveCharacterTextSplitter
- .pdf:  PyPDFLoader（按页提取）+ RecursiveCharacterTextSplitter
- .docx: Docx2txtLoader（提取文本）+ RecursiveCharacterTextSplitter

新增文件类型只需在 SUPPORTED_EXTENSIONS 注册并实现 split_xxx 方法。
"""

from pathlib import Path
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from loguru import logger

from app.config import config


class DocumentSplitterService:
    """文档分割服务 - 使用 LangChain 的分割器，支持多文件类型"""

    # 文件类型 -> 处理器方法名 的注册表
    # 新增类型只需在此注册并实现对应 split_xxx 方法（开闭原则）
    SUPPORTED_EXTENSIONS = {
        ".md": "split_markdown",
        ".txt": "split_text",
        ".pdf": "split_pdf",
        ".docx": "split_docx",
    }

    def __init__(self):
        """初始化文档分割服务"""
        self.chunk_size = config.chunk_max_size
        self.chunk_overlap = config.chunk_overlap

        # Markdown 标题分割器 (只按一级和二级标题分割，减少分片数)
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
            ],
            strip_headers=False,
        )

        # 递归字符分割器 (用于二次分割，使用更大的chunk_size)
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size * 2,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

        logger.info(
            f"文档分割服务初始化完成, chunk_size={self.chunk_size}, "
            f"secondary_chunk_size={self.chunk_size * 2}, "
            f"overlap={self.chunk_overlap}, "
            f"supported={list(self.SUPPORTED_EXTENSIONS.keys())}"
        )

    def split_document(self, file_path: str) -> List[Document]:
        """
        智能分割文档（根据文件类型自动路由到对应处理器）

        Args:
            file_path: 文件路径（由处理器内部负责读取和解析）

        Returns:
            List[Document]: 文档分片列表

        Raises:
            ValueError: 不支持的文件类型时抛出
        """
        ext = Path(file_path).suffix.lower()
        handler_name = self.SUPPORTED_EXTENSIONS.get(ext)
        if not handler_name:
            raise ValueError(
                f"不支持的文件类型: {ext}，"
                f"支持的类型: {list(self.SUPPORTED_EXTENSIONS.keys())}"
            )
        handler = getattr(self, handler_name)
        return handler(file_path)

    def split_markdown(self, file_path: str) -> List[Document]:
        """
        分割 Markdown 文档（三阶段：按标题切 -> 按大小切 -> 合并小片段）

        Args:
            file_path: 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        content = Path(file_path).read_text(encoding="utf-8")
        if not content or not content.strip():
            logger.warning(f"Markdown 文档内容为空: {file_path}")
            return []

        try:
            # 第一阶段: 按标题分割
            md_docs = self.markdown_splitter.split_text(content)

            # 第二阶段: 按大小进一步分割
            docs_after_split = self.text_splitter.split_documents(md_docs)

            # 第三阶段: 合并太小的分片 (< 300字符)
            final_docs = self._merge_small_chunks(docs_after_split, min_size=300)

            # 添加文件路径元数据
            for doc in final_docs:
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = ".md"
                doc.metadata["_file_name"] = Path(file_path).name

            logger.info(f"Markdown 分割完成: {file_path} -> {len(final_docs)} 个分片")
            return final_docs

        except Exception as e:
            logger.error(f"Markdown 分割失败: {file_path}, 错误: {e}")
            raise

    def split_text(self, file_path: str) -> List[Document]:
        """
        分割普通文本文档（单阶段：按字符大小切）

        Args:
            file_path: 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        content = Path(file_path).read_text(encoding="utf-8")
        if not content or not content.strip():
            logger.warning(f"文本文档内容为空: {file_path}")
            return []

        try:
            docs = self.text_splitter.create_documents(
                texts=[content],
                metadatas=[
                    {
                        "_source": file_path,
                        "_extension": Path(file_path).suffix,
                        "_file_name": Path(file_path).name,
                    }
                ],
            )

            logger.info(f"文本分割完成: {file_path} -> {len(docs)} 个分片")
            return docs

        except Exception as e:
            logger.error(f"文本分割失败: {file_path}, 错误: {e}")
            raise

    def split_pdf(self, file_path: str) -> List[Document]:
        """
        分割 PDF 文档（两阶段：PyPDFLoader 按页提取 -> RecursiveCharacterTextSplitter 按大小切）

        使用 LangChain 的 PyPDFLoader（基于 pypdf）逐页提取文本，每页一个 Document，
        再用 RecursiveCharacterTextSplitter 控制分片大小，最后合并过小片段。

        注意：仅支持文本型 PDF，扫描版 PDF（图片型）需 OCR，此处不处理。

        Args:
            file_path: PDF 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        from langchain_community.document_loaders import PyPDFLoader

        try:
            # 第一阶段: PyPDFLoader 按页提取（每页一个 Document）
            loader = PyPDFLoader(file_path)
            pages = loader.load()

            if not pages:
                logger.warning(f"PDF 内容为空（可能是扫描版 PDF）: {file_path}")
                return []

            logger.info(f"PDF 加载完成: {file_path}, 共 {len(pages)} 页")

            # 第二阶段: 按大小进一步分割
            docs_after_split = self.text_splitter.split_documents(pages)

            # 第三阶段: 合并太小的分片
            final_docs = self._merge_small_chunks(docs_after_split, min_size=300)

            # 补充元数据（PyPDFLoader 已带 page 元数据，这里补 _source 等）
            for doc in final_docs:
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = ".pdf"
                doc.metadata["_file_name"] = Path(file_path).name

            logger.info(f"PDF 分割完成: {file_path} -> {len(final_docs)} 个分片")
            return final_docs

        except Exception as e:
            logger.error(f"PDF 分割失败: {file_path}, 错误: {e}")
            raise

    def split_docx(self, file_path: str) -> List[Document]:
        """
        分割 Word 文档（两阶段：Docx2txtLoader 提取文本 -> RecursiveCharacterTextSplitter 按大小切）

        使用 LangChain 的 Docx2txtLoader（基于 docx2txt）提取 .docx 全文为纯文本，
        再用 RecursiveCharacterTextSplitter 控制分片大小。

        注意：仅支持 .docx（Office 2007+），不支持旧版 .doc。

        Args:
            file_path: .docx 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        from langchain_community.document_loaders import Docx2txtLoader

        try:
            # 第一阶段: Docx2txtLoader 提取全文
            loader = Docx2txtLoader(file_path)
            documents = loader.load()

            if not documents:
                logger.warning(f"Word 文档内容为空: {file_path}")
                return []

            logger.info(
                f"Word 文档加载完成: {file_path}, "
                f"文本长度: {len(documents[0].page_content)} 字符"
            )

            # 第二阶段: 按大小分割
            docs_after_split = self.text_splitter.split_documents(documents)

            # 补充元数据
            for doc in docs_after_split:
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = ".docx"
                doc.metadata["_file_name"] = Path(file_path).name

            logger.info(f"Word 文档分割完成: {file_path} -> {len(docs_after_split)} 个分片")
            return docs_after_split

        except Exception as e:
            logger.error(f"Word 文档分割失败: {file_path}, 错误: {e}")
            raise

    def _merge_small_chunks(
        self, documents: List[Document], min_size: int = 300
    ) -> List[Document]:
        """
        合并太小的分片

        Args:
            documents: 文档列表
            min_size: 最小分片大小 (字符数)

        Returns:
            List[Document]: 合并后的文档列表
        """
        if not documents:
            return []

        merged_docs = []
        current_doc = None

        for doc in documents:
            doc_size = len(doc.page_content)

            if current_doc is None:
                current_doc = doc
            elif doc_size < min_size and len(current_doc.page_content) < self.chunk_size * 2:
                current_doc.page_content += "\n\n" + doc.page_content
            else:
                merged_docs.append(current_doc)
                current_doc = doc

        if current_doc is not None:
            merged_docs.append(current_doc)

        return merged_docs


# 全局单例
document_splitter_service = DocumentSplitterService()
