"""PdfIngestor의 DB·pdfplumber 독립 계약 테스트.

Storage(PdfIngestStorage)와 Embedder를 가짜로 주입하고, page_extractor를 주입해
실제 PDF 바이트·pdfplumber 없이 페이지별 청킹·메타데이터 저장을 검증한다.
"""

import unittest

from app.llm import Embedder
from app.models import EMBEDDING_DIM
from app.pdf_ingest import PdfIngestor


class FakeEmbedder(Embedder):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.0] * EMBEDDING_DIM


class FakeStorage:
    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self._next_id = 1

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
    ):
        self.upserts.append(
            {
                "school_id": school_id,
                "source_url": source_url,
                "title": title,
                "content": content,
                "chunk_index": chunk_index,
                "content_hash": content_hash,
                "source_type": source_type,
                "page": page,
            }
        )
        doc_id = self._next_id
        self._next_id += 1
        return doc_id


def pages_of(*texts: str):
    """고정된 페이지 텍스트 목록을 돌려주는 page_extractor 팩토리."""

    return lambda pdf_bytes: list(texts)


ATTACHMENT_URL = "https://example.edu/files/guide.pdf"


class PdfIngestorTests(unittest.TestCase):
    def test_ingests_one_chunk_per_page_with_page_number_and_pdf_source_type(self) -> None:
        storage = FakeStorage()
        embedder = FakeEmbedder()
        ingestor = PdfIngestor(storage, embedder, page_extractor=pages_of("1페이지 본문", "2페이지 본문"))

        result = ingestor.ingest(1, ATTACHMENT_URL, "수강편람.pdf", b"%PDF-1.4 ...")

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.chunk_count, 2)
        self.assertEqual([u["page"] for u in storage.upserts], [1, 2])
        self.assertEqual([u["source_type"] for u in storage.upserts], ["pdf", "pdf"])
        self.assertEqual([u["chunk_index"] for u in storage.upserts], [0, 1])
        self.assertEqual([u["content"] for u in storage.upserts], ["1페이지 본문", "2페이지 본문"])
        self.assertTrue(all(u["title"] == "수강편람.pdf" for u in storage.upserts))

    def test_source_url_is_the_real_attachment_link(self) -> None:
        """근거 인용과 재크롤 미관측 판정이 모두 실제 첨부 URL을 기준으로 동작해야 한다."""
        storage = FakeStorage()
        ingestor = PdfIngestor(storage, FakeEmbedder(), page_extractor=pages_of("본문", "본문2"))

        result = ingestor.ingest(1, ATTACHMENT_URL, "수강편람.pdf", b"...")

        self.assertEqual(result.source_url, ATTACHMENT_URL)
        self.assertTrue(all(u["source_url"] == ATTACHMENT_URL for u in storage.upserts))

    def test_empty_page_produces_no_chunk(self) -> None:
        storage = FakeStorage()
        ingestor = PdfIngestor(storage, FakeEmbedder(), page_extractor=pages_of("본문 있음", ""))

        result = ingestor.ingest(1, ATTACHMENT_URL, "파일.pdf", b"...")

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.chunk_count, 1)  # 빈 페이지는 청크를 만들지 않음

    def test_long_page_splits_into_multiple_chunks_sharing_the_same_page_number(self) -> None:
        storage = FakeStorage()
        long_text = "\n\n".join(f"문단{i} " + "내용" * 400 for i in range(5))
        ingestor = PdfIngestor(
            storage, FakeEmbedder(), chunk_chars=200, overlap_chars=20, page_extractor=pages_of(long_text)
        )

        result = ingestor.ingest(1, ATTACHMENT_URL, "긴파일.pdf", b"...")

        self.assertGreater(result.chunk_count, 1)
        self.assertTrue(all(u["page"] == 1 for u in storage.upserts))  # 같은 페이지에서 나온 여러 청크
        self.assertEqual([u["chunk_index"] for u in storage.upserts], list(range(result.chunk_count)))

    def test_reingesting_the_same_attachment_is_idempotent_on_the_unique_key(self) -> None:
        """재크롤에서 같은 첨부를 다시 받아도 같은 (source_url, content_hash, chunk_index)라 멱등하다."""
        storage = FakeStorage()
        ingestor = PdfIngestor(storage, FakeEmbedder(), page_extractor=pages_of("본문"))

        ingestor.ingest(1, ATTACHMENT_URL, "동일파일.pdf", b"%PDF-1.4 same-file")
        ingestor.ingest(1, ATTACHMENT_URL, "동일파일.pdf", b"%PDF-1.4 same-file")

        first, second = storage.upserts
        self.assertEqual(
            (first["source_url"], first["content_hash"], first["chunk_index"]),
            (second["source_url"], second["content_hash"], second["chunk_index"]),
        )


if __name__ == "__main__":
    unittest.main()
