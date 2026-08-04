"""크롤링 중 발견한 PDF 첨부파일(수강편람 등)을 문서 RAG 청크로 바꾸는 모듈.

Crawler가 공지 상세에서 찾은 PDF 첨부를 ``Crawler.fetch_pdf_attachments()``로
내려받아 바이트로 넘겨준다(03_crawler.md). 이 모듈은 그 바이트를 페이지 단위로
텍스트를 뽑아 ``app.extractor.chunk_document``로 분할하고, 임베딩만 만들어
``documents(source_type='pdf')``에 저장한다. HTTP 다운로드나 HTML 파싱은 하지
않는다 — 순수하게 바이트 → 청크 변환만 담당한다.

엔티티·관계 추출(그래프 반영)은 하지 않는다 — 문서 RAG는 벡터 top-k만으로 동작한다
(07_graph-rag-engine.md §5).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Callable, Protocol, Sequence

from app.extractor import DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS, chunk_document
from app.llm import Embedder

_NON_SLUG_CHARACTERS = re.compile(r"[\W_]+", re.UNICODE)


class PdfIngestStorage(Protocol):
    """PdfIngestor가 쓰는 Storage 공개 메서드의 최소 계약(06_storage.md)."""

    def upsert_document(
        self,
        school_id: int,
        source_url: str,
        title: str | None,
        content: str,
        chunk_index: int,
        content_hash: str,
        embedding: Sequence[float] | None,
        *,
        source_type: str = "web",
        page: int | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class PdfIngestResult:
    """PDF 한 건 처리 결과."""

    filename: str
    source_url: str
    page_count: int
    chunk_count: int
    doc_ids: list[int]


def source_url_for_pdf_attachment(filename: str, pdf_bytes: bytes) -> str:
    """PDF 첨부의 합성 ``source_url``을 만든다.

    첨부 원본 URL은 원문 근거로 그대로 쓰기엔 파일명 충돌(같은 이름 다른 학교/시기)
    위험이 있어, 파일명 슬러그 + 파일 내용 해시(12자)로 안정적인 의사 URI를 만든다.
    재크롤링에서 같은 파일을 다시 받으면 같은 ``source_url``이라 멱등 upsert되고,
    파일명이 같아도 내용이 다르면 다른 문서로 취급된다.
    """

    slug = _NON_SLUG_CHARACTERS.sub("-", unicodedata.normalize("NFKC", filename)).strip("-").lower()
    file_hash = sha256(pdf_bytes).hexdigest()[:12]
    return f"attachment:{slug or 'file'}:{file_hash}"


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """PDF 바이트에서 페이지별 텍스트를 뽑는다. 빈 페이지는 빈 문자열로 남는다."""

    import pdfplumber

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


class PdfIngestor:
    """PDF 첨부 바이트를 페이지 단위로 청킹·임베딩해 저장한다."""

    def __init__(
        self,
        storage: PdfIngestStorage,
        embedder: Embedder,
        *,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        page_extractor: Callable[[bytes], list[str]] = extract_pages,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars
        self.page_extractor = page_extractor

    def ingest(self, school_id: int, filename: str, pdf_bytes: bytes) -> PdfIngestResult:
        """PDF 한 건을 파싱·청킹·임베딩해 ``documents``에 저장한다."""

        pages = self.page_extractor(pdf_bytes)
        source_url = source_url_for_pdf_attachment(filename, pdf_bytes)

        doc_ids: list[int] = []
        chunk_index = 0
        for page_number, page_text in enumerate(pages, start=1):
            for content, _local_index in chunk_document(page_text, self.chunk_chars, self.overlap_chars):
                content_hash = sha256(content.encode("utf-8")).hexdigest()
                embedding = self.embedder.embed(content)
                doc_id = self.storage.upsert_document(
                    school_id,
                    source_url,
                    filename,
                    content,
                    chunk_index,
                    content_hash,
                    embedding,
                    source_type="pdf",
                    page=page_number,
                )
                doc_ids.append(doc_id)
                chunk_index += 1

        return PdfIngestResult(
            filename=filename,
            source_url=source_url,
            page_count=len(pages),
            chunk_count=len(doc_ids),
            doc_ids=doc_ids,
        )
