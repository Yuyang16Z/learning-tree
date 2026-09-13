"""Local document storage and bounded text extraction; no provider calls or remote uploads."""

import asyncio
import hashlib
import io
import multiprocessing
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import JSON, Column, LargeBinary
from sqlalchemy.orm import defer
from sqlmodel import Field, Session, SQLModel, select

from .db import get_session
from .models import Message, Node

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 200_000
MAX_PDF_PAGES = 200
MAX_DOCUMENTS_PER_MESSAGE = 4
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_ZIP_ENTRIES = 2_000
PARSE_TIMEOUT_SECONDS = 20

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".log": "text/plain",
}


class DocumentAttachment(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    tree_id: int = Field(foreign_key="knowledgetree.id", index=True)
    name: str
    media_type: str
    size: int
    sha256: str = Field(index=True)
    content: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    sections: list[dict[str, str]] = Field(sa_column=Column(JSON, nullable=False))
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentSection(BaseModel):
    label: str
    text: str


class DocumentSummary(BaseModel):
    id: str
    name: str
    media_type: str
    size: int
    characters: int
    warnings: list[str]


class DocumentDetail(DocumentSummary):
    sections: list[DocumentSection]


class DocumentParseError(ValueError):
    """A user-facing extraction error, intentionally excluding parser internals."""


def document_summary(document: DocumentAttachment) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        name=document.name,
        media_type=document.media_type,
        size=document.size,
        characters=sum(len(section["text"]) for section in document.sections),
        warnings=document.warnings,
    )


def resolve_documents(
    session: Session, ids: list[str] | None, tree_id: int
) -> list[DocumentAttachment]:
    unique_ids = list(dict.fromkeys(ids or []))
    if len(unique_ids) > MAX_DOCUMENTS_PER_MESSAGE:
        raise HTTPException(422, "每条消息最多附加 4 个文档。")
    result = []
    for document_id in unique_ids:
        document = session.get(
            DocumentAttachment, document_id, options=[defer(DocumentAttachment.content)]
        )
        if document is None or document.tree_id != tree_id:
            raise HTTPException(422, "文档不存在或不属于当前学习主题，请重新上传。")
        result.append(document)
    return result


def safe_document_name(name: str | None) -> str:
    # Browser filenames are untrusted labels, never filesystem paths.
    result = PurePosixPath((name or "").replace("\\", "/")).name
    result = "".join(char for char in result if ord(char) >= 32 and ord(char) != 127).strip()
    if not result or len(result) > 240:
        raise DocumentParseError("文件名为空或过长，请重命名后上传。")
    return result


def _add_section(sections: list[dict[str, str]], label: str, text: str, total: int) -> int:
    text = text.replace("\x00", "").strip()
    if not text:
        return total
    total += len(text)
    if total > MAX_EXTRACTED_CHARACTERS:
        raise DocumentParseError("文档提取文本超过 200,000 字符，请拆分后上传。")
    sections.append({"label": label, "text": text})
    return total


def _extract_pdf(content: bytes) -> tuple[list[dict[str, str]], list[str]]:
    from pypdf import PdfReader

    if not content.startswith(b"%PDF-"):
        raise DocumentParseError("文件内容不是有效的 PDF。")
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        raise DocumentParseError("暂不支持加密或密码保护的 PDF，请先解密。")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentParseError("PDF 超过 200 页，请拆分后上传。")
    sections, empty_pages, total = [], [], 0
    for index, page in enumerate(reader.pages, 1):
        # Bound individual inflated page streams before text extraction.
        stream = page.get_contents()
        if stream and len(stream.get_data()) > MAX_ZIP_BYTES:
            raise DocumentParseError("PDF 页面内容过大，请简化或拆分后上传。")
        text = page.extract_text() or ""
        if text.strip():
            total = _add_section(sections, f"Page {index}", text, total)
        else:
            empty_pages.append(str(index))
    if not sections:
        raise DocumentParseError("PDF 没有可提取的文字，可能是扫描件；暂不支持 OCR，请上传文字版。")
    warnings = []
    if empty_pages:
        pages = ", ".join(empty_pages[:20]) + ("…" if len(empty_pages) > 20 else "")
        warnings.append(f"第 {pages} 页没有可提取的文字（可能为空白页或扫描图片）；未进行 OCR。")
    return sections, warnings


def _validate_docx_archive(content: bytes) -> None:
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise DocumentParseError("文件内容不是有效的 DOCX；旧版 .doc 请另存为 .docx。")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
            raise DocumentParseError("文件内容不是有效的 Word DOCX 文档。")
        if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_ZIP_BYTES:
            raise DocumentParseError("DOCX 解压后内容过大，请精简图片或拆分后上传。")
        for entry in entries:
            if entry.flag_bits & 1:
                raise DocumentParseError("暂不支持加密或密码保护的 Word 文档。")
            if (
                entry.file_size > 1024 * 1024
                and entry.file_size / max(entry.compress_size, 1) > 100
            ):
                raise DocumentParseError("DOCX 压缩比例异常，请重新保存或拆分后上传。")
            if entry.filename.endswith(".xml"):
                xml = archive.read(entry)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise DocumentParseError("DOCX 包含不支持的 XML 声明，请另存文档后上传。")


