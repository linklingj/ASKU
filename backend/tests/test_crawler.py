from datetime import datetime, timezone
from uuid import uuid4
import unittest

import requests

from app.crawler import (
    Board,
    CommonNoticeAdapter,
    Crawler,
    CrawlSettings,
    HongikNoticeAdapter,
    SejongNoticeAdapter,
    SkkuNoticeAdapter,
    YonseiNoticeAdapter,
    adapter_for,
    boards_for,
    html_hash,
    normalize_detail_url,
    normalize_url,
)
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
        self.request_headers: list[dict[str, str] | None] = []

    def get(self, url: str, timeout: float, headers: dict[str, str] | None = None) -> FakeResponse:
        self.calls.append(url)
        self.request_headers.append(headers)
        return self.pages[url]


class FakeStorage:
    def __init__(self, existing_hashes: set[tuple[int, str, str]], existing_urls: set[tuple[int, str]]) -> None:
        self.existing_hashes = existing_hashes
        self.existing_urls = existing_urls

    def doc_hash_exists(self, school_id: int, source_url: str, content_hash: str) -> bool:
        return (school_id, source_url, content_hash) in self.existing_hashes

    def doc_url_exists(self, school_id: int, source_url: str) -> bool:
        return (school_id, source_url) in self.existing_urls


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
        self.assertEqual(session.request_headers[1], {"Referer": "https://example.edu/notice/list.do"})

    def test_crawler_from_storage_uses_hash_and_url_history(self) -> None:
        storage = FakeStorage({(1, "https://example.edu/notice/view.do?id=1", "same")}, { (1, "https://example.edu/notice/view.do?id=2") })
        crawler = Crawler.from_storage(storage)

        self.assertTrue(crawler.hash_exists(1, "https://example.edu/notice/view.do?id=1", "same"))
        self.assertTrue(crawler.url_exists(1, "https://example.edu/notice/view.do?id=2"))

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

    def test_sejong_override_reads_metadata_and_next_page(self) -> None:
        """세종대 실제 목록의 메타데이터와 K2Web 페이지네이션을 검증한다."""
        html = """
        <table><tbody><tr class='b-top-box'>
          <td class='b-num-box'><span class='b-noti'>공지</span></td>
          <td class='b-td-title b-td-left'><div class='b-title-box'>
            <a href='?mode=view&amp;articleNo=890806&amp;article.offset=0&amp;articleLimit=10'>
              <span class='b-title'>학생생활상담소 계약직원 모집</span>
            </a>
          </div><div class='b-m-con'><span class='b-writer'>총무과</span><span class='b-date'>2026.07.24</span></div></td>
          <td>총무과</td><td>2026.07.24</td><td class='b-hit-box'>216</td>
        </tr></tbody></table>
        <div class='b-paging'><div class='b-paging-wrap'>
          <li class='next pager'><a href='?mode=list&amp;articleLimit=10&amp;article.offset=10'
            title='다음 페이지로 이동하기'><span class='hide'>다음 페이지로 이동하기</span></a></li>
        </div></div>
        """
        listing_url = "https://www.sejong.ac.kr/kor/intro/notice1.do"
        adapter = SejongNoticeAdapter()
        item = next(iter(adapter.parse_listing(html, listing_url)))

        self.assertEqual(item.title_hint, "학생생활상담소 계약직원 모집")
        self.assertEqual(item.author_hint, "총무과")
        self.assertEqual(item.published_at_hint, datetime(2026, 7, 24, tzinfo=timezone.utc))
        self.assertEqual(
            adapter.next_listing_url(html, listing_url),
            f"{listing_url}?mode=list&articleLimit=10&article.offset=10",
        )

    def test_adapter_for_resolves_registered_hosts(self) -> None:
        """등록된 학교는 전용 어댑터로, 그 외에는 공용 어댑터로 내려간다."""
        cases = {
            "https://www.yonsei.ac.kr/sc/254/subview.do": YonseiNoticeAdapter,
            "https://www.sejong.ac.kr/kor/intro/notice1.do": SejongNoticeAdapter,
            "https://WWW.HONGIK.AC.KR/kr/newscenter/notice.do": HongikNoticeAdapter,
            "https://www.skku.edu/skku/campus/skk_comm/notice02.do": SkkuNoticeAdapter,
            "https://example.edu/notice/list.do": CommonNoticeAdapter,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertIsInstance(adapter_for(url), expected)

    def test_sejong_override_reads_pinned_and_normal_rows(self) -> None:
        """``b-top-box``(상단 고정공지)가 아닌 일반 공지 행도 수집한다."""
        html = """
        <table><tbody>
          <tr class='b-top-box'>
            <td class='b-td-title'><div class='b-title-box'>
              <a href='?mode=view&amp;articleNo=1'><span class='b-title'>고정 공지</span></a>
            </div><div class='b-m-con'><span class='b-writer'>총무과</span>
              <span class='b-date'>2026.07.24</span></div></td>
          </tr>
          <tr>
            <td class='b-td-title'><div class='b-title-box'>
              <a href='?mode=view&amp;articleNo=2'><span class='b-title'>일반 공지</span></a>
            </div><div class='b-m-con'><span class='b-writer'>학사팀</span>
              <span class='b-date'>2026.07.20</span></div></td>
          </tr>
        </tbody></table>
        """
        items = list(SejongNoticeAdapter().parse_listing(html, "https://www.sejong.ac.kr/kor/intro/notice1.do"))

        self.assertEqual([item.title_hint for item in items], ["고정 공지", "일반 공지"])
        self.assertEqual(items[1].author_hint, "학사팀")

    def test_hongik_override_reads_metadata_and_next_page(self) -> None:
        """홍익대 실제 목록의 메타데이터와 K2Web 페이지네이션을 검증한다."""
        html = """
        <table><tbody><tr class='b-top-box'>
          <td class='b-num-box'>1819</td>
          <td class='b-td-left'><div class='b-title-box'>
            <div class='b-cate-box'><span class='b-mini-cate'>일반</span></div>
            <a href='?mode=view&amp;articleNo=154546&amp;article.offset=0&amp;articleLimit=10&amp;noCat=29'>
              <span class='b-title'>바이오메디컬아티스트 서머캠프 모집</span>
            </a>
            <div class='b-m-con'><span class='b-date'>2026.07.24</span></div>
          </div></td><td>370</td><td>2026.07.24</td>
        </tr></tbody></table>
        <div class='b-paging'><div class='b-paging-wrap'>
          <li class='next pager'><a href='?mode=list&amp;articleLimit=10&amp;article.offset=10'
            title='다음 페이지로 이동하기'><span class='hide'>다음 페이지로 이동하기</span></a></li>
        </div></div>
        """
        listing_url = "https://www.hongik.ac.kr/kr/newscenter/notice.do"
        adapter = HongikNoticeAdapter()
        item = next(iter(adapter.parse_listing(html, listing_url)))

        self.assertEqual(item.title_hint, "바이오메디컬아티스트 서머캠프 모집")
        self.assertEqual(item.category_hint, "일반")
        self.assertIsNone(item.author_hint)
        self.assertEqual(item.published_at_hint, datetime(2026, 7, 24, tzinfo=timezone.utc))
        self.assertEqual(
            adapter.next_listing_url(html, listing_url),
            f"{listing_url}?mode=list&articleLimit=10&article.offset=10",
        )

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
          <div class='etc-area'>재무팀</div><div class='date-area'><span>작성일</span>2026.07.24</div>
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
        <li><a href='?mode=list&amp;articleLimit=10&amp;article.offset=10' class='pg_next' title='다음 페이지로 이동하기'>다음</a></li>
        """
        adapter = SkkuNoticeAdapter()
        item = next(iter(adapter.parse_listing(html, "https://www.skku.edu/skku/campus/skk_comm/notice02.do")))

        self.assertEqual(item.title_hint, "학점교류 안내")
        self.assertEqual(item.category_hint, "공지")
        self.assertEqual(item.author_hint, "교무팀")
        self.assertEqual(item.published_at_hint, datetime(2026, 7, 24, tzinfo=timezone.utc))
        self.assertEqual(
            adapter.next_listing_url(html, "https://www.skku.edu/skku/campus/skk_comm/notice02.do"),
            "https://www.skku.edu/skku/campus/skk_comm/notice02.do?mode=list&articleLimit=10&article.offset=10",
        )

    def test_skku_override_does_not_treat_notice_number_as_category(self) -> None:
        html = """
        <dl class='board-list-content-wrap'><dt class='board-list-content-title'><a href='?mode=view&articleNo=2'>학점교류 안내</a></dt>
        <dd class='board-list-content-info'><ul><li>No.2146</li><li>교무팀</li><li>2026-07-24</li></ul></dd></dl>
        """

        item = next(iter(SkkuNoticeAdapter().parse_listing(html, "https://www.skku.edu/skku/campus/skk_comm/notice02.do")))

        self.assertIsNone(item.category_hint)
        self.assertEqual(item.author_hint, "교무팀")


class RecordingSleeper:
    """호출된 대기 시간을 기록해 요청 간격을 검증한다."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class RobotsSession(FakeSession):
    """robots.txt 를 따로 응답하고 그 요청 횟수를 셀 수 있는 세션."""

    def __init__(self, robots, pages: dict[str, FakeResponse] | None = None) -> None:
        super().__init__(pages or {})
        self.robots = robots

    def get(self, url: str, timeout: float, headers: dict[str, str] | None = None):
        if url.endswith("/robots.txt"):
            self.calls.append(url)
            self.request_headers.append(headers)
            if isinstance(self.robots, Exception):
                raise self.robots
            return self.robots
        return super().get(url, timeout, headers)

    def robots_fetch_count(self) -> int:
        return sum(1 for call in self.calls if call.endswith("/robots.txt"))


def robots_crawler(robots, **kwargs) -> Crawler:
    """실제 ``_robots_allowed`` 를 타는 크롤러 (robots_allowed 를 주입하지 않는다)."""

    kwargs.setdefault("sleeper", lambda _seconds: None)
    return Crawler(hash_exists=lambda *_args: False, session=RobotsSession(robots), **kwargs)


class RobotsPolicyTests(unittest.TestCase):
    """실제 robots.txt 판정 경로 검증. 기존 테스트는 robots_allowed 를 주입해 이 경로를 우회한다."""

    URL = "https://example.edu/notice/view.do?mode=view&id=1"

    def _allows(self, body: str, url: str | None = None) -> bool:
        return robots_crawler(FakeResponse(200, body))._robots_allowed(url or self.URL)

    def test_wildcard_disallow_is_honoured(self) -> None:
        """표준 robotparser 는 경로 안 와일드카드를 못 다뤄 금지를 허용으로 오판했다."""

        self.assertFalse(self._allows("User-agent: *\nDisallow: /*?mode=view"))

    def test_dollar_anchor_is_honoured(self) -> None:
        body = "User-agent: *\nDisallow: /*.pdf$"

        self.assertFalse(self._allows(body, "https://example.edu/files/guide.pdf"))
        self.assertTrue(self._allows(body, "https://example.edu/files/guide.pdf.html"))

    def test_longest_match_wins_regardless_of_line_order(self) -> None:
        """Disallow 가 먼저 쓰여 있어도 더 구체적인 Allow 가 이긴다(RFC 9309)."""

        body = "User-agent: *\nDisallow: /\nAllow: /notice/"

        self.assertTrue(self._allows(body, "https://example.edu/notice/view.do?id=1"))
        self.assertFalse(self._allows(body, "https://example.edu/admin/"))

    def test_crawler_specific_group_beats_wildcard_group(self) -> None:
        body = "User-agent: ASKU-Crawler\nDisallow: /secret\n\nUser-agent: *\nDisallow: /"

        self.assertTrue(self._allows(body, "https://example.edu/notice/view.do?id=1"))
        self.assertFalse(self._allows(body, "https://example.edu/secret/x"))

    def test_missing_robots_allows_everything(self) -> None:
        self.assertTrue(robots_crawler(FakeResponse(404))._robots_allowed(self.URL))

    def test_forbidden_robots_blocks_the_whole_origin(self) -> None:
        self.assertFalse(robots_crawler(FakeResponse(403))._robots_allowed(self.URL))

    def test_server_error_blocks_instead_of_allowing_everything(self) -> None:
        """5xx 를 빈 규칙으로 읽으면 서버가 아플 때 오히려 더 긁는다."""

        self.assertFalse(robots_crawler(FakeResponse(503))._robots_allowed(self.URL))

    def test_denial_is_cached_so_robots_is_fetched_once(self) -> None:
        """거부를 캐시하지 않으면 수집을 거부한 서버에 URL 마다 다시 요청하게 된다."""

        crawler = robots_crawler(FakeResponse(403))

        for index in range(5):
            crawler._robots_allowed(f"https://example.edu/notice/view.do?id={index}")

        self.assertEqual(crawler.session.robots_fetch_count(), 1)

    def test_unreachable_robots_is_cached_and_blocks(self) -> None:
        crawler = robots_crawler(requests.ConnectionError("boom"))

        self.assertFalse(crawler._robots_allowed(self.URL))
        self.assertFalse(crawler._robots_allowed("https://example.edu/notice/view.do?id=2"))
        self.assertEqual(crawler.session.robots_fetch_count(), 1)

    def test_each_origin_is_evaluated_separately(self) -> None:
        crawler = robots_crawler(FakeResponse(200, "User-agent: *\nDisallow: /"))

        crawler._robots_allowed("https://example.edu/a")
        crawler._robots_allowed("https://other.edu/a")

        self.assertEqual(crawler.session.robots_fetch_count(), 2)


class CrawlDelayTests(unittest.TestCase):
    def _delay_for(self, body: str, configured: float) -> float:
        crawler = robots_crawler(
            FakeResponse(200, body), settings=CrawlSettings(request_delay_seconds=configured)
        )
        crawler._robots_allowed("https://example.edu/a")
        return crawler._crawl_delay("https://example.edu/a")

    def test_declared_crawl_delay_overrides_the_configured_interval(self) -> None:
        self.assertEqual(self._delay_for("User-agent: *\nCrawl-delay: 10\nDisallow:", 1.0), 10.0)

    def test_configured_interval_wins_when_it_is_longer(self) -> None:
        """robots.txt 가 더 짧게 선언해도 우리 쪽 하한을 내리지는 않는다."""

        self.assertEqual(self._delay_for("User-agent: *\nCrawl-delay: 0.1\nDisallow:", 2.0), 2.0)

    def test_falls_back_to_settings_without_a_declaration(self) -> None:
        self.assertEqual(self._delay_for("User-agent: *\nDisallow:", 1.5), 1.5)

    def test_unknown_origin_falls_back_to_settings(self) -> None:
        """robots 를 아직 읽지 않았거나 호출자가 robots_allowed 를 주입한 경우."""

        crawler = robots_crawler(FakeResponse(404), settings=CrawlSettings(request_delay_seconds=1.5))

        self.assertEqual(crawler._crawl_delay("https://unknown.edu/a"), 1.5)

    def test_crawl_applies_the_declared_delay_to_every_response(self) -> None:
        listing = "https://example.edu/notice/list.do"
        sleeper = RecordingSleeper()
        session = RobotsSession(
            FakeResponse(200, "User-agent: *\nCrawl-delay: 5\nDisallow:"),
            {
                listing: FakeResponse(200, LISTING_HTML),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>본문</main>"),
                "https://example.edu/notice/view.do?id=2": FakeResponse(404),  # 죽은 링크도 간격을 지킨다
                "https://example.edu/notice/view.do?id=3": FakeResponse(404),
            },
        )
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            session=session,
            settings=CrawlSettings(request_delay_seconds=1.0, max_retries=0),
            sleeper=sleeper,
        )

        crawler.crawl(
            CrawlRequest(
                crawl_id=uuid4(),
                school_id=1,
                base_url=listing,
                mode="initial",
                scope=CrawlScope(allowed_hosts=["example.edu"], path_prefixes=["/notice"], max_listing_pages=1),
            ),
            CommonNoticeAdapter(),
        )

        self.assertEqual(sleeper.waits[0], 1.0)  # robots.txt 직후는 설정값
        page_waits = sleeper.waits[1:]
        self.assertEqual(page_waits, [5.0] * 4)  # 목록 1 + 상세 3 (404 포함)


