"""규격 기반 파서가 전용 파이썬 어댑터와 같은 결과를 내는지 확인한다.

전용 어댑터는 실제 사이트로 검증된 기준이다. 규격 방식이 그것과 같은 결과를
내야 코드를 데이터로 옮겨도 된다는 근거가 되고, 나중에 자동 생성한 규격을
채점할 정답지도 된다.
"""

from datetime import datetime, timezone
import unittest

from app.adapter_spec import (
    AdapterSpec,
    BoardSpec,
    DetailSpec,
    FormPagination,
    LinkPagination,
    ListingSpec,
    OffsetPagination,
)
from app.crawler import (
    CommonNoticeAdapter,
    HongikNoticeAdapter,
    SejongNoticeAdapter,
    SkkuNoticeAdapter,
    SpecNoticeAdapter,
    YonseiNoticeAdapter,
    adapter_for,
    boards_for,
)
from app.extractor import (
    CommonContentParser,
    K2WebContentParser,
    SpecContentParser,
    content_parser_for,
)


K2WEB_ROW = """
<table><tbody>
  <tr class='b-top-box'>
    <td class='b-td-title'><div class='b-title-box'>
      <div class='b-cate-box'><span class='b-mini-cate'>일반</span></div>
      <a href='?mode=view&amp;articleNo=1'><span class='b-title'>장학금 안내</span></a>
    </div><div class='b-m-con'><span class='b-writer'>학생지원과</span>
      <span class='b-date'>2026.07.24</span></div></td>
  </tr>
</tbody></table>
<div class='b-paging'><ul>
  <li class='next pager'><a href='?mode=list&amp;article.offset=10'
     title='다음 페이지로 이동하기'>다음</a></li>
</ul></div>
"""

SEJONG_URL = "https://www.sejong.ac.kr/kor/intro/notice1.do"
HONGIK_URL = "https://www.hongik.ac.kr/kr/newscenter/notice.do"


def k2web_listing(**overrides) -> ListingSpec:
    fields = {
        "row": "table tbody tr",
        "detail_link": ".b-title-box a[href*='mode=view'][href*='articleNo']",
        "title": ".b-title",
        "author": ".b-writer",
        "date": ".b-date",
        "pagination": LinkPagination(selector="li.next a[href]"),
    }
    fields.update(overrides)
    return ListingSpec(**fields)


def spec(host: str, listing: ListingSpec, detail: DetailSpec | None = None) -> AdapterSpec:
    return AdapterSpec(host=host, listing=listing, detail=detail or DetailSpec())


