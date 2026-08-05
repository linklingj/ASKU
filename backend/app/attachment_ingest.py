"""사용자가 올린 첨부 문서(수강편람 PDF·HWP 등)를 문서 RAG 청크로 바꾸는 모듈.

크롤러는 관여하지 않는다. API 가 업로드 받은 **바이트**를 그대로 넘겨주면, 이 모듈이
확장자에 맞는 파서로 텍스트를 뽑아 ``app.extractor.chunk_document`` 로 분할하고,
임베딩만 만들어 ``documents(source_type='attachment')`` 에 저장한다. HTTP 다운로드나
HTML 파싱은 하지 않는다 — 순수하게 바이트 → 청크 변환만 담당한다.

엔티티·관계 추출(그래프 반영)은 하지 않는다. 문서 RAG 는 벡터 top-k 만으로 동작하며
그래프를 확장하지 않는다(07_graph-rag-engine.md).

새 포맷을 더하려면 ``_PARSERS`` 에 확장자 → ``_Format`` 을 등록하면 된다.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from typing import Callable, Protocol, Sequence

from app.extractor import DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS, chunk_document
from app.llm import Embedder
from app.models import SOURCE_TYPE_ATTACHMENT, SOURCE_TYPE_WEB, Attachment, attachment_source_uri


class AttachmentError(Exception):
    """첨부 처리 실패. ``code`` 는 API 응답·첨부 상태에 그대로 남는다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UnsupportedAttachmentError(AttachmentError):
    """지원하지 않는 확장자. 업로드 시점에 걸러 저장까지 가지 않게 한다."""

    def __init__(self, filename: str) -> None:
        super().__init__(
            "UNSUPPORTED_FILE_TYPE",
            f"지원하지 않는 파일 형식입니다: {filename} (지원: {', '.join(supported_extensions())})",
        )


class AttachmentParseError(AttachmentError):
    """파일은 지원 형식이지만 텍스트를 뽑지 못했다(손상·암호·스캔본 등)."""


# ── 포맷별 텍스트 추출 ────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedAttachment:
    """첨부 한 건에서 뽑은 텍스트.

    ``units`` 는 인용 단위(PDF 는 페이지, HWP 는 구역)별 텍스트다. ``paginated`` 가
    False 인 포맷(txt 등)은 단위 번호가 의미 없으므로 ``page`` 를 남기지 않는다.
    """

    units: list[str]
    paginated: bool


@dataclass(frozen=True)
class _Format:
    parse: Callable[[bytes], list[str]]
    paginated: bool


def extract_pdf_pages(data: bytes) -> list[str]:
    """PDF 바이트에서 페이지별 텍스트를 뽑는다. 빈 페이지는 빈 문자열로 남는다."""

    try:
        import pdfplumber
    except ImportError as error:  # 배포 환경에 선택 의존이 빠진 경우
        raise AttachmentParseError("PDF_BACKEND_MISSING", "PDF 처리 의존(pdfplumber)이 설치되어 있지 않습니다") from error

    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages]
    except AttachmentParseError:
        raise
    except Exception as error:
        raise AttachmentParseError("PDF_PARSE_FAILED", f"PDF 를 읽지 못했습니다: {type(error).__name__}") from error


# HWP 5.0 레코드 헤더: 하위 10비트 tag_id, 다음 10비트 level, 상위 12비트 size.
# size 가 0xFFF 면 뒤따르는 4바이트가 실제 크기다.
_HWPTAG_BEGIN = 0x010
_HWPTAG_PARA_TEXT = _HWPTAG_BEGIN + 51

# 문단 텍스트(UTF-16LE) 안에 섞여 있는 제어 문자. 코드값이 곧 종류다.
_HWP_INLINE_CONTROLS = frozenset({4, 5, 6, 7, 8, 9, 19, 20})
_HWP_EXTENDED_CONTROLS = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_HWP_CONTROL_SPAN = 8  # 위 두 종류는 UTF-16 코드 단위 8개를 차지한다


def extract_hwp_sections(data: bytes) -> list[str]:
    """HWP 5.0(OLE) 바이트에서 구역(BodyText/Section*)별 텍스트를 뽑는다.

    HWP 는 페이지 경계를 파일에 담지 않는다(뷰어가 레이아웃할 때 정해진다).
    그래서 인용 단위는 페이지가 아니라 **구역**으로 잡는다.
    """

    try:
        import olefile
    except ImportError as error:
        raise AttachmentParseError("HWP_BACKEND_MISSING", "HWP 처리 의존(olefile)이 설치되어 있지 않습니다") from error

    try:
        ole = olefile.OleFileIO(BytesIO(data))
    except Exception as error:
        raise AttachmentParseError("HWP_PARSE_FAILED", f"HWP 를 읽지 못했습니다: {type(error).__name__}") from error

    try:
        if not ole.exists("FileHeader"):
            raise AttachmentParseError("HWP_PARSE_FAILED", "HWP 5.0 형식이 아닙니다 (FileHeader 없음)")
        header = ole.openstream("FileHeader").read()
        if len(header) < 40:
            raise AttachmentParseError("HWP_PARSE_FAILED", "HWP FileHeader 가 손상되었습니다")
        flags = int.from_bytes(header[36:40], "little")
        compressed = bool(flags & 0x01)
        if flags & 0x02:  # 암호 설정 문서는 본문을 복호화할 수 없다
            raise AttachmentParseError("HWP_ENCRYPTED", "암호가 걸린 HWP 는 처리할 수 없습니다")

        section_paths = sorted(
            (path for path in ole.listdir() if len(path) == 2 and path[0] == "BodyText"),
            key=lambda path: _section_order(path[1]),
        )
        if not section_paths:
            raise AttachmentParseError("HWP_PARSE_FAILED", "HWP 본문(BodyText)을 찾지 못했습니다")

        return [_hwp_section_text(ole.openstream("/".join(path)).read(), compressed) for path in section_paths]
    finally:
        ole.close()