class ListingPolicyTests(unittest.TestCase):
    def test_pagination_url_blocked_by_robots_stops_the_crawl(self) -> None:
        """2페이지부터 robots 검사를 건너뛰면 막아 둔 페이지네이션을 그냥 긁는다."""

        listing = "https://example.edu/notice/list.do"
        next_listing = f"{listing}?article.offset=10"
        paginated_html = f"{LISTING_HTML}<a rel='next' href='{next_listing}'>다음</a>"
        session = RobotsSession(
            FakeResponse(200, "User-agent: *\nDisallow: /*article.offset="),
            {
                listing: FakeResponse(200, paginated_html),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
                "https://example.edu/notice/view.do?id=2": FakeResponse(200, "<main>2</main>"),
                "https://example.edu/notice/view.do?id=3": FakeResponse(200, "<main>3</main>"),
            },
        )
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            session=session,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            sleeper=lambda _seconds: None,
        )

        run = crawler.crawl(
            CrawlRequest(
                crawl_id=uuid4(),
                school_id=1,
                base_url=listing,
                mode="initial",
                scope=CrawlScope(allowed_hosts=["example.edu"], path_prefixes=["/notice"], max_listing_pages=5),
            ),
            CommonNoticeAdapter(),
        )

        self.assertNotIn(next_listing, session.calls)  # 금지된 2페이지는 요청하지 않는다
        self.assertEqual(len(run.pages), 3)  # 1페이지 수집분은 유지
        self.assertEqual([failure.error_code for failure in run.failures], ["ROBOTS_DISALLOWED"])

    def test_unreachable_robots_is_recorded_as_retryable(self) -> None:
        """네트워크 오류를 정책 거부로 기록하면 원인 진단이 어긋나고 재시도 대상에서도 빠진다."""

        listing = "https://example.edu/notice/list.do"
        session = RobotsSession(requests.ConnectionError("boom"), {listing: FakeResponse(200, LISTING_HTML)})
        crawler = Crawler(hash_exists=lambda *_args: False, session=session, sleeper=lambda _seconds: None)

        run = crawler.crawl(
            CrawlRequest(crawl_id=uuid4(), school_id=1, base_url=listing, mode="initial"),
            CommonNoticeAdapter(),
        )

        self.assertEqual(run.pages, [])
        self.assertEqual(run.failures[0].error_code, "ROBOTS_UNREACHABLE")
        self.assertTrue(run.failures[0].retryable)


