"""검증기 테스트.

각 항목은 실제로 겪은 사고를 재현한다. 이 검증기가 있었다면 사람이 페이지를
열어보지 않고도 잡혔을 것들이다.
"""

from datetime import datetime, timezone
from uuid import uuid4
import unittest

from app.crawler import (
    DEFAULT_BOARD_LABEL,
    Board,
    CommonNoticeAdapter,
    CrawlRun,
    ListingItem,
    SejongNoticeAdapter,
    YonseiNoticeAdapter,
)
from app.extractor import CleanedDocument
from app.schemas import CrawledPage
from app.validation import validate_crawl, validate_detail, validate_listing


LISTING_URL = "https://example.edu/notice/list.do"


def row(index: int, *, title: str = "공지", date: str = "2026-07-01") -> str:
    return (
        f"<tr><td>{index}</td>"
        f"<td><a href='/notice/view.do?id={index}'>{title}</a></td>"
        f"<td>학사팀</td><td>{date}</td></tr>"
    )


def listing_html(rows: int = 3, **kwargs) -> str:
    return f"<table><tbody>{''.join(row(i, **kwargs) for i in range(1, rows + 1))}</tbody></table>"


def item(title: str = "장학금 신청 안내") -> ListingItem:
    return ListingItem(
        url="https://example.edu/notice/view.do?id=1",
        title_hint=title,
        published_at_hint=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


class ListingValidationTests(unittest.TestCase):
    def test_healthy_listing_passes(self) -> None:
        report = validate_listing(CommonNoticeAdapter(), listing_html(), LISTING_URL)

        self.assertTrue(report.passed, report.findings)
        self.assertEqual(report.listing_rows, 3)

    def test_zero_rows_is_reported(self) -> None:
        """연세대·성균관대가 서비스에서 0건 수집 중이던 상황."""

        yonsei_markup = "<div class='boardWrap'><ul><li><a href='/artclView.do'>공지</a></li></ul></div>"

        report = validate_listing(CommonNoticeAdapter(), yonsei_markup, LISTING_URL)

        self.assertFalse(report.passed)
        self.assertEqual([f.code for f in report.findings], ["NO_LISTING_ROWS"])

    def test_partial_rows_are_not_reported_as_zero(self) -> None:
        """세종대가 16행 중 6행만 읽던 상황은 행 수만으로는 잡히지 않는다.

        전체 대비 몇 행인지는 알 수 없으므로, 이 경우는 제목·날짜 비율과
        상세 검증으로 걸러야 한다. 여기서는 오탐이 없다는 것만 확인한다.
        """

        report = validate_listing(SejongNoticeAdapter(), listing_html(6), LISTING_URL)

        self.assertEqual(report.listing_rows, 0)  # 세종대 선택자는 이 마크업과 안 맞는다
        self.assertEqual([f.code for f in report.findings], ["NO_LISTING_ROWS"])

    def test_missing_titles_are_reported(self) -> None:
        report = validate_listing(CommonNoticeAdapter(), listing_html(3, title=""), LISTING_URL)

        self.assertIn("MISSING_TITLES", [f.code for f in report.findings])

    def test_missing_dates_are_reported(self) -> None:
        """세종대를 공용 파서로 읽으면 작성자 칸에 날짜가 들어가고 날짜는 빈다."""

        report = validate_listing(CommonNoticeAdapter(), listing_html(3, date="-"), LISTING_URL)

        self.assertIn("MISSING_DATES", [f.code for f in report.findings])

    def test_pagination_pointing_to_itself_is_reported(self) -> None:
        html = listing_html() + f"<a rel='next' href='{LISTING_URL}'>다음</a>"

        report = validate_listing(CommonNoticeAdapter(), html, LISTING_URL)

        self.assertIn("PAGINATION_LOOP", [f.code for f in report.findings])

    def test_yonsei_adapter_passes_on_its_own_markup(self) -> None:
        html = """
        <div class='boardWrap'><ul>
          <li><a href='/bbs/sc/58/1/artclView.do'>
            <span class='title'>학위수여식 안내</span>
            <span class='notice-title'>일반공지</span>
            <span class='etc-area'>교무처</span>
            <span class='date-area'>2026.08.05</span>
          </a></li>
        </ul></div>
        """

        report = validate_listing(YonseiNoticeAdapter(), html, "https://www.yonsei.ac.kr/sc/254/subview.do")

        self.assertTrue(report.passed, report.findings)


class DetailValidationTests(unittest.TestCase):
    def document(self, content: str, *, title: str | None = "장학금 신청 안내", fallback: bool = False):
        return CleanedDocument(title=title, content=content, used_body_fallback=fallback)

    def test_healthy_detail_passes(self) -> None:
        body = "장학금 신청 안내 " + "신청 기간과 제출 서류를 아래와 같이 안내합니다. " * 3

        report = validate_detail(self.document(body), item())

        self.assertTrue(report.passed, report.findings)

    def test_empty_body_is_reported(self) -> None:
        """본문 대신 안내 문구나 목록만 잡힌 껍데기 문서를 걸러낸다."""

        report = validate_detail(self.document("공지사항"), item())

        self.assertIn("EMPTY_CONTENT", [f.code for f in report.findings])

    def test_neighbour_titles_in_body_are_reported(self) -> None:
        """홍익대는 본문 아래에 다음 글 목록이 붙어 공용 파서가 함께 읽는다."""

        body = "정전으로 서비스가 중단됩니다. " * 3 + "후기 학위수여식 졸업가운 대여 안내"

        report = validate_detail(
            self.document(body),
            item(),
            other_titles=["후기 학위수여식 졸업가운 대여 안내"],
        )

        self.assertIn("NEIGHBOUR_LEAK", [f.code for f in report.findings])

    def test_listing_title_absent_from_detail_is_reported(self) -> None:
        """목록에서 상세 링크를 잘못 잡으면 다른 글이 열린다."""

        body = "전혀 다른 공지의 본문입니다. " * 4

        report = validate_detail(self.document(body, title="다른 공지"), item())

        self.assertIn("TITLE_MISMATCH", [f.code for f in report.findings])

    def test_title_match_ignores_whitespace_and_punctuation(self) -> None:
        """목록 제목과 상세 제목은 구분자·공백이 달라 그대로 비교하면 오탐이 난다."""

        report = validate_detail(
            self.document("본문 내용을 충분히 채운 공지 문단입니다. " * 3, title="[장학] 장학금  신청 안내!"),
            item("장학금 신청 안내"),
        )

        self.assertNotIn("TITLE_MISMATCH", [f.code for f in report.findings])

    def test_body_fallback_is_reported(self) -> None:
        report = validate_detail(
            self.document("장학금 신청 안내 " + "본문 " * 30, fallback=True),
            item(),
        )

        self.assertIn("BODY_FALLBACK", [f.code for f in report.findings])


class CrawlValidationTests(unittest.TestCase):
    """끝난 크롤 결과를 게시판별로 판정한다(추가 요청 없이)."""

    def run_fixture(self, *, rows: int = 3, label: str | None = None) -> CrawlRun:
        run = CrawlRun()
        run.first_listing_html[label or DEFAULT_BOARD_LABEL] = listing_html(rows)
        for index in range(1, rows + 1):
            run.board_of[f"https://example.edu/notice/view.do?id={index}"] = label or DEFAULT_BOARD_LABEL
            run.pages.append(
                CrawledPage(
                    crawl_id=uuid4(),
                    school_id=1,
                    source_url=f"https://example.edu/notice/view.do?id={index}",
                    canonical_url=f"https://example.edu/notice/view.do?id={index}",
                    title_hint="공지",
                    category_hint=label,
                    raw_html=(
                        "<main><p>공지 본문입니다. 신청 기간과 제출 서류를 아래와 같이 "
                        "안내하오니 기한 내에 신청하시기 바랍니다.</p></main>"
                    ),
                    content_hash="hash",
                    fetched_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                    crawl_status="new",
                )
            )
        return run

    def test_healthy_crawl_reports_pass(self) -> None:
        boards = (Board(LISTING_URL, "일반공지"),)

        reports = validate_crawl(self.run_fixture(label="일반공지"), CommonNoticeAdapter(), boards)

        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].passed, reports[0].findings)
        self.assertEqual(reports[0].target, "일반공지")

    def test_board_without_listing_html_is_reported(self) -> None:
        """목록을 못 받은 게시판은 조용히 빠지지 않고 실패로 남는다."""

        boards = (Board(LISTING_URL, "일반공지"), Board("https://example.edu/l2.do", "장학"))

        reports = validate_crawl(self.run_fixture(label="일반공지"), CommonNoticeAdapter(), boards)

        self.assertEqual([r.passed for r in reports], [True, False])
        self.assertIn("LISTING_FETCH_FAILED", [f.code for f in reports[1].findings])

    def test_listing_row_drop_is_reported(self) -> None:
        """선택자가 부분적으로 어긋나면 행 수가 0 이 아니라 일부만 줄어든다."""

        boards = (Board(LISTING_URL, "일반공지"),)

        reports = validate_crawl(
            self.run_fixture(rows=3, label="일반공지"),
            CommonNoticeAdapter(),
            boards,
            previous_rows=lambda _board: 16,
        )

        self.assertIn("LISTING_ROWS_DROPPED", [f.code for f in reports[0].findings])

    def test_details_are_checked_when_listing_supplies_its_own_category(self) -> None:
        """목록이 자체 분류를 주면 `category_hint` 가 게시판 라벨과 달라진다.

        분류로 페이지를 게시판에 되짚으면(아주대 `기타`·`학사`) 어느 게시판에도
        붙지 않아 상세 검증이 통째로 건너뛰어지고, 본문이 빈 공지를 놓친다.
        """

        run = self.run_fixture(rows=2, label="일반공지")
        for page in run.pages:
            page.category_hint = "학사"  # 목록이 준 분류. 게시판 라벨과 다르다
            page.raw_html = "<main><p>짧음</p></main>"  # 본문 미달 → EMPTY_CONTENT 대상
        boards = (Board(LISTING_URL, "일반공지"),)

        reports = validate_crawl(run, CommonNoticeAdapter(), boards)

        self.assertGreater(reports[0].checked_details, 0, "상세 검증이 건너뛰어졌다")
        self.assertIn("EMPTY_CONTENT", [f.code for f in reports[0].findings])

    def test_first_crawl_has_no_previous_rows_to_compare(self) -> None:
        boards = (Board(LISTING_URL, "일반공지"),)

        reports = validate_crawl(
            self.run_fixture(label="일반공지"),
            CommonNoticeAdapter(),
            boards,
            previous_rows=lambda _board: None,
        )

        self.assertTrue(reports[0].passed, reports[0].findings)


if __name__ == "__main__":
    unittest.main()