def _section_order(name: str) -> int:
    """'Section10' 이 'Section2' 보다 뒤에 오도록 숫자 기준으로 정렬한다."""

    match = re.search(r"(\d+)$", name)
    return int(match.group(1)) if match else 0


def _hwp_section_text(stream: bytes, compressed: bool) -> str:
    if compressed:
        try:
            stream = zlib.decompress(stream, -15)  # HWP 는 raw deflate(헤더 없음)로 저장한다
        except zlib.error as error:
            raise AttachmentParseError("HWP_PARSE_FAILED", "HWP 본문 압축을 풀지 못했습니다") from error

    paragraphs: list[str] = []
    offset = 0
    size = len(stream)
    while offset + 4 <= size:
        header = int.from_bytes(stream[offset : offset + 4], "little")
        offset += 4
        tag_id = header & 0x3FF
        payload_size = (header >> 20) & 0xFFF
        if payload_size == 0xFFF:
            if offset + 4 > size:
                break
            payload_size = int.from_bytes(stream[offset : offset + 4], "little")
            offset += 4
        payload = stream[offset : offset + payload_size]
        offset += payload_size
        if tag_id == _HWPTAG_PARA_TEXT:
            paragraphs.append(_decode_hwp_paragraph(payload))
    return "\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


def _decode_hwp_paragraph(payload: bytes) -> str:
    """문단 레코드(UTF-16LE)에서 제어 문자를 걷어내고 본문만 남긴다.

    인라인·확장 제어 문자는 자기 자신을 포함해 코드 단위 8개를 차지하므로 통째로
    건너뛴다. 그러지 않으면 표·그림 자리에 깨진 글자가 섞여 근거로 인용된다.
    """

    text = payload.decode("utf-16-le", errors="ignore")
    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        code = ord(text[index])
        if code in _HWP_INLINE_CONTROLS or code in _HWP_EXTENDED_CONTROLS:
            index += _HWP_CONTROL_SPAN
            continue
        if code in (10, 13):  # 줄바꿈·문단 끝
            result.append("\n")
        elif code >= 32:
            result.append(text[index])
        index += 1
    return "".join(result).strip()


def extract_hwpx_sections(data: bytes) -> list[str]:
    """HWPX(OWPML, zip+XML)에서 구역(Contents/section*.xml)별 텍스트를 뽑는다."""

    from xml.etree import ElementTree

    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except Exception as error:
        raise AttachmentParseError("HWPX_PARSE_FAILED", f"HWPX 를 읽지 못했습니다: {type(error).__name__}") from error

    with archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", name)),
            key=_section_order_from_name,
        )
        if not names:
            raise AttachmentParseError("HWPX_PARSE_FAILED", "HWPX 본문(Contents/section*.xml)을 찾지 못했습니다")

        sections: list[str] = []
        for name in names:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError as error:
                raise AttachmentParseError("HWPX_PARSE_FAILED", f"HWPX 본문 XML 이 손상되었습니다: {name}") from error
            sections.append(_hwpx_section_text(root))
        return sections


def _section_order_from_name(name: str) -> int:
    return _section_order(PurePosixPath(name).stem)


def _hwpx_section_text(root) -> str:
    """문단(``p``) 단위로 줄을 나누고, 문단 안의 글자 조각(``t``)은 이어 붙인다."""

    lines: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "p":
            continue
        parts = [node.text for node in element.iter() if _local_name(node.tag) == "t" and node.text]
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _local_name(tag: str) -> str:
    """'{ns}p' 처럼 네임스페이스가 붙은 태그에서 이름만 뽑는다."""

    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def extract_plain_text(data: bytes) -> list[str]:
    """텍스트 파일을 통째로 한 단위로 읽는다. UTF-8 우선, 실패 시 CP949."""

    for encoding in ("utf-8", "cp949"):
        try:
            return [data.decode(encoding)]
        except UnicodeDecodeError:
            continue
    return [data.decode("utf-8", errors="ignore")]


