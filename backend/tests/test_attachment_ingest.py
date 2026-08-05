"""첨부 문서 인제스트의 DB·모델 독립 계약 테스트.

Storage·Embedder 를 가짜로 주입해 Postgres·bge-m3 없이 파싱·청킹·상태 전이를
검증한다. 포맷 파서는 바이트를 직접 만들어(HWP 레코드·HWPX zip) 실제 코드를 태운다.
"""

from __future__ import annotations

import io
import unittest
import zipfile

from app.attachment_ingest import (
    AttachmentIngestor,
    AttachmentParseError,
    ParsedAttachment,
    UnsupportedAttachmentError,
    _decode_hwp_paragraph,
    _hwp_section_text,
    extract_hwpx_sections,
    extract_plain_text,
    is_supported,
    parse_attachment,
)
from app.llm import Embedder
from app.models import EMBEDDING_DIM, SOURCE_TYPE_ATTACHMENT, Attachment


class FakeEmbedder(Embedder):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.0] * EMBEDDING_DIM


class FakeStorage:
    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.status_calls: list[tuple] = []
        self._next_doc_id = 1

    def upsert_document(
        self,
        school_id,
        source_url,
        title,
        content,
        chunk_index,
        content_hash,
        embedding,
        *,
        source_type="web",
        page=None,
        attachment_id=None,
    ) -> int:
        doc_id = self._next_doc_id
        self._next_doc_id += 1
        self.documents.append(
            {
                "doc_id": doc_id,
                "school_id": school_id,
                "source_url": source_url,
                "title": title,
                "content": content,
                "chunk_index": chunk_index,
                "content_hash": content_hash,
                "source_type": source_type,
                "page": page,
                "attachment_id": attachment_id,
            }
        )
        return doc_id

    def update_attachment_status(
        self, school_id, attachment_id, status, *, page_count=None, chunk_count=None, error_code=None
    ):
        self.status_calls.append((status, page_count, chunk_count, error_code))
        return None

    def statuses(self) -> list[str]:
        return [call[0] for call in self.status_calls]


def make_attachment(filename: str = "수강편람.pdf") -> Attachment:
    return Attachment(
        attachment_id=7,
        school_id=1,
        filename=filename,
        content_type="application/pdf",
        byte_size=1024,
        file_hash="hash",
    )


def ingestor(storage, *, parser, **kwargs) -> AttachmentIngestor:
    return AttachmentIngestor(storage, FakeEmbedder(), parser=parser, **kwargs)


# ── 인제스트 흐름 ──────────────────────────────────────────────────────


class IngestTests(unittest.TestCase):
    def test_paginated_units_become_chunks_with_page_numbers(self) -> None:
        storage = FakeStorage()
        parsed = ParsedAttachment(units=["1페이지 본문", "2페이지 본문"], paginated=True)

        result = ingestor(storage, parser=lambda name, data: parsed).ingest(make_attachment(), b"x")

        self.assertEqual(result.unit_count, 2)
        self.assertEqual(result.chunk_count, 2)
        self.assertEqual([doc["page"] for doc in storage.documents], [1, 2])
        self.assertEqual([doc["chunk_index"] for doc in storage.documents], [0, 1])
        for doc in storage.documents:
            self.assertEqual(doc["source_type"], SOURCE_TYPE_ATTACHMENT)
            self.assertEqual(doc["source_url"], "attachment://7")  # 합성 URI (원문 링크 없음)
            self.assertEqual(doc["attachment_id"], 7)
            self.assertEqual(doc["title"], "수강편람.pdf")

    def test_unpaginated_format_leaves_page_empty(self) -> None:
        storage = FakeStorage()
        parsed = ParsedAttachment(units=["안내 본문"], paginated=False)

        ingestor(storage, parser=lambda name, data: parsed).ingest(make_attachment("안내.txt"), b"x")

        self.assertEqual([doc["page"] for doc in storage.documents], [None])

    def test_long_unit_is_split_into_multiple_chunks(self) -> None:
        storage = FakeStorage()
        parsed = ParsedAttachment(units=["가" * 250], paginated=True)

        result = ingestor(
            storage, parser=lambda name, data: parsed, chunk_chars=100, overlap_chars=10
        ).ingest(make_attachment(), b"x")

        self.assertGreater(result.chunk_count, 1)
        # 같은 페이지에서 나온 청크는 페이지 번호를 공유하고 chunk_index 만 이어진다
        self.assertEqual({doc["page"] for doc in storage.documents}, {1})
        self.assertEqual(
            [doc["chunk_index"] for doc in storage.documents], list(range(result.chunk_count))
        )

    def test_status_moves_indexing_then_ready_with_counts(self) -> None:
        storage = FakeStorage()
        parsed = ParsedAttachment(units=["본문 1", "본문 2"], paginated=True)

        ingestor(storage, parser=lambda name, data: parsed).ingest(make_attachment(), b"x")

        self.assertEqual(storage.statuses(), ["indexing", "ready"])
        self.assertEqual(storage.status_calls[-1], ("ready", 2, 2, None))

    def test_parse_failure_marks_attachment_failed_and_reraises(self) -> None:
        storage = FakeStorage()

        def failing_parser(name, data):
            raise AttachmentParseError("HWP_ENCRYPTED", "암호 문서")

        with self.assertRaises(AttachmentParseError):
            ingestor(storage, parser=failing_parser).ingest(make_attachment(), b"x")

        self.assertEqual(storage.statuses(), ["indexing", "failed"])
        self.assertEqual(storage.status_calls[-1][3], "HWP_ENCRYPTED")  # 원인 코드 보존
        self.assertEqual(storage.documents, [])

    def test_text_free_file_fails_with_empty_content(self) -> None:
        """스캔본 PDF 처럼 텍스트 계층이 없는 파일은 ready 로 넘기지 않는다."""

        storage = FakeStorage()
        parsed = ParsedAttachment(units=["", "   "], paginated=True)

        with self.assertRaises(AttachmentParseError):
            ingestor(storage, parser=lambda name, data: parsed).ingest(make_attachment(), b"x")

        self.assertEqual(storage.status_calls[-1][3], "EMPTY_CONTENT")

    def test_unsaved_attachment_is_rejected(self) -> None:
        attachment = make_attachment().model_copy(update={"attachment_id": None})

        with self.assertRaises(ValueError):
            ingestor(FakeStorage(), parser=lambda name, data: None).ingest(attachment, b"x")


