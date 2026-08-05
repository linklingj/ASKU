"""수집 품질 검증.

학교별 Adapter·ContentParser 가 실제로 동작하는지 코드로 판정한다. 사람이 눈으로
페이지를 열어 확인하던 것을 대신하며, 다음 세 곳에서 같은 기준을 쓴다.

1. 개발용 스크립트 — 학교를 등록하기 전 수집 품질 확인
2. 크롤 파이프라인 — 매 실행마다 지표를 남겨 사이트 개편으로 파서가 조용히
   죽는 것을 감지
3. (예정) 자동 생성한 규격이 쓸 만한지 판정하는 게이트

수집 실패는 예외가 아니라 **0건 성공**으로 나타난다. 목록을 한 줄도 읽지 못해도
크롤은 정상 종료하므로, 지표를 남기지 않으면 아무도 알아채지 못한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Iterable

from app.crawler import (
    DEFAULT_BOARD_LABEL,
    Board,
    CrawlRun,
    ListingItem,
    NoticeAdapter,
    normalize_url,
)
from app.extractor import CleanedDocument, content_parser_for
from app.schemas import CrawledPage


# 목록에서 한 줄도 읽지 못하면 파서가 사이트 구조와 맞지 않는 것이다.
MIN_LISTING_ROWS = 1
# 제목 없는 행이 이보다 많으면 링크는 잡았지만 제목 선택자가 어긋난 것이다.
MIN_TITLE_RATIO = 0.9
# 날짜는 일부 게시판이 실제로 비워 두기도 해 기준을 낮게 잡는다.
MIN_DATE_RATIO = 0.7
# 본문이 이보다 짧으면 껍데기다. 한두 줄짜리 공지도 이 정도는 넘는다.
MIN_CONTENT_CHARS = 50
# 직전 크롤 대비 목록 행 수가 이 비율 아래로 떨어지면 사이트 개편을 의심한다.
# 공지가 실제로 줄어들 수도 있으므로 여유를 두고, 판정은 경고 성격으로 남긴다.
LISTING_DROP_RATIO = 0.5


@dataclass(frozen=True)
class Finding:
    """검증에서 발견한 문제 하나."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class ValidationReport:
    """한 게시판의 검증 결과. `findings` 가 비어 있으면 통과다."""

    target: str
    listing_rows: int = 0
    title_ratio: float = 0.0
    date_ratio: float = 0.0
    checked_details: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def add(self, code: str, detail: str) -> None:
        self.findings.append(Finding(code, detail))

    def summary(self) -> str:
        state = "통과" if self.passed else f"실패 {len(self.findings)}건"
        return (
            f"{self.target} — {state} | 목록 {self.listing_rows}행 "
            f"제목 {self.title_ratio:.0%} 날짜 {self.date_ratio:.0%} "
            f"상세 {self.checked_details}건"
        )


def validate_listing(
    adapter: NoticeAdapter,
    html: str,
    listing_url: str,
    *,
    report: ValidationReport | None = None,
) -> ValidationReport:
    """목록 페이지 파싱 결과를 판정한다."""

    report = report or ValidationReport(target=listing_url)
    items = list(adapter.parse_listing(html, listing_url))
    report.listing_rows = len(items)

    if len(items) < MIN_LISTING_ROWS:
        report.add("NO_LISTING_ROWS", "목록에서 공지를 한 줄도 읽지 못했다")
        return report

    report.title_ratio = _ratio(items, lambda item: bool(item.title_hint and item.title_hint.strip()))
    report.date_ratio = _ratio(items, lambda item: item.published_at_hint is not None)

    if report.title_ratio < MIN_TITLE_RATIO:
        report.add("MISSING_TITLES", f"제목이 채워진 행이 {report.title_ratio:.0%} 뿐이다")
    if report.date_ratio < MIN_DATE_RATIO:
        report.add("MISSING_DATES", f"등록일이 채워진 행이 {report.date_ratio:.0%} 뿐이다")

    _check_pagination(adapter, html, listing_url, report)
    return report


def validate_detail(
    document: CleanedDocument,
    item: ListingItem,
    *,
    other_titles: list[str] | None = None,
    report: ValidationReport | None = None,
) -> ValidationReport:
    """상세 페이지에서 정제한 본문을 판정한다.

    `other_titles` 는 같은 목록의 **다른** 공지 제목이다. 본문에 이들이 섞여 있으면
    본문 선택자가 이전·다음 글 목록까지 함께 잡은 것이다.
    """

    report = report or ValidationReport(target=item.url)
    report.checked_details += 1
    content = document.content.strip()

    if len(content) < MIN_CONTENT_CHARS:
        report.add("EMPTY_CONTENT", f"본문이 {len(content)}자뿐이다(기준 {MIN_CONTENT_CHARS}자)")

    # 목록의 제목이 상세 본문·제목 어디에도 없으면 링크를 잘못 잡았을 가능성이 크다.
    if item.title_hint:
        haystack = _squash(f"{document.title or ''} {content}")
        if _squash(item.title_hint) not in haystack:
            report.add("TITLE_MISMATCH", f"목록 제목이 상세 페이지에 없다: {item.title_hint[:40]}")

    leaked = [title for title in (other_titles or []) if _squash(title) in _squash(content)]
    if leaked:
        report.add("NEIGHBOUR_LEAK", f"다른 공지 제목이 본문에 섞였다: {leaked[0][:40]}")

    if document.used_body_fallback:
        report.add("BODY_FALLBACK", "본문 영역을 못 찾아 페이지 전체를 본문으로 썼다")
    return report