class SpecMatchesDedicatedAdapterTests(unittest.TestCase):
    def assert_same_listing(self, dedicated, spec_adapter, html: str, url: str) -> None:
        expected = list(dedicated.parse_listing(html, url))
        actual = list(spec_adapter.parse_listing(html, url))

        self.assertEqual(len(actual), len(expected))
        for want, got in zip(expected, actual):
            self.assertEqual(got.url, want.url)
            self.assertEqual(got.title_hint, want.title_hint)
            self.assertEqual(got.author_hint, want.author_hint)
            self.assertEqual(got.published_at_hint, want.published_at_hint)

    def test_sejong_spec_matches_adapter(self) -> None:
        adapter = SpecNoticeAdapter(spec("www.sejong.ac.kr", k2web_listing()))

        self.assert_same_listing(SejongNoticeAdapter(), adapter, K2WEB_ROW, SEJONG_URL)
        self.assertEqual(
            adapter.next_listing_url(K2WEB_ROW, SEJONG_URL),
            SejongNoticeAdapter().next_listing_url(K2WEB_ROW, SEJONG_URL),
        )

    def test_hongik_spec_matches_adapter(self) -> None:
        """홍익대는 행마다 분류가 붙고 작성자 칸이 없다."""

        listing = k2web_listing(category=".b-mini-cate", author=None)
        adapter = SpecNoticeAdapter(spec("www.hongik.ac.kr", listing))

        expected = list(HongikNoticeAdapter().parse_listing(K2WEB_ROW, HONGIK_URL))
        actual = list(adapter.parse_listing(K2WEB_ROW, HONGIK_URL))

        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].category_hint, expected[0].category_hint)
        self.assertEqual(actual[0].title_hint, expected[0].title_hint)
        self.assertIsNone(actual[0].author_hint)

    def test_skku_spec_reads_dl_listing(self) -> None:
        """성균관대는 표가 아니라 `dl` 구조다. 행 선택자만 바꾸면 된다."""

        html = """
        <dl class='board-list-content-wrap'>
          <dt class='board-list-content-title'><a href='?articleNo=7'>채용 공고</a></dt>
          <dd class='board-list-content-info'><ul>
            <li class='cate'>공지</li><li class='writer'>김민혜</li><li class='date'>2026-08-05</li>
          </ul></dd>
        </dl>
        """
        listing = ListingSpec(
            row="dl.board-list-content-wrap",
            detail_link="dt.board-list-content-title a[href*='articleNo']",
            author="dd.board-list-content-info li.writer",
            date="dd.board-list-content-info li.date",
            category="dd.board-list-content-info li.cate",
        )
        url = "https://www.skku.edu/skku/campus/skk_comm/notice02.do"

        expected = list(SkkuNoticeAdapter().parse_listing(html, url))
        actual = list(SpecNoticeAdapter(spec("www.skku.edu", listing)).parse_listing(html, url))

        self.assertEqual(len(actual), len(expected), expected)
        self.assertEqual(actual[0].title_hint, "채용 공고")
        self.assertEqual(actual[0].author_hint, "김민혜")
        self.assertEqual(actual[0].published_at_hint, datetime(2026, 8, 5, tzinfo=timezone.utc))

    def test_yonsei_form_pagination_matches_adapter(self) -> None:
        """연세대는 폼의 hidden input 을 조합해야 다음 목록 URL 이 나온다."""

        html = """
        <div class='_paging'><a class='_listNext' href="javascript:page_link('2')">다음</a></div>
        <form name='pageForm' action='/sc/254/subview.do'>
          <input name='enc' value='abc'><input name='page' value='1'>
        </form>
        """
        url = "https://www.yonsei.ac.kr/sc/254/subview.do"
        listing = ListingSpec(
            row=".boardWrap > ul > li",
            detail_link="a[href*='artclView.do']",
            pagination=FormPagination(
                form_selector="form[name='pageForm'][action]",
                next_selector="._paging ._listNext[href]",
                page_pattern=r"page_link\('(\d+)'\)",
            ),
        )

        actual = SpecNoticeAdapter(spec("www.yonsei.ac.kr", listing)).next_listing_url(html, url)

        self.assertEqual(actual, YonseiNoticeAdapter().next_listing_url(html, url))
        self.assertIn("page=2", actual)


    def test_category_ignore_drops_article_numbers(self) -> None:
        """성균관대는 분류 자리에 고정공지면 '공지', 일반 글이면 'No.2149' 를 넣는다.

        거르지 않으면 글 번호가 분류로 저장돼 검색 결과에 섞인다.
        """

        html = """
        <dl class='board-list-content-wrap'>
          <dt class='board-list-content-title'><a href='?articleNo=7'>공고</a></dt>
          <dd class='board-list-content-info'><ul><li>No.2149</li><li>교무팀</li></ul></dd>
        </dl>
        """
        listing = ListingSpec(
            row="dl.board-list-content-wrap",
            detail_link="dt.board-list-content-title a[href*='articleNo']",
            category="dd.board-list-content-info li:nth-of-type(1)",
            category_ignore=r"No\.\s*\d+",
        )

        item = next(iter(SpecNoticeAdapter(spec("www.skku.edu", listing)).parse_listing(html, "https://x/")))

        self.assertIsNone(item.category_hint)

    def test_category_is_kept_when_it_does_not_match_the_ignore_rule(self) -> None:
        html = """
        <dl class='board-list-content-wrap'>
          <dt class='board-list-content-title'><a href='?articleNo=7'>공고</a></dt>
          <dd class='board-list-content-info'><ul><li>공지</li></ul></dd>
        </dl>
        """
        listing = ListingSpec(
            row="dl.board-list-content-wrap",
            detail_link="dt.board-list-content-title a[href*='articleNo']",
            category="dd.board-list-content-info li:nth-of-type(1)",
            category_ignore=r"No\.\s*\d+",
        )

        item = next(iter(SpecNoticeAdapter(spec("www.skku.edu", listing)).parse_listing(html, "https://x/")))

        self.assertEqual(item.category_hint, "공지")