class MultiBoardTests(unittest.TestCase):
    """공지가 여러 탭으로 쪼개진 학교(세종대) 수집."""

    FIRST = "https://example.edu/notice/list.do"
    SECOND = "https://example.edu/notice/list2.do"

    def board_html(self, ids: list[int]) -> str:
        """분류 칸이 없는 목록. 이런 학교에서만 탭 이름이 분류로 쓰인다."""

        rows = "".join(
            f"<tr><td>{i}</td><td><a href='/notice/view.do?id={i}'>공지 {i}</a></td>"
            f"<td>학사팀</td><td>2026-07-0{i}</td></tr>"
            for i in ids
        )
        return f"<table><tbody>{rows}</tbody></table>"

    def crawler(self, session: FakeSession) -> Crawler:
        return Crawler(
            hash_exists=lambda *_args: False,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

    def request(self, **overrides) -> CrawlRequest:
        return CrawlRequest(
            crawl_id=uuid4(),
            school_id=1,
            base_url=self.FIRST,
            mode="initial",
            scope=CrawlScope(allowed_hosts=["example.edu"], **overrides),
        )

    def test_every_board_is_crawled_and_label_fills_empty_category(self) -> None:
        session = FakeSession(
            {
                self.FIRST: FakeResponse(200, self.board_html([1, 2])),
                self.SECOND: FakeResponse(200, self.board_html([3])),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
                "https://example.edu/notice/view.do?id=2": FakeResponse(200, "<main>2</main>"),
                "https://example.edu/notice/view.do?id=3": FakeResponse(200, "<main>3</main>"),
            }
        )
        boards = (Board(self.FIRST, "일반공지"), Board(self.SECOND, "장학"))

        run = self.crawler(session).crawl_boards(self.request(), boards, CommonNoticeAdapter())

        self.assertEqual(len(run.pages), 3)
        self.assertIn(self.SECOND, session.calls)
        # 목록이 분류를 주지 않으므로 탭 이름이 채워진다
        self.assertEqual([page.category_hint for page in run.pages], ["일반공지", "일반공지", "장학"])

    def test_board_label_does_not_overwrite_listing_category(self) -> None:
        """홍익대처럼 행마다 분류가 붙는 학교의 값을 탭 이름으로 덮으면 안 된다."""

        listing = "<table><tbody><tr><td>1</td><td>입시</td><td><a href='/notice/view.do?id=1'>공지</a></td><td>입학팀</td><td>2026-07-01</td></tr></tbody></table>"
        session = FakeSession(
            {
                self.FIRST: FakeResponse(200, listing),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
            }
        )

        run = self.crawler(session).crawl_boards(self.request(), (Board(self.FIRST, "장학"),), CommonNoticeAdapter())

        self.assertEqual(run.pages[0].category_hint, "입시")

    def test_notice_listed_in_two_boards_is_collected_once(self) -> None:
        """탭 사이에서도 중복 URL 을 걸러야 같은 공지를 두 번 받지 않는다."""

        session = FakeSession(
            {
                self.FIRST: FakeResponse(200, self.board_html([1])),
                self.SECOND: FakeResponse(200, self.board_html([1])),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
            }
        )
        boards = (Board(self.FIRST, "일반공지"), Board(self.SECOND, "장학"))

        run = self.crawler(session).crawl_boards(self.request(), boards, CommonNoticeAdapter())

        self.assertEqual(len(run.pages), 1)
        self.assertEqual(session.calls.count("https://example.edu/notice/view.do?id=1"), 1)

    def test_budget_is_shared_across_boards(self) -> None:
        """게시판마다 예산을 새로 잡으면 탭이 늘어난 만큼 총 요청량이 늘어난다."""

        session = FakeSession(
            {
                self.FIRST: FakeResponse(200, self.board_html([1, 2])),
                self.SECOND: FakeResponse(200, self.board_html([3])),
                "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
                "https://example.edu/notice/view.do?id=2": FakeResponse(200, "<main>2</main>"),
                "https://example.edu/notice/view.do?id=3": FakeResponse(200, "<main>3</main>"),
            }
        )
        boards = (Board(self.FIRST, "일반공지"), Board(self.SECOND, "장학"))

        run = self.crawler(session).crawl_boards(self.request(max_requests=3), boards, CommonNoticeAdapter())

        self.assertEqual(len(session.calls), 3)
        self.assertNotIn(self.SECOND, session.calls)  # 예산이 바닥나면 다음 탭으로 넘어가지 않는다
        self.assertEqual([f.error_code for f in run.failures], ["REQUEST_BUDGET_EXCEEDED"])

    def test_boards_are_visited_round_robin_so_no_tab_starves(self) -> None:
        """게시판을 하나씩 끝까지 돌면 공지가 많은 앞쪽 탭이 예산을 다 쓴다."""

        def listing(board: int, page: int) -> str:
            rows = "".join(
                f"<tr><td>{i}</td><td><a href='/v.do?id={board}-{page}-{i}'>공지</a></td>"
                f"<td>부서</td><td>2026-07-01</td></tr>"
                for i in range(4)
            )
            return f"<table><tbody>{rows}</tbody></table><a rel='next' href='/l.do?b={board}&p={page + 1}'>다음</a>"

        class Pages(dict):
            """페이지를 미리 다 만들지 않고 요청이 올 때 만들어 준다."""

            def __missing__(self, url: str) -> FakeResponse:
                if "/l.do" in url:
                    board = int(url.split("b=")[1].split("&")[0])
                    page = int(url.split("p=")[1])
                    return FakeResponse(200, listing(board, page))
                return FakeResponse(200, "<main>본문</main>")

        session = FakeSession(Pages())
        boards = tuple(Board(f"https://example.edu/l.do?b={i}&p=1", f"탭{i}") for i in range(1, 4))
        crawler = Crawler(
            hash_exists=lambda *_args: False,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        # 1바퀴에 목록 3회 + 상세 12회 = 15회. 예산 20회면 2바퀴를 다 돌지 못한다.
        run = crawler.crawl_boards(self.request(max_requests=20), boards, CommonNoticeAdapter())

        collected = {label: 0 for label in ("탭1", "탭2", "탭3")}
        for page in run.pages:
            collected[page.category_hint] += 1
        # 순서대로 돌면 탭1 이 20회를 다 써 탭2·탭3 이 0건이 된다
        self.assertTrue(all(count >= 4 for count in collected.values()), collected)

    def test_boards_for_returns_registered_tabs_or_the_url_itself(self) -> None:
        sejong = boards_for("https://www.sejong.ac.kr/kor/intro/notice1.do")
        self.assertGreater(len(sejong), 1)
        self.assertIn("장학", [board.label for board in sejong])

        other = boards_for("https://example.edu/notice/list.do")
        self.assertEqual(other, (Board("https://example.edu/notice/list.do"),))


class CrawlBudgetTests(unittest.TestCase):
    """게시판 수와 무관하게 크롤 1회 전체 요청량을 묶는 예산."""

    LISTING = "https://example.edu/notice/list.do"

    def pages(self) -> dict[str, FakeResponse]:
        return {
            self.LISTING: FakeResponse(200, LISTING_HTML),
            "https://example.edu/notice/view.do?id=1": FakeResponse(200, "<main>1</main>"),
            "https://example.edu/notice/view.do?id=2": FakeResponse(200, "<main>2</main>"),
            "https://example.edu/notice/view.do?id=3": FakeResponse(200, "<main>3</main>"),
        }

    def crawl(self, scope: CrawlScope, *, mode: str = "initial", clock=None, hash_exists=None):
        session = FakeSession(self.pages())
        crawler = Crawler(
            hash_exists=hash_exists or (lambda *_args: False),
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
            **({"clock": clock} if clock else {}),
        )
        run = crawler.crawl(
            CrawlRequest(crawl_id=uuid4(), school_id=1, base_url=self.LISTING, mode=mode, scope=scope),
            CommonNoticeAdapter(),
        )
        return run, session

    def test_request_budget_stops_crawl_and_keeps_partial_result(self) -> None:
        """예산이 바닥나면 예외 대신 중단한다. 그때까지 모은 페이지는 살아야 한다."""

        scope = CrawlScope(allowed_hosts=["example.edu"], max_requests=3)
        run, session = self.crawl(scope)

        # 목록 1회 + 상세 2회로 예산 소진 → 세 번째 상세는 요청하지 않는다
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(run.pages), 2)
        self.assertEqual([f.error_code for f in run.failures], ["REQUEST_BUDGET_EXCEEDED"])
        self.assertEqual(run.failures[0].stage, "budget")
        self.assertTrue(run.failures[0].retryable)

    def test_time_budget_stops_crawl(self) -> None:
        """요청 수가 남아도 시간 상한을 넘기면 멈춘다."""

        ticks = iter([0.0, 0.0, 5.0, 99.0, 99.0])
        scope = CrawlScope(allowed_hosts=["example.edu"], max_duration_seconds=10.0)
        run, _session = self.crawl(scope, clock=lambda: next(ticks))

        self.assertEqual([f.error_code for f in run.failures], ["TIME_BUDGET_EXCEEDED"])
        self.assertLess(len(run.pages), 3)

    def test_recrawl_stops_when_a_listing_page_is_fully_unchanged(self) -> None:
        """목록은 최신순이라 한 페이지가 통째로 unchanged 면 뒤쪽도 볼 필요가 없다."""

        listing_html = LISTING_HTML + f"<a rel='next' href='{self.LISTING}?page=2'>다음</a>"
        session = FakeSession({**self.pages(), self.LISTING: FakeResponse(200, listing_html)})
        crawler = Crawler(
            hash_exists=lambda *_args: True,  # 전부 이미 저장된 내용
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        run = crawler.crawl(
            CrawlRequest(
                crawl_id=uuid4(),
                school_id=1,
                base_url=self.LISTING,
                mode="recrawl",
                scope=CrawlScope(allowed_hosts=["example.edu"], max_listing_pages=5),
            ),
            CommonNoticeAdapter(),
        )

        self.assertNotIn(f"{self.LISTING}?page=2", session.calls)
        self.assertEqual({page.crawl_status for page in run.pages}, {"unchanged"})
        self.assertEqual(run.failures, [])

    def test_initial_crawl_does_not_stop_on_unchanged_page(self) -> None:
        """초기 수집은 unchanged 가 나와도 뒤쪽 페이지를 계속 봐야 한다."""

        second = f"{self.LISTING}?page=2"
        listing_html = LISTING_HTML + f"<a rel='next' href='{second}'>다음</a>"
        session = FakeSession({**self.pages(), self.LISTING: FakeResponse(200, listing_html), second: FakeResponse(200, "")})
        crawler = Crawler(
            hash_exists=lambda *_args: True,
            settings=CrawlSettings(request_delay_seconds=0, max_retries=0),
            session=session,
            sleeper=lambda _seconds: None,
            robots_allowed=lambda _url: True,
        )

        crawler.crawl(
            CrawlRequest(
                crawl_id=uuid4(),
                school_id=1,
                base_url=self.LISTING,
                mode="initial",
                scope=CrawlScope(allowed_hosts=["example.edu"], max_listing_pages=5),
            ),
            CommonNoticeAdapter(),
        )

        self.assertIn(second, session.calls)


if __name__ == "__main__":
    unittest.main()


class DateParsingTests(unittest.TestCase):
    """게시판마다 날짜 표기가 갈린다. 못 읽으면 규격이 통째로 실패한다."""

    def parse(self, text: str):
        html = f"<table><tbody><tr><td><a href='/v.do'>공지</a></td><td>부서</td><td>{text}</td></tr></tbody></table>"
        item = next(iter(CommonNoticeAdapter().parse_listing(html, "https://x/")))
        return item.published_at_hint

    def test_four_digit_year_formats(self) -> None:
        for text in ("2026-08-05", "2026.08.05", "2026/08/05"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), datetime(2026, 8, 5, tzinfo=timezone.utc))

    def test_two_digit_year(self) -> None:
        """아주대는 같은 행의 모바일 요소에 `26.08.05` 를 넣는다."""

        self.assertEqual(self.parse("26.08.05"), datetime(2026, 8, 5, tzinfo=timezone.utc))

    def test_invalid_date_is_none(self) -> None:
        self.assertIsNone(self.parse("등록일 없음"))
        self.assertIsNone(self.parse("26.13.45"))

    def test_spaced_date_format(self) -> None:
        """서울대는 `2026. 7. 31.` 처럼 구분자 뒤를 띄어 쓴다."""

        self.assertEqual(self.parse("2026. 7. 31."), datetime(2026, 7, 31, tzinfo=timezone.utc))
        self.assertEqual(self.parse("2026. 07. 31."), datetime(2026, 7, 31, tzinfo=timezone.utc))