def validate_crawl(
    run: CrawlRun,
    adapter: NoticeAdapter,
    boards: Iterable[Board],
    *,
    detail_sample: int = 3,
    previous_rows: Callable[[str], int | None] | None = None,
) -> list[ValidationReport]:
    """끝난 크롤 결과를 게시판별로 판정한다. 추가 요청은 하지 않는다.

    상세는 게시판마다 표본 몇 건만 본다. 전수 검사해도 얻는 신호가 같고, 크롤
    직후 파이프라인에서 도는 작업이라 가볍게 유지한다.
    """

    reports: list[ValidationReport] = []
    pages_by_board = _group_pages(run, boards)

    for board in boards:
        label = board.label or DEFAULT_BOARD_LABEL
        report = ValidationReport(target=label)
        listing_html = run.first_listing_html.get(label)
        if listing_html is None:
            report.add("LISTING_FETCH_FAILED", f"목록을 받지 못했다: {board.url}")
            reports.append(report)
            continue

        validate_listing(adapter, listing_html, board.url, report=report)
        if previous_rows is not None:
            check_listing_drop(report, previous_rows(label))

        titles = [item.title_hint or "" for item in adapter.parse_listing(listing_html, board.url)]
        for page in pages_by_board.get(label, [])[:detail_sample]:
            document = content_parser_for(page.canonical_url).parse(page.raw_html)
            others = [title for title in titles if title and title != page.title_hint]
            validate_detail(document, _as_listing_item(page), other_titles=others, report=report)
        reports.append(report)
    return reports


def _group_pages(run: CrawlRun, boards: Iterable[Board]) -> dict[str, list[CrawledPage]]:
    """수집된 페이지를 게시판 라벨로 묶는다.

    라벨이 없는 학교(단일 게시판)는 `category_hint` 가 목록의 분류값이므로 라벨로
    쓸 수 없다. 그때는 모든 페이지를 기본 게시판에 넣는다.
    """

    labels = {board.label for board in boards if board.label}
    grouped: dict[str, list[CrawledPage]] = {}
    for page in run.pages:
        label = page.category_hint if page.category_hint in labels else DEFAULT_BOARD_LABEL
        grouped.setdefault(label, []).append(page)
    return grouped


def _as_listing_item(page: CrawledPage) -> ListingItem:
    return ListingItem(
        url=page.canonical_url,
        title_hint=page.title_hint,
        category_hint=page.category_hint,
        author_hint=page.author_hint,
        published_at_hint=page.published_at_hint,
    )


def check_listing_drop(report: ValidationReport, previous_rows: int | None) -> ValidationReport:
    """직전 크롤과 비교해 목록 행 수가 급감했는지 본다.

    선택자가 부분적으로만 어긋나면 행 수가 0 이 아니라 일부만 줄어든다. 세종대가
    16행 중 6행만 읽던 경우처럼, 한 번의 결과만 봐서는 정상과 구분되지 않는다.
    """

    if not previous_rows or report.listing_rows >= previous_rows * LISTING_DROP_RATIO:
        return report
    report.add(
        "LISTING_ROWS_DROPPED",
        f"목록 행이 직전 {previous_rows}행에서 {report.listing_rows}행으로 줄었다",
    )
    return report


def _check_pagination(adapter: NoticeAdapter, html: str, listing_url: str, report: ValidationReport) -> None:
    """다음 페이지 링크가 현재 페이지와 다른 URL 인지 본다.

    같은 URL 을 돌려주면 크롤러의 방문 기록이 막아 주지만, 페이지네이션 선택자가
    깨졌다는 신호이므로 남긴다.
    """

    next_url = adapter.next_listing_url(html, listing_url)
    if next_url is not None and normalize_url(next_url) == normalize_url(listing_url):
        report.add("PAGINATION_LOOP", "다음 페이지 링크가 현재 페이지와 같다")


def _ratio(items: list[ListingItem], predicate) -> float:
    return sum(1 for item in items if predicate(item)) / len(items) if items else 0.0


def _squash(value: str) -> str:
    """공백·특수문자 차이를 무시하고 비교하기 위한 정규화.

    목록의 제목과 상세 페이지의 제목은 줄바꿈·구분자·말줄임이 달라 그대로 비교하면
    같은 글도 다르다고 판정된다.
    """

    return re.sub(r"[\s\W_]+", "", value).lower()