def _extract_docx(content: bytes) -> tuple[list[dict[str, str]], list[str]]:
    from docx import Document
    from docx.table import Table

    _validate_docx_archive(content)
    document = Document(io.BytesIO(content))
    sections, total, paragraph_count, table_count = [], 0, 0, 0
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            table_count += 1
            # Retain rows/columns in source order; do not execute embedded objects or macros.
            text = "\n".join("\t".join(cell.text for cell in row.cells) for row in block.rows)
            total = _add_section(sections, f"Table {table_count}", text, total)
        else:
            paragraph_count += 1
            total = _add_section(sections, f"Paragraph {paragraph_count}", block.text, total)
    if not sections:
        raise DocumentParseError("Word 文档没有可提取的段落或表格文字；图片中的文字暂不支持 OCR。")
    warnings = []
    if document.inline_shapes:
        warnings.append("仅提取 Word 段落与表格文字，内嵌图片未进行 OCR。")
    return sections, warnings


def parse_document(content: bytes, name: str) -> tuple[list[dict[str, str]], list[str]]:
    """Pure extraction helper; production calls execute this in a disposable process."""
    extension = PurePosixPath(name).suffix.lower()
    if extension == ".doc":
        raise DocumentParseError("暂不支持旧版 .doc，请在 Word 中另存为 .docx 或 PDF 后上传。")
    if extension not in MEDIA_TYPES:
        raise DocumentParseError("支持 PDF、DOCX、TXT、Markdown、CSV、TSV、JSON 和 LOG 文件。")
    if not content:
        raise DocumentParseError("文件为空，请选择有内容的文档。")
    if len(content) > MAX_FILE_BYTES:
        raise DocumentParseError("单个文档不能超过 10 MiB。")
    try:
        if extension == ".pdf":
            return _extract_pdf(content)
        if extension == ".docx":
            return _extract_docx(content)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentParseError("文本文件需使用 UTF-8 编码，请转换编码后上传。") from exc
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            raise DocumentParseError("文件包含二进制内容，请上传 UTF-8 纯文本文件。")
        sections = []
        _add_section(sections, "Text", text, 0)
        if not sections:
            raise DocumentParseError("文档没有可提取的文字。")
        return sections, []
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(
            "文档已损坏或无法解析，请重新导出为 PDF、DOCX 或 UTF-8 文本。"
        ) from exc


def _parse_worker(connection, content: bytes, name: str) -> None:
    try:
        # A CPU bound is useful on Unix; wall-clock enforcement also works on Windows.
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
            # macOS does not reliably support RLIMIT_AS. The Linux bound protects
            # server installations from compressed PDF streams exhausting memory.
            import sys

            if sys.platform.startswith("linux"):
                memory_limit = 768 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        except (ImportError, ValueError, OSError):
            pass
        connection.send((True, parse_document(content, name)))
    except DocumentParseError as exc:
        connection.send((False, str(exc)))
    except BaseException:
        connection.send((False, "文档解析失败，请精简文档后重试。"))
    finally:
        connection.close()


def parse_document_limited(content: bytes, name: str) -> tuple[list[dict[str, str]], list[str]]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_parse_worker, args=(sender, content, name), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(PARSE_TIMEOUT_SECONDS):
            raise DocumentParseError("文档解析超时，请拆分或重新导出文档后上传。")
        try:
            success, payload = receiver.recv()
        except EOFError as exc:
            raise DocumentParseError("文档解析失败或资源消耗过大，请拆分后上传。") from exc
        if not success:
            raise DocumentParseError(payload)
        return payload
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


router = APIRouter(tags=["documents"])


@router.post("/nodes/{node_id}/documents", response_model=DocumentSummary)
async def upload_document(
    node_id: int, file: UploadFile, session: Session = Depends(get_session)
) -> DocumentSummary:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "学习节点不存在。")
    try:
        name = safe_document_name(file.filename)
        content = await file.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, "单个文档不能超过 10 MiB。")
        sections, warnings = await asyncio.to_thread(parse_document_limited, content, name)
    except DocumentParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()
    digest = hashlib.sha256(content).hexdigest()
    document = session.exec(
        select(DocumentAttachment).where(
            DocumentAttachment.tree_id == node.tree_id,
            DocumentAttachment.sha256 == digest,
            DocumentAttachment.name == name,
        )
    ).first()
    if document is None:
        document = DocumentAttachment(
            tree_id=node.tree_id,
            name=name,
            media_type=MEDIA_TYPES[PurePosixPath(name).suffix.lower()],
            size=len(content),
            sha256=digest,
            content=content,
            sections=sections,
            warnings=warnings,
        )
        session.add(document)
        session.commit()
        session.refresh(document)
    return document_summary(document)


def _get_document(session: Session, document_id: str) -> DocumentAttachment:
    document = session.get(
        DocumentAttachment, document_id, options=[defer(DocumentAttachment.content)]
    )
    if document is None:
        raise HTTPException(404, "文档不存在。")
    return document


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: str, session: Session = Depends(get_session)) -> DocumentDetail:
    document = _get_document(session, document_id)
    return DocumentDetail(**document_summary(document).model_dump(), sections=document.sections)


@router.get("/documents/{document_id}/download")
def download_document(document_id: str, session: Session = Depends(get_session)) -> Response:
    document = _get_document(session, document_id)
    return Response(
        content=document.content,
        media_type=document.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, session: Session = Depends(get_session)) -> Response:
    document = _get_document(session, document_id)
    # Select only references, not stored image data or answer bodies.
    for document_ids in session.exec(select(Message.document_ids)).all():
        if document.id in (document_ids or []):
            raise HTTPException(409, "文档已用于对话，请保留来源记录；删除学习主题时会一并清理。")
    session.delete(document)
    session.commit()
    return Response(status_code=204)