class PaginationTests(unittest.TestCase):
    def test_offset_pagination_advances_the_parameter(self) -> None:
        listing = ListingSpec(row="tr", detail_link="a", pagination=OffsetPagination(param="article.offset", step=10))
        adapter = SpecNoticeAdapter(spec("example.edu", listing))

        first = adapter.next_listing_url("", "https://example.edu/list.do")
        second = adapter.next_listing_url("", first)

        self.assertIn("article.offset=10", first)
        self.assertIn("article.offset=20", second)

    def test_missing_next_link_ends_the_board(self) -> None:
        listing = ListingSpec(row="tr", detail_link="a", pagination=LinkPagination(selector="li.next a[href]"))
        adapter = SpecNoticeAdapter(spec("example.edu", listing))

        self.assertIsNone(adapter.next_listing_url("<div>마지막</div>", "https://example.edu/list.do"))


class ResolutionOrderTests(unittest.TestCase):
    """전용 클래스 → 규격 → 공용 파서 순으로 고른다."""

    def listing(self) -> ListingSpec:
        return ListingSpec(row="li.custom", detail_link="a[href]")

    def test_dedicated_class_wins_over_spec(self) -> None:
        """손으로 검증한 전용 클래스가 앞선다. 자동 생성 규격이 이상해도 기존 학교는 안전하다."""

        adapter = adapter_for("https://www.sejong.ac.kr/kor/intro/notice1.do", spec("www.sejong.ac.kr", self.listing()))

        self.assertIsInstance(adapter, SejongNoticeAdapter)

    def test_spec_is_used_for_unregistered_host(self) -> None:
        adapter = adapter_for("https://new.ac.kr/notice.do", spec("new.ac.kr", self.listing()))

        self.assertIsInstance(adapter, SpecNoticeAdapter)

    def test_common_adapter_when_no_spec(self) -> None:
        adapter = adapter_for("https://new.ac.kr/notice.do")

        self.assertIsInstance(adapter, CommonNoticeAdapter)
        self.assertNotIsInstance(adapter, SpecNoticeAdapter)

    def test_boards_come_from_spec_for_unregistered_host(self) -> None:
        spec_with_boards = AdapterSpec(
            host="new.ac.kr",
            boards=[BoardSpec(url="https://new.ac.kr/n1.do", label="일반"), BoardSpec(url="https://new.ac.kr/n2.do", label="장학")],
            listing=self.listing(),
        )

        boards = boards_for("https://new.ac.kr/n1.do", spec_with_boards)

        self.assertEqual([board.label for board in boards], ["일반", "장학"])

    def test_registered_boards_win_over_spec(self) -> None:
        spec_with_boards = AdapterSpec(
            host="www.sejong.ac.kr",
            boards=[BoardSpec(url="https://www.sejong.ac.kr/x.do", label="엉뚱한 탭")],
            listing=self.listing(),
        )

        boards = boards_for("https://www.sejong.ac.kr/kor/intro/notice1.do", spec_with_boards)

        self.assertNotIn("엉뚱한 탭", [board.label for board in boards])
        self.assertIn("장학", [board.label for board in boards])

    def test_content_parser_resolution_follows_the_same_order(self) -> None:
        detail = DetailSpec(body=[".custom-body"])
        generated = AdapterSpec(host="new.ac.kr", listing=self.listing(), detail=detail)

        self.assertIsInstance(content_parser_for("https://www.sejong.ac.kr/n.do", generated), K2WebContentParser)
        self.assertIsInstance(content_parser_for("https://new.ac.kr/n.do", generated), SpecContentParser)
        self.assertIsInstance(content_parser_for("https://new.ac.kr/n.do"), CommonContentParser)