# ── 포맷 판별 ──────────────────────────────────────────────────────────


class FormatDispatchTests(unittest.TestCase):
    def test_supported_extensions_are_case_insensitive(self) -> None:
        self.assertTrue(is_supported("수강편람.PDF"))
        self.assertTrue(is_supported("학칙.hwp"))
        self.assertFalse(is_supported("사진.png"))
        self.assertFalse(is_supported("확장자없음"))

    def test_unsupported_extension_raises_with_guidance(self) -> None:
        with self.assertRaises(UnsupportedAttachmentError) as raised:
            parse_attachment("사진.png", b"x")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_FILE_TYPE")
        self.assertIn(".pdf", raised.exception.message)

    def test_plain_text_falls_back_to_cp949(self) -> None:
        self.assertEqual(extract_plain_text("한글 본문".encode("utf-8")), ["한글 본문"])
        self.assertEqual(extract_plain_text("한글 본문".encode("cp949")), ["한글 본문"])

    def test_txt_is_parsed_as_a_single_unpaginated_unit(self) -> None:
        parsed = parse_attachment("안내.txt", "본문".encode("utf-8"))

        self.assertEqual(parsed.units, ["본문"])
        self.assertFalse(parsed.paginated)


# ── PDF 파싱 ──────────────────────────────────────────────────────────


def make_pdf(pages: list[str]) -> bytes:
    """페이지마다 한 줄만 있는 최소 PDF 를 만든다(외부 생성 라이브러리 없이)."""

    page_count = len(pages)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [{}] /Count {} >>".format(
            " ".join(f"{3 + index * 2} 0 R" for index in range(page_count)), page_count
        ).encode(),
    ]
    for index, text in enumerate(pages):
        content_ref = 4 + index * 2
        font_ref = 3 + page_count * 2
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_ref} 0 R "
            f"/Resources << /Font << /F1 {font_ref} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(out)


class PdfTests(unittest.TestCase):
    def test_each_pdf_page_becomes_its_own_unit(self) -> None:
        parsed = parse_attachment("수강편람.pdf", make_pdf(["First page", "Second page"]))

        self.assertEqual(parsed.units, ["First page", "Second page"])
        self.assertTrue(parsed.paginated)  # 인용에 페이지 번호가 붙는다

    def test_broken_pdf_raises_parse_error(self) -> None:
        with self.assertRaises(AttachmentParseError):
            parse_attachment("깨진.pdf", b"%PDF-1.4 truncated")


# ── HWP 5.0 레코드 파싱 ────────────────────────────────────────────────


def hwp_para_text_record(text: str) -> bytes:
    """HWPTAG_PARA_TEXT(67) 레코드 하나를 만든다. 헤더는 tag|level|size 비트필드."""

    payload = text.encode("utf-16-le")
    header = 67 | (0 << 10) | (len(payload) << 20)
    return header.to_bytes(4, "little") + payload