_PARSERS: dict[str, _Format] = {
    ".pdf": _Format(extract_pdf_pages, paginated=True),
    ".hwp": _Format(extract_hwp_sections, paginated=True),
    ".hwpx": _Format(extract_hwpx_sections, paginated=True),
    ".txt": _Format(extract_plain_text, paginated=False),
    ".md": _Format(extract_plain_text, paginated=False),
}


def supported_extensions() -> tuple[str, ...]:
    """업로드를 받아주는 확장자 목록. API 검증과 오류 문구가 함께 참조한다."""

    return tuple(sorted(_PARSERS))


def is_supported(filename: str) -> bool:
    return _extension(filename) in _PARSERS


def parse_attachment(filename: str, data: bytes) -> ParsedAttachment:
    """확장자에 맞는 파서로 첨부 텍스트를 뽑는다."""

    fmt = _PARSERS.get(_extension(filename))
    if fmt is None:
        raise UnsupportedAttachmentError(filename)
    return ParsedAttachment(units=fmt.parse(data), paginated=fmt.paginated)


def _extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


# ── 저장 ──────────────────────────────────────────────────────────────


class AttachmentStorage(Protocol):
    """AttachmentIngestor 가 쓰는 Storage 공개 메서드의 최소 계약(06_storage.md)."""

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
        source_type: str = SOURCE_TYPE_WEB,
        page: int | None = None,
        attachment_id: int | None = None,
    ) -> int: ...

    def update_attachment_status(
        self,
        school_id: int,
        attachment_id: int,
        status: str,
        *,
        page_count: int | None = None,
        chunk_count: int | None = None,
        error_code: str | None = None,
    ) -> Attachment | None: ...


@dataclass(frozen=True)
class AttachmentIngestResult:
    """첨부 한 건 처리 결과."""

    attachment_id: int
    filename: str
    unit_count: int
    chunk_count: int
    doc_ids: list[int]


class AttachmentIngestor:
    """첨부 바이트를 파싱·청킹·임베딩해 저장하고 첨부 상태를 갱신한다."""

    def __init__(
        self,
        storage: AttachmentStorage,
        embedder: Embedder,
        *,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        parser: Callable[[str, bytes], ParsedAttachment] = parse_attachment,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars
        self.parser = parser

    def ingest(self, attachment: Attachment, data: bytes) -> AttachmentIngestResult:
        """첨부 한 건을 색인한다. 실패하면 첨부를 ``failed`` 로 남기고 예외를 올린다.

        청크의 ``source_url`` 은 ``attachment://{id}`` 합성 URI 다. 같은 첨부를 다시
        색인해도 (school_id, source_url, content_hash, chunk_index) 기준으로 멱등
        업서트되므로 중복이 쌓이지 않는다(06_storage.md).
        """

        if attachment.attachment_id is None:
            raise ValueError("저장되지 않은 첨부는 색인할 수 없습니다")

        school_id = attachment.school_id
        attachment_id = attachment.attachment_id
        source_url = attachment_source_uri(attachment_id)
        self.storage.update_attachment_status(school_id, attachment_id, "indexing")

        try:
            parsed = self.parser(attachment.filename, data)
            doc_ids = self._store_chunks(attachment, source_url, parsed)
            if not doc_ids:
                # 스캔 이미지만 있는 PDF 처럼 텍스트 계층이 없는 파일이 여기 걸린다.
                raise AttachmentParseError("EMPTY_CONTENT", "첨부에서 텍스트를 찾지 못했습니다")
        except AttachmentError as error:
            self.storage.update_attachment_status(
                school_id, attachment_id, "failed", error_code=error.code
            )
            raise
        except Exception:
            self.storage.update_attachment_status(
                school_id, attachment_id, "failed", error_code="INGEST_FAILED"
            )
            raise

        self.storage.update_attachment_status(
            school_id,
            attachment_id,
            "ready",
            page_count=len(parsed.units),
            chunk_count=len(doc_ids),
        )
        return AttachmentIngestResult(
            attachment_id=attachment_id,
            filename=attachment.filename,
            unit_count=len(parsed.units),
            chunk_count=len(doc_ids),
            doc_ids=doc_ids,
        )

    def _store_chunks(
        self, attachment: Attachment, source_url: str, parsed: ParsedAttachment
    ) -> list[int]:
        doc_ids: list[int] = []
        chunk_index = 0
        for unit_number, unit_text in enumerate(parsed.units, start=1):
            for content, _local_index in chunk_document(unit_text, self.chunk_chars, self.overlap_chars):
                doc_id = self.storage.upsert_document(
                    attachment.school_id,
                    source_url,
                    attachment.filename,
                    content,
                    chunk_index,
                    sha256(content.encode("utf-8")).hexdigest(),
                    self.embedder.embed(content),
                    source_type=SOURCE_TYPE_ATTACHMENT,
                    page=unit_number if parsed.paginated else None,
                    attachment_id=attachment.attachment_id,
                )
                doc_ids.append(doc_id)
                chunk_index += 1
        return doc_ids