class SpecContentParserTests(unittest.TestCase):
    HTML = """
    <main>
      <div class='b-content-box'><div class='fr-view'><p>본문 내용입니다.</p></div></div>
      <ul class='b-list'><li><a href='/n.do?articleNo=2'>다음 글 제목</a></li></ul>
    </main>
    """

    def test_matches_dedicated_parser(self) -> None:
        parser = SpecContentParser(DetailSpec(body=[".b-content-box .fr-view", ".b-content-box"], title=[".b-title"]))

        self.assertEqual(parser.parse(self.HTML).content, K2WebContentParser().parse(self.HTML).content)

    def test_unmatched_selector_raises_so_extractor_can_fall_back(self) -> None:
        parser = SpecContentParser(DetailSpec(body=[".not-here"]))

        with self.assertRaises(ValueError):
            parser.parse(self.HTML)


class DetailLinkTemplateTests(unittest.TestCase):
    """상세 링크를 자바스크립트 호출로만 주는 게시판."""

    KOREA_ROW = """
    <table><tbody><tr>
      <td class='td-title alignL'>
        <a href='#1' onclick="jf_view('000060000000061085','1','ko');">교육성과 지원금 안내</a>
      </td>
      <td class='td-date'>2026-08-06</td>
    </tr></tbody></table>
    """
    KOREA_URL = "https://www.korea.ac.kr/ko/566/subview.do"

    def korea_listing(self) -> ListingSpec:
        return ListingSpec(
            row="table tbody tr",
            detail_link="td.td-title a",
            detail_link_attr="onclick",
            detail_link_pattern=r"jf_view\('([^']+)','([^']+)','([^']+)'",
            detail_link_template="/portalBoard/{2}/{1}/{0}/portalBoardView.do",
            title="td.td-title a",
            date="td.td-date",
        )

    def test_builds_detail_url_from_onclick(self) -> None:
        """`href` 는 `#1` 이라 따라갈 수 없다. 글 번호는 `onclick` 에만 있다."""

        adapter = SpecNoticeAdapter(spec("www.korea.ac.kr", self.korea_listing()))

        items = list(adapter.parse_listing(self.KOREA_ROW, self.KOREA_URL))

        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0].url,
            "https://www.korea.ac.kr/portalBoard/ko/1/000060000000061085/portalBoardView.do",
        )

    def test_row_without_the_pattern_is_skipped(self) -> None:
        """조립할 번호가 없으면 `#1` 을 상세로 착각하는 대신 그 행을 버린다."""

        html = "<table><tbody><tr><td class='td-title alignL'><a href='#1'>제목</a></td></tr></tbody></table>"
        adapter = SpecNoticeAdapter(spec("www.korea.ac.kr", self.korea_listing()))

        self.assertEqual(list(adapter.parse_listing(html, self.KOREA_URL)), [])

    def test_plain_href_still_wins_when_no_template(self) -> None:
        listing = ListingSpec(row="tr", detail_link="a")
        adapter = SpecNoticeAdapter(spec("www.korea.ac.kr", listing))

        items = list(adapter.parse_listing("<tr><a href='/view.do?id=3'>제목</a></tr>", self.KOREA_URL))

        self.assertEqual(items[0].url, "https://www.korea.ac.kr/view.do?id=3")


if __name__ == "__main__":
    unittest.main()