def hwp_other_record(tag_id: int, payload: bytes) -> bytes:
    header = tag_id | (0 << 10) | (len(payload) << 20)
    return header.to_bytes(4, "little") + payload


class HwpRecordTests(unittest.TestCase):
    def test_section_text_collects_only_paragraph_records(self) -> None:
        stream = (
            hwp_other_record(66, b"\x00" * 8)  # 문단 헤더 — 본문 아님
            + hwp_para_text_record("첫 문단")
            + hwp_other_record(68, b"\x01" * 4)  # 글자 모양 — 본문 아님
            + hwp_para_text_record("둘째 문단")
        )

        self.assertEqual(_hwp_section_text(stream, compressed=False), "첫 문단\n둘째 문단")

    def test_empty_paragraphs_are_dropped(self) -> None:
        stream = hwp_para_text_record("본문") + hwp_para_text_record("   ")

        self.assertEqual(_hwp_section_text(stream, compressed=False), "본문")

    def test_extended_control_characters_are_skipped_whole(self) -> None:
        """표·그림 제어 문자는 코드 단위 8개를 차지한다. 통째로 건너뛰어야 한다."""

        text = "앞" + "\x0b" + "\x00" * 6 + "\x0b" + "뒤"

        self.assertEqual(_decode_hwp_paragraph(text.encode("utf-16-le")), "앞뒤")

    def test_line_break_control_becomes_newline(self) -> None:
        text = "첫 줄" + "\x0a" + "둘째 줄"

        self.assertEqual(_decode_hwp_paragraph(text.encode("utf-16-le")), "첫 줄\n둘째 줄")

    def test_large_record_uses_extended_size_field(self) -> None:
        """payload 가 0xFFF 이상이면 뒤따르는 4바이트가 실제 크기다."""

        body = "가" * 3000
        payload = body.encode("utf-16-le")
        header = 67 | (0 << 10) | (0xFFF << 20)
        stream = header.to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload

        self.assertEqual(_hwp_section_text(stream, compressed=False), body)

    def test_compressed_section_is_inflated(self) -> None:
        import zlib

        compressor = zlib.compressobj(wbits=-15)
        raw = hwp_para_text_record("압축 본문")
        compressed = compressor.compress(raw) + compressor.flush()

        self.assertEqual(_hwp_section_text(compressed, compressed=True), "압축 본문")

    def test_corrupt_compressed_section_raises_parse_error(self) -> None:
        with self.assertRaises(AttachmentParseError):
            _hwp_section_text(b"not-deflate-data", compressed=True)


# ── HWPX(zip + XML) 파싱 ──────────────────────────────────────────────


def make_hwpx(sections: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        for name, xml in sections.items():
            archive.writestr(name, xml)
    return buffer.getvalue()


_HWPX_SECTION = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
    "<hp:p><hp:run><hp:t>{first}</hp:t></hp:run></hp:p>"
    "<hp:p><hp:run><hp:t>{second_a}</hp:t></hp:run><hp:run><hp:t>{second_b}</hp:t></hp:run></hp:p>"
    "<hp:p><hp:run><hp:t></hp:t></hp:run></hp:p>"
    "</hs:sec>"
)


class HwpxTests(unittest.TestCase):
    def test_sections_are_read_in_numeric_order(self) -> None:
        data = make_hwpx(
            {
                "Contents/section10.xml": _HWPX_SECTION.format(
                    first="열번째", second_a="이어", second_b="붙임"
                ),
                "Contents/section2.xml": _HWPX_SECTION.format(
                    first="두번째", second_a="이어", second_b="붙임"
                ),
            }
        )

        sections = extract_hwpx_sections(data)

        # section10 이 section2 보다 앞서면 안 된다(문자열 정렬 함정)
        self.assertEqual([section.splitlines()[0] for section in sections], ["두번째", "열번째"])

    def test_runs_inside_a_paragraph_join_and_empty_paragraphs_drop(self) -> None:
        data = make_hwpx(
            {"Contents/section0.xml": _HWPX_SECTION.format(first="첫 줄", second_a="이어", second_b="붙임")}
        )

        self.assertEqual(extract_hwpx_sections(data), ["첫 줄\n이어붙임"])

    def test_hwpx_without_body_raises_parse_error(self) -> None:
        data = make_hwpx({"Contents/header.xml": "<root/>"})

        with self.assertRaises(AttachmentParseError):
            extract_hwpx_sections(data)

    def test_non_zip_bytes_raise_parse_error(self) -> None:
        with self.assertRaises(AttachmentParseError):
            extract_hwpx_sections(b"not a zip")


if __name__ == "__main__":
    unittest.main()
