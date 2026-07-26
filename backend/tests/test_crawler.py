from datetime import datetime, timezone
from uuid import uuid4
import unittest

from app.crawler import CommonNoticeAdapter, Crawler, CrawlSettings, SkkuNoticeAdapter, YonseiNoticeAdapter, html_hash, normalize_detail_url, normalize_url
from app.schemas import CrawlRequest, CrawlScope


LISTING_HTML = """
<table><tbody>
  <tr><td>1</td><td>학사</td><td><a href="/notice/view.do?id=1&utm_source=test">수강신청 안내</a></td><td>학사팀</td><td>2026-07-01</td></tr>
  <tr><td>2</td><td>장학</td><td><a href="/notice/view.do?id=2">장학금 안내</a></td><td>학생팀</td><td>2026-07-02</td></tr>
  <tr><td>3</td><td>일반</td><td><a href="/notice/view.do?id=3">일반 안내</a></td><td>홍보팀</td><td>2026-07-03</td></tr>
</tbody></table>
"""


class FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, pages: dict[str, FakeResponse]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, timeout: float) -> FakeResponse:
        self.calls.append(url)
        return self.pages[url]


class CrawlerTests(unittest.TestCase):
    def request(self) -> CrawlRequest:
        return CrawlRequest(
            crawl_id=uuid4(),
            school_id=1,
            base_url="https://example.edu/notice/list.do",
            mode="initial",
            scope=CrawlScope(allowed_hosts=["example.edu"], path_prefixes=["/notice"], max_listing_pages=10, max_items=300),
        )

    def test_normalize_url_removes_tracking_and_fragment(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://Example.edu/notice/view.do/?b=2&utm_source=x&a=1#section"),
            "https://example.edu/notice/view.do?a=1&b=2",
        )

    def test_normalize_detail_url_removes_listing_context(self) -> None:
        self.assertEqual(
            normalize_detail_url("https://example.edu/notice.do?mode=view&articleNo=1&article.offset=10&articleLimit=10"),
            "https://example.edu/notice.do?articleNo=1&mode=view",
        )

    def test_crawl_classifies_new_changed_and_unchanged(self) -> None:
        one = "https://example.edu/notice/view.do?id=1"
        two = "https://example.edu/notice/view.do?id=2"
        three = "https://example.edu/notice/view.do?id=3"
        session = FakeSession(
            {
                "https://example.edu/notice/list.do": FakeResponse(200, LISTING_HTML),
                one: FakeResponse(200, "<main>첫 번째 본문 <a href='/files/form.hwp'>신청서</a></main>"),
                two: FakeResponse(200, "<main>두 번째 본문</main>"),
                three: FakeResponse(200, "<main>세 번째 본문</main>"),
            }
        )
        one_hash = html_hash(session.pages[one].text)
        crawler = Crawler(
            hash_exists=lambda _school, _url, digest: digest == one_hash,
            url_exists=lambda _school, url: url == two,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        run = crawler.crawl(self.request(), CommonNoticeAdapter())

        self.assertEqual([page.crawl_status for page in run.pages], ["unchanged", "changed", "new"])
        self.assertEqual(len(crawler.pages_for_extractor(run)), 2)
        self.assertEqual(run.pages[0].attachments[0].name_hint, "신청서")
        self.assertEqual(run.pages[0].published_at_hint, datetime(2026, 7, 1, tzinfo=timezone.utc))

    def test_retryable_failure_is_recorded_after_retry_limit(self) -> None:
        session = FakeSession({"https://example.edu/notice/list.do": FakeResponse(503)})
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=1),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        run = crawler.crawl(self.request(), CommonNoticeAdapter())

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(run.failures[0].error_code, "HTTP_503")
        self.assertTrue(run.failures[0].retryable)

    def test_crawl_follows_listing_pagination_until_limit(self) -> None:
        first_list = "https://example.edu/notice/list.do"
        second_list = "https://example.edu/notice/list.do?page=2"
        second_listing_html = """
        <table><tbody><tr><td>4</td><td>학사</td><td><a href='/notice/view.do?id=4'>네 번째</a></td><td>학사팀</td><td>2026-07-04</td></tr></tbody></table>
        """
        session = FakeSession(
            {
                first_list: FakeResponse(200, LISTING_HTML + "<a rel='next' href='/notice/list.do?page=2'>다음</a>"),
                second_list: FakeResponse(200, second_listing_html),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
                "https://example.edu/notice/view.do?id=2": FakeResponse(200, "<main>2</main>"),
                "https://example.edu/notice/view.do?id=3": FakeResponse(200, "<main>3</main>"),
                "https://example.edu/notice/view.do?id=4": FakeResponse(200, "<main>4</main>"),
            }
        )
        request = self.request().model_copy(update={"scope": CrawlScope(allowed_hosts=["example.edu"], path_prefixes=["/notice"], max_listing_pages=2, max_items=4)})
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        run = crawler.crawl(request, CommonNoticeAdapter())

        self.assertEqual(len(run.pages), 4)
        self.assertIn(second_list, session.calls)

    def test_common_adapter_recognizes_k2web_next_page_title(self) -> None:
        html = "<a title='다음 페이지로 이동하기' href='?mode=list&article.offset=10'>다음</a>"

        next_url = CommonNoticeAdapter().next_listing_url(html, "https://example.edu/notice/list.do")

        self.assertEqual(next_url, "https://example.edu/notice/list.do?mode=list&article.offset=10")

    def test_policy_rejection_stops_before_fetch(self) -> None:
        session = FakeSession({})
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            session=session,
            robots_allowed=lambda _url: False,
        )

        run = crawler.crawl(self.request(), CommonNoticeAdapter())

        self.assertEqual(run.failures[0].error_code, "ROBOTS_DISALLOWED")
        self.assertEqual(session.calls, [])

    def test_default_session_identifies_asku_crawler(self) -> None:
        crawler = Crawler(hash_exists=lambda *_args: False)

        self.assertEqual(crawler.session.headers["User-Agent"], "ASKU-Crawler/0.1 (+https://github.com/linklingj/ASKU)")

    def test_yonsei_override_reads_card_list_metadata(self) -> None:
        html = """
        <div class='boardWrap'><ul><li><a href='/bbs/sc/58/1/artclView.do'>
          <span class='notice-title'>일반공지</span><div class='title'>등록금 안내</div>
          <div class='etc-area'>재무팀</div><div class='date-area'>2026.07.24</div>
        </a></li></ul></div>
        <form name='pageForm' action='/bbs/sc/58/artclList.do'>
          <input name='layout' value='abc'><input name='page' value='1'>
        </form><div class='_paging'><a class='_listNext' href="javascript:page_link('2')">다음</a></div>
        """
        adapter = YonseiNoticeAdapter()
        item = next(iter(adapter.parse_listing(html, "https://www.yonsei.ac.kr/sc/254/subview.do")))
        self.assertEqual(item.title_hint, "등록금 안내")
        self.assertEqual(item.category_hint, "일반공지")
        self.assertEqual(item.author_hint, "재무팀")
        self.assertEqual(
            adapter.next_listing_url(html, "https://www.yonsei.ac.kr/sc/254/subview.do"),
            "https://www.yonsei.ac.kr/bbs/sc/58/artclList.do?layout=abc&page=2",
        )

    def test_skku_override_reads_definition_list_metadata(self) -> None:
        html = """
        <dl class='board-list-content-wrap'><dt class='board-list-content-title'><a href='?mode=view&articleNo=1'>학점교류 안내</a></dt>
        <dd class='board-list-content-info'><ul><li>공지</li><li>교무팀</li><li>2026-07-24</li><li>조회수 10</li></ul></dd></dl>
        """
        item = next(iter(SkkuNoticeAdapter().parse_listing(html, "https://www.skku.edu/skku/campus/skk_comm/notice02.do")))

        self.assertEqual(item.title_hint, "학점교류 안내")
        self.assertEqual(item.category_hint, "공지")
        self.assertEqual(item.author_hint, "교무팀")
        self.assertEqual(item.published_at_hint, datetime(2026, 7, 24, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
