from datetime import datetime, timezone
from uuid import uuid4
import unittest

from app.extractor import DocumentExtractor
from app.llm import Extraction, Extractor as LLMExtractor
from app.schemas import Attachment, CrawledPage, ExtractedEntity, ExtractedRelation, ExtractionFailure


class FakeLLM(LLMExtractor):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def extract(self, text: str) -> Extraction:
        self.calls.append(text)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def page(*, html: str = "<main><h1>장학금 안내</h1><p>본문 내용</p></main>", status: str = "new") -> CrawledPage:
    return CrawledPage(
        crawl_id=uuid4(), school_id=1, source_url="https://example.edu/notices/1",
        canonical_url="https://example.edu/notices/1", title_hint="목록 제목", category_hint="장학",
        author_hint="학생지원팀", published_at_hint=datetime(2026, 7, 1, tzinfo=timezone.utc),
        raw_html=html, attachments=[Attachment(url="https://example.edu/form.hwp", name_hint="신청서")],
        content_hash="hash", fetched_at=datetime(2026, 7, 2, tzinfo=timezone.utc), crawl_status=status,
    )


class ExtractorTests(unittest.TestCase):
    def valid(self) -> Extraction:
        return Extraction(
            entities=[
                ExtractedEntity(type="공지", name="목록 제목"),
                ExtractedEntity(type="부서·기관", name="학생지원팀"),
            ],
            relations=[ExtractedRelation(source="목록 제목", relation="게시", target="학생지원팀")],
        )

    def test_process_cleans_html_and_includes_metadata_for_llm(self) -> None:
        llm = FakeLLM([self.valid()])
        out = DocumentExtractor(llm, sleeper=lambda _: None).process(
            page(html="<nav>메뉴</nav><main><h1>HTML 제목</h1><p>첫 문단</p><script>bad()</script><p>둘째 문단</p></main><footer>푸터</footer>")
        )

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "목록 제목")
        self.assertEqual(out[0].content, "HTML 제목\n첫 문단\n둘째 문단")
        self.assertNotIn("메뉴", llm.calls[0])
        self.assertIn("허용 엔티티 타입:", llm.calls[0])
        self.assertIn("공지", llm.calls[0])
        self.assertIn("분류: 장학", llm.calls[0])
        self.assertIn("첨부 링크: 신청서", llm.calls[0])

    def test_html_inline_tags_do_not_split_words_across_lines(self) -> None:
        llm = FakeLLM([self.valid()])
        out = DocumentExtractor(llm, sleeper=lambda _: None).process(
            page(html="<main><p>등록금의 <span>100</span>분의 <strong>2</strong>를 납부합니다.</p></main>")
        )

        self.assertEqual(out[0].content, "등록금의 100 분의 2 를 납부합니다.")
        self.assertNotIn("100\n분의", out[0].content)

    def test_common_parser_removes_explicit_ui_lines_and_adjacent_repeated_blocks(self) -> None:
        llm = FakeLLM([self.valid()])
        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page(html="""
            <main>
              <article><h1>목록 제목</h1><p>실제 공지 본문입니다.</p><p>실제 공지 본문입니다.</p></article>
              <div class='prev-next'><p>이전글 이전글이 없습니다.</p><p>다음글 다음글이 없습니다.</p></div>
            </main>
        """))

        self.assertEqual(out[0].content, "목록 제목\n실제 공지 본문입니다.")
        self.assertNotIn("이전글", llm.calls[0])

    def test_reversely_generated_post_relation_is_normalized(self) -> None:
        llm = FakeLLM([Extraction(
            entities=[
                ExtractedEntity(type="공지", name="목록 제목"),
                ExtractedEntity(type="부서·기관", name="학생지원팀"),
            ],
            relations=[ExtractedRelation(source="학생지원팀", relation="게시", target="목록 제목")],
        )])

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page())

        self.assertEqual(out[0].relations[0].source, "목록 제목")
        self.assertEqual(out[0].relations[0].target, "학생지원팀")

    def test_whitelist_and_unknown_relation_make_partial(self) -> None:
        llm = FakeLLM([Extraction(
            entities=[
                ExtractedEntity(type="공지", name="목록 제목"),
                ExtractedEntity(type="임의타입", name="버려짐"),
            ],
            relations=[
                ExtractedRelation(source="목록 제목", relation="안내", target="없는 엔티티"),
                ExtractedRelation(source="목록 제목", relation="임의관계", target="목록 제목"),
            ],
        )])

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page())

        self.assertEqual(out[0].extraction_status, "partial")
        self.assertEqual([entity.name for entity in out[0].entities], ["목록 제목"])
        self.assertEqual(out[0].relations, [])

    def test_retries_transient_llm_error(self) -> None:
        llm = FakeLLM([RuntimeError("temporary"), RuntimeError("temporary"), self.valid()])
        pauses: list[float] = []

        out = DocumentExtractor(llm, sleeper=pauses.append).process(page())

        self.assertEqual(len(out), 1)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(pauses, [1.0, 2.0])

    def test_all_llm_failures_return_failure(self) -> None:
        llm = FakeLLM([RuntimeError("down"), RuntimeError("down"), RuntimeError("down")])

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page())

        self.assertIsInstance(out, ExtractionFailure)
        self.assertEqual(out.error_code, "LLM_EXTRACTION_FAILED")
        self.assertTrue(out.retryable)

    def test_one_failed_chunk_keeps_other_chunks_as_partial(self) -> None:
        llm = FakeLLM([self.valid(), RuntimeError("down"), RuntimeError("down"), RuntimeError("down")])
        extractor = DocumentExtractor(llm, chunk_chars=12, overlap_chars=2, sleeper=lambda _: None)

        out = extractor.process(page(html="<main>첫 문단\n둘째 문단\n셋째 문단</main>"))

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].chunk_index, 0)
        self.assertEqual(out[0].extraction_status, "partial")

    def test_long_document_chunks_with_overlap(self) -> None:
        extractor = DocumentExtractor(FakeLLM([]), chunk_chars=20, overlap_chars=4, sleeper=lambda _: None)
        chunks = extractor.chunk("가" * 25)

        self.assertEqual([index for _, index in chunks], [0, 1])
        self.assertEqual(chunks[0][0][-4:], chunks[1][0][:4])

    def test_chunk_prefers_html_block_boundaries(self) -> None:
        extractor = DocumentExtractor(FakeLLM([]), chunk_chars=12, overlap_chars=2, sleeper=lambda _: None)

        chunks = extractor.chunk("첫 문단\n둘째 문단\n셋째 문단")

        self.assertEqual(chunks[0][0], "첫 문단\n\n둘째 문단")
        self.assertTrue(chunks[1][0].endswith("셋째 문단"))

    def test_unchanged_page_is_not_extracted(self) -> None:
        llm = FakeLLM([])
        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page(status="unchanged"))

        self.assertIsInstance(out, ExtractionFailure)
        self.assertEqual(out.error_code, "UNSUPPORTED_CRAWL_STATUS")
        self.assertEqual(llm.calls, [])

    def test_empty_body_returns_failure(self) -> None:
        out = DocumentExtractor(FakeLLM([]), sleeper=lambda _: None).process(page(html="<html><body><nav>메뉴</nav></body></html>"))

        self.assertIsInstance(out, ExtractionFailure)
        self.assertEqual(out.error_code, "EMPTY_CONTENT")


if __name__ == "__main__":
    unittest.main()
