from datetime import datetime, timezone
from uuid import uuid4
import unittest

from app.extractor import DocumentExtractor, K2WebContentParser
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


def page(
    *,
    html: str = "<main><h1>장학금 안내</h1><p>본문 내용</p></main>",
    status: str = "new",
    canonical_url: str = "https://example.edu/notices/1",
) -> CrawledPage:
    return CrawledPage(
        crawl_id=uuid4(), school_id=1, source_url="https://example.edu/notices/1",
        canonical_url=canonical_url, title_hint="목록 제목", category_hint="장학",
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

    def test_sejong_parser_selects_post_content_instead_of_site_navigation(self) -> None:
        html = """
            <main>
              <section class="quick-menu"><p>세종소개</p><p>챗봇 준비중입니다.</p></section>
              <div class="b-view-common-wrap">
                <p class="b-title">학생생활상담소 계약직원 모집공고</p>
                <div class="b-content-box"><div class="fr-view">
                  <p>모집분야: 계약직원</p><p>지원 방법: 이메일 접수</p>
                </div></div>
              </div>
            </main>
        """
        llm = FakeLLM([self.valid()])

        out = DocumentExtractor(llm, parsers={1: K2WebContentParser()}, sleeper=lambda _: None).process(page(html=html))

        self.assertEqual(out[0].content, "모집분야: 계약직원\n지원 방법: 이메일 접수")
        self.assertNotIn("세종소개", llm.calls[0])
        self.assertNotIn("챗봇", llm.calls[0])

    def test_sejong_parser_falls_back_to_common_parser_when_selector_changes(self) -> None:
        llm = FakeLLM([self.valid()])
        out = DocumentExtractor(llm, parsers={1: K2WebContentParser()}, sleeper=lambda _: None).process(
            page(html="<main><p>선택자 변경 뒤에도 읽을 수 있는 본문</p></main>")
        )

        self.assertEqual(out[0].content, "선택자 변경 뒤에도 읽을 수 있는 본문")

    def test_k2web_parser_is_chosen_by_host_and_drops_next_article_titles(self) -> None:
        """홍익대는 본문 바로 아래에 다음 글 목록이 붙는다. 공용 파서는 이를 본문에 섞는다."""

        html = """
            <main>
              <div class="b-content-box"><div class="fr-view"><p>중단일시: 8월 8일</p></div></div>
              <ul class="b-list">
                <li><a href="/kr/newscenter/notice.do?articleNo=2">다음 글 제목</a></li>
              </ul>
            </main>
        """
        llm = FakeLLM([self.valid()])
        url = "https://www.hongik.ac.kr/kr/newscenter/notice.do?articleNo=1"

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page(html=html, canonical_url=url))

        self.assertEqual(out[0].content, "중단일시: 8월 8일")
        self.assertNotIn("다음 글 제목", llm.calls[0])

    def test_unregistered_host_still_uses_common_parser(self) -> None:
        llm = FakeLLM([self.valid()])

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page())

        self.assertIn("본문 내용", out[0].content)

    def test_crawler_category_is_preserved_when_llm_omits_it(self) -> None:
        llm = FakeLLM([Extraction(
            entities=[ExtractedEntity(type="공지", name="목록 제목")],
            relations=[],
        )])

        out = DocumentExtractor(llm, sleeper=lambda _: None).process(page())

        self.assertIn(("주제·카테고리", "장학"), {(item.type, item.name) for item in out[0].entities})
        self.assertIn(
            ("목록 제목", "분류", "장학"),
            {(item.source, item.relation, item.target) for item in out[0].relations},
        )

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
        self.assertEqual([entity.name for entity in out[0].entities], ["목록 제목", "장학"])
        self.assertEqual(
            [(relation.source, relation.relation, relation.target) for relation in out[0].relations],
            [("목록 제목", "분류", "장학")],
        )

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
