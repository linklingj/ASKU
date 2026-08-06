"""대학교 공지 게시판용 HTML Crawler.

목록/상세 HTML 수집과 변경 감지만 담당한다. 본문 정제나 엔티티 추출은
Extractor의 책임이다. Storage가 병합되기 전에도 테스트할 수 있도록 Storage
조회는 콜백으로 주입한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re
from time import monotonic, sleep
from typing import Callable, Iterable, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from protego import Protego

from app.adapter_spec import AdapterSpec
from app.schemas import Attachment, CrawledPage, CrawlFailure, CrawlRequest


TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DETAIL_CONTEXT_QUERY_KEYS = {"article.offset", "articlelimit"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
USER_AGENT = "ASKU-Crawler/0.1 (+https://github.com/linklingj/ASKU)"
# robots.txt 의 User-agent 줄과 대조할 제품 토큰. 헤더 UA 의 버전·URL 부분은 뺀다.
ROBOTS_USER_AGENT = "ASKU-Crawler"


@dataclass(frozen=True)
class ListingItem:
    """목록에서 발견한 상세 링크와 Extractor에 넘길 메타데이터 힌트."""

    url: str
    title_hint: str | None = None
    category_hint: str | None = None
    author_hint: str | None = None
    published_at_hint: datetime | None = None


@dataclass(frozen=True)
class CrawlSettings:
    request_delay_seconds: float = 1.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    timeout_seconds: float = 15.0


class NoticeAdapter(Protocol):
    """학교별 HTML 차이를 감추는 목록/상세 어댑터 계약."""

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]: ...

    def parse_attachments(self, html: str, page_url: str) -> list[Attachment]: ...

    def next_listing_url(self, html: str, page_url: str) -> str | None: ...


class CommonNoticeAdapter:
    """서버 렌더링 대학 게시판의 공통 파서.

    `row_selector`와 `detail_link_selector`를 학교별로 넘겨 선택자를 덮어쓸 수 있다.
    기본값은 table 기반 .do 게시판을 대상으로 하며, 실제 사이트의 예외는 별도
    어댑터에서 처리한다.
    """

    def __init__(
        self,
        *,
        row_selector: str = "table tbody tr",
        detail_link_selector: str = "a[href]",
        attachment_selector: str = "a[href$='.pdf'], a[href$='.hwp'], a[href$='.hwpx'], a[href$='.doc'], a[href$='.docx']",
    ) -> None:
        self.row_selector = row_selector
        self.detail_link_selector = detail_link_selector
        self.attachment_selector = attachment_selector

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select(self.row_selector):
            link = row.select_one(self.detail_link_selector)
            if link is None or not link.get("href"):
                continue
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            title = link.get_text(" ", strip=True) or None
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=title,
                # 국내 공지 게시판의 일반적인 순서: 번호, 분류, 제목, 작성부서, 등록일
                category_hint=cells[1] if len(cells) >= 5 else None,
                author_hint=cells[-2] if len(cells) >= 4 else None,
                published_at_hint=_parse_date(cells[-1]) if cells else None,
            )

    def parse_attachments(self, html: str, page_url: str) -> list[Attachment]:
        soup = BeautifulSoup(html, "html.parser")
        return [
            Attachment(url=urljoin(page_url, str(link["href"])), name_hint=link.get_text(" ", strip=True) or None)
            for link in soup.select(self.attachment_selector)
            if link.get("href")
        ]

    def next_listing_url(self, html: str, page_url: str) -> str | None:
        """공통 페이지네이션의 '다음' 링크를 찾는다.

        구조가 다른 사이트는 이 메서드만 오버라이드하면 된다. 링크가 없으면
        현재 목록 페이지가 마지막 페이지로 간주된다.
        """
        soup = BeautifulSoup(html, "html.parser")
        selectors = (
            "a[rel='next'][href]",
            ".pagination .next a[href]",
            ".paging .next a[href]",
            "a[aria-label='다음 페이지'][href]",
            "a[title='다음 페이지'][href]",
            "a[title='다음 페이지로 이동하기'][href]",
        )
        for selector in selectors:
            link = soup.select_one(selector)
            if link and link.get("href"):
                return urljoin(page_url, str(link["href"]))
        return None


class YonseiNoticeAdapter(CommonNoticeAdapter):
    """연세대 K2Web 공지 목록용 학교별 오버라이드."""

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select(".boardWrap > ul > li"):
            link = row.select_one("a[href*='artclView.do']")
            if link is None or not link.get("href"):
                continue
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=_text_or_none(row.select_one(".title")),
                category_hint=_text_or_none(row.select_one(".notice-title")),
                author_hint=_text_or_none(row.select_one(".etc-area")),
                published_at_hint=_parse_date(_text_or_none(row.select_one(".date-area")) or ""),
            )

    def next_listing_url(self, html: str, page_url: str) -> str | None:
        """K2Web의 JavaScript 페이지 이동을 GET 호환 URL로 바꾼다."""
        soup = BeautifulSoup(html, "html.parser")
        next_link = soup.select_one("._paging ._listNext[href]")
        page_form = soup.select_one("form[name='pageForm'][action]")
        if next_link is None or page_form is None:
            return None
        match = re.search(r"page_link\('(?P<page>\d+)'\)", str(next_link.get("href")))
        if match is None:
            return None
        params = {
            str(input_tag["name"]): str(input_tag.get("value", ""))
            for input_tag in page_form.select("input[name]")
        }
        params["page"] = match.group("page")
        return f"{urljoin(page_url, str(page_form['action']))}?{urlencode(params)}"


class SejongNoticeAdapter(CommonNoticeAdapter):
    """세종대 K2Web 공지 목록용 학교별 오버라이드.

    ``tr.b-top-box``는 상단 고정공지에만 붙으므로 행 선택자로 쓰면 일반 공지가
    통째로 누락된다(실측: 16행 중 6행). 상세 링크가 있는 행 전체를 읽는다.
    """

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("table tbody tr"):
            link = row.select_one(".b-title-box a[href*='mode=view'][href*='articleNo']")
            if link is None or not link.get("href"):
                continue
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=_text_or_none(row.select_one(".b-title")) or _text_or_none(link),
                author_hint=_text_or_none(row.select_one(".b-writer")),
                published_at_hint=_parse_date(_text_or_none(row.select_one(".b-date")) or ""),
            )


class HongikNoticeAdapter(CommonNoticeAdapter):
    """홍익대 K2Web 공지 목록용 학교별 오버라이드."""

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("tr.b-top-box"):
            link = row.select_one(".b-title-box a[href*='mode=view'][href*='articleNo']")
            if link is None or not link.get("href"):
                continue
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=_text_or_none(row.select_one(".b-title")) or _text_or_none(link),
                category_hint=_text_or_none(row.select_one(".b-mini-cate")),
                published_at_hint=_parse_date(_text_or_none(row.select_one(".b-date")) or ""),
            )


class SkkuNoticeAdapter(CommonNoticeAdapter):
    """성균관대 K2Web ``dl`` 공지 목록용 학교별 오버라이드."""

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("dl.board-list-content-wrap"):
            link = row.select_one("dt.board-list-content-title a[href*='articleNo']")
            info = row.select("dd.board-list-content-info li")
            if link is None or not link.get("href"):
                continue
            values = [item.get_text(" ", strip=True) for item in info]
            category = values[0] if len(values) >= 1 and not re.fullmatch(r"No\.\s*\d+", values[0]) else None
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=_text_or_none(link),
                category_hint=category,
                author_hint=values[1] if len(values) >= 2 else None,
                published_at_hint=_parse_date(values[2]) if len(values) >= 3 else None,
            )


# 호스트별 학교 어댑터. 미등록 호스트는 `CommonNoticeAdapter` 로 폴백한다.
# 새 학교를 추가할 땐 어댑터 클래스와 함께 여기에 호스트를 등록한다.
ADAPTER_REGISTRY: dict[str, type[CommonNoticeAdapter]] = {
    "www.yonsei.ac.kr": YonseiNoticeAdapter,
    "www.sejong.ac.kr": SejongNoticeAdapter,
    "www.hongik.ac.kr": HongikNoticeAdapter,
    "www.skku.edu": SkkuNoticeAdapter,
}


def adapter_for(base_url: str, spec: AdapterSpec | None = None) -> CommonNoticeAdapter:
    """`base_url` 에 맞는 어댑터를 만든다.

    ``전용 클래스 → 규격 → 공용 파서`` 순으로 고른다. 손으로 검증한 전용 클래스가
    규격보다 앞서므로, 자동 생성한 규격이 이상해도 기존 학교는 영향을 받지 않는다.

    공용 파서는 `table tbody tr` 기반이라 연세대(`ul > li`)·성균관대(`dl`)처럼
    구조가 다른 게시판에서는 목록을 한 줄도 읽지 못한다. 어느 단계로도 잡히지
    않는 학교는 수집이 0건이 되므로 검증기(`app.validation`)가 걸러야 한다.

    `spec` 은 호출자가 저장소에서 꺼내 넘긴다. Crawler 가 Storage 를 직접 알지
    않도록 하기 위해서다.
    """

    host = (urlsplit(base_url).hostname or "").lower()
    if host in ADAPTER_REGISTRY:
        return ADAPTER_REGISTRY[host]()
    if spec is not None:
        return SpecNoticeAdapter(spec)
    return CommonNoticeAdapter()


# 라벨 없는 단일 게시판을 지표에 표기할 때 쓰는 이름.
DEFAULT_BOARD_LABEL = "기본"


@dataclass(frozen=True)
class Board:
    """수집할 게시판 하나. `label` 은 공지의 분류 힌트로 전달된다."""

    url: str
    label: str | None = None


# 호스트별 하위 게시판(탭). 한 학교의 공지가 탭으로 쪼개져 있으면 여기에 적는다.
# 미등록 호스트는 등록 URL 하나만 수집한다.
#
# 자동 탐색은 하지 않는다. 세종대는 같은 메뉴에 `qna1~8.do`(Q&A)가 섞여 있어
# URL 패턴만으로는 공지 게시판을 가려낼 수 없다.
BOARD_REGISTRY: dict[str, tuple[Board, ...]] = {
    "www.sejong.ac.kr": (
        Board("https://www.sejong.ac.kr/kor/intro/notice1.do", "일반공지"),
        Board("https://www.sejong.ac.kr/kor/intro/notice2.do", "입학공지"),
        Board("https://www.sejong.ac.kr/kor/intro/notice3.do", "학사공지"),
        Board("https://www.sejong.ac.kr/kor/intro/notice4.do", "국제교류"),
        Board("https://www.sejong.ac.kr/kor/intro/notice6.do", "취업"),
        Board("https://www.sejong.ac.kr/kor/intro/notice7.do", "장학"),
        Board("https://www.sejong.ac.kr/kor/intro/notice8.do", "채용·모집"),
        Board("https://www.sejong.ac.kr/kor/intro/notice9.do", "법무감사"),
        Board("https://www.sejong.ac.kr/kor/intro/notice10.do", "입찰공고"),
    ),
}


def boards_for(base_url: str, spec: AdapterSpec | None = None) -> tuple[Board, ...]:
    """수집할 게시판 목록. ``등록 목록 → 규격 → 기준 URL 하나`` 순으로 고른다."""

    host = (urlsplit(base_url).hostname or "").lower()
    if host in BOARD_REGISTRY:
        return BOARD_REGISTRY[host]
    if spec is not None and spec.boards:
        return tuple(Board(board.url, board.label) for board in spec.boards)
    return (Board(base_url),)


class SpecNoticeAdapter(CommonNoticeAdapter):
    """규격(`AdapterSpec`)을 읽어 목록을 파싱하는 어댑터.

    학교마다 클래스를 만드는 대신 선택자를 데이터로 받는다. 동작은 전용 어댑터와
    같아야 하며, 그 동일성은 학교별 회귀 테스트로 확인한다.
    """

    def __init__(self, spec: "AdapterSpec") -> None:
        listing = spec.listing
        super().__init__(
            row_selector=listing.row,
            detail_link_selector=listing.detail_link,
            attachment_selector=spec.detail.attachment or CommonNoticeAdapter().attachment_selector,
        )
        self.spec = spec

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        listing = self.spec.listing
        soup = BeautifulSoup(html, "html.parser")
        for row in _first_matching(soup, listing.row):
            link = row.select_one(listing.detail_link)
            if link is None or not link.get("href"):
                continue
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                # 제목 선택자가 없으면 링크 텍스트를 쓴다. 링크 안에 제목이 그대로
                # 들어 있는 게시판이 흔하다.
                title_hint=_pick(row, listing.title) or _text_or_none(link),
                category_hint=_drop_if_matches(_pick(row, listing.category), listing.category_ignore),
                author_hint=_pick(row, listing.author),
                published_at_hint=_parse_date(_pick(row, listing.date) or ""),
            )

    def next_listing_url(self, html: str, page_url: str) -> str | None:
        pagination = self.spec.listing.pagination
        if pagination is None:
            return super().next_listing_url(html, page_url)
        if pagination.type == "link":
            link = BeautifulSoup(html, "html.parser").select_one(pagination.selector)
            if link is None or not link.get("href"):
                return None
            return urljoin(page_url, str(link["href"]))
        if pagination.type == "offset":
            return _advance_offset(page_url, pagination.param, pagination.step, pagination.start)
        return _form_next_url(html, page_url, pagination)


def _first_matching(soup, selectors: list[str]) -> list:
    """행이 하나라도 잡히는 첫 후보의 결과를 쓴다.

    같은 게시판 제품이라도 목록을 표로 그리는 학교와 `ul > li` 로 그리는 학교가
    있다. 후보를 순서대로 대보면 규격 하나로 둘 다 덮는다.
    """

    for selector in selectors:
        rows = soup.select(selector)
        if rows:
            return rows
    return []


def _drop_if_matches(value: str | None, pattern: str | None) -> str | None:
    """분류 자리에 분류가 아닌 값(글 번호 등)이 오면 버린다."""

    if value is None or pattern is None:
        return value
    return None if re.fullmatch(pattern, value.strip()) else value


def _pick(row, selectors: list[str]) -> str | None:
    """행 안에서 후보 선택자를 훑어 **값이 나오는 첫 번째** 결과를 쓴다.

    요소가 있어도 비어 있으면 다음 후보로 넘어간다. 아주대는 `.b-date` 요소가
    모바일용이라 존재하되 비어 있고, 실제 날짜는 마지막 칸에 있다. 요소 유무만
    보면 빈 값을 잡고 멈춘다.
    """

    for selector in selectors:
        value = _text_or_none(row.select_one(selector))
        if value:
            return value
    return None


def _advance_offset(page_url: str, param: str, step: int, start: int = 0) -> str:
    """목록 URL 의 페이지 파라미터를 한 페이지만큼 늘린다.

    파라미터가 없으면 `start` 를 현재 값으로 본다. 페이지 번호를 쓰는 게시판은
    1페이지가 `1` 이라, 0 에서 더하면 같은 페이지를 다시 요청하게 된다.
    """

    split = urlsplit(page_url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    current = query.get(param)
    base = int(current) if current and current.isdigit() else start
    query[param] = str(base + step)
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def _form_next_url(html: str, page_url: str, pagination) -> str | None:
    """폼의 hidden input 을 모아 다음 페이지 URL 을 만든다(연세대 K2Web)."""

    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.select_one(pagination.next_selector)
    page_form = soup.select_one(pagination.form_selector)
    if next_link is None or page_form is None or not page_form.get("action"):
        return None
    match = re.search(pagination.page_pattern, str(next_link.get("href") or ""))
    if match is None:
        return None
    params = {
        str(tag["name"]): str(tag.get("value", ""))
        for tag in page_form.select("input[name]")
    }
    params[pagination.page_param] = match.group(1)
    return f"{urljoin(page_url, str(page_form['action']))}?{urlencode(params)}"


HashExists = Callable[[int, str, str], bool]
UrlExists = Callable[[int, str], bool]
RobotsAllowed = Callable[[str], bool]


class CrawlStorage(Protocol):
    """변경 감지에 필요한 Storage의 최소 공개 인터페이스."""

    def doc_hash_exists(self, school_id: int, source_url: str, content_hash: str) -> bool: ...

    def doc_url_exists(self, school_id: int, source_url: str) -> bool: ...


@dataclass
class CrawlRun:
    pages: list[CrawledPage] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)
    # 게시판별 첫 목록 페이지 HTML. 수집 품질 검증이 파서를 다시 돌려 보기 위해
    # 남긴다(`app.validation`). 목록 전체를 들고 있지는 않는다.
    first_listing_html: dict[str, str] = field(default_factory=dict)
    # 상세 URL → 게시판 라벨. 어느 게시판에서 나온 공지인지 기록한다.
    # `category_hint` 로 되짚을 수 없다. 목록이 자체 분류를 주면 게시판 라벨 대신
    # 그 값이 들어가, 검증이 페이지를 게시판에 붙이지 못하고 조용히 건너뛴다.
    board_of: dict[str, str] = field(default_factory=dict)


@dataclass
class _BoardCursor:
    """게시판 하나의 순회 상태. 라운드 로빈으로 한 페이지씩 처리하며 이어간다."""

    board: Board
    listing_url: str | None
    visited_listing_urls: set[str] = field(default_factory=set)
    collected: int = 0
    pages_done: int = 0

    @property
    def finished(self) -> bool:
        """더 볼 목록 페이지가 없다."""

        return self.listing_url is None


@dataclass
class _CrawlBudget:
    """크롤 1회 전체를 묶는 요청 수·시간 예산.

    페이지·건수 상한은 게시판마다 따로 적용되므로 하위 게시판이 늘어나면 총
    요청량을 못 막는다. 예산이 바닥나면 예외를 던지지 않고 그때까지 모은 결과를
    유지한 채 수집을 멈춘다 — 부분 수집이 전량 실패보다 낫다.
    """

    max_requests: int
    deadline: float
    clock: Callable[[], float]
    requests_used: int = 0
    exceeded_code: str | None = None

    def charge(self) -> bool:
        """요청 하나를 예산에서 차감한다. 예산이 남아 있으면 True."""

        if self.exceeded_code is not None:
            return False
        if self.requests_used >= self.max_requests:
            self.exceeded_code = "REQUEST_BUDGET_EXCEEDED"
            return False
        if self.clock() >= self.deadline:
            self.exceeded_code = "TIME_BUDGET_EXCEEDED"
            return False
        self.requests_used += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.exceeded_code is not None


@dataclass(frozen=True)
class RobotsPolicy:
    """오리진 하나의 robots.txt 판정 결과. 성공·거부 모두 캐시해 재요청을 막는다.

    ``parser`` 가 ``None`` 이면 그 오리진 전체가 금지다(응답을 얻지 못했거나 401/403·5xx).
    그때 ``denial_code`` 가 실패 이력에 남길 사유를 들고 있다.
    """

    parser: Protego | None
    denial_code: str | None = None

    def allows(self, url: str) -> bool:
        if self.parser is None:
            return False
        return self.parser.can_fetch(url, ROBOTS_USER_AGENT)

    def crawl_delay(self) -> float | None:
        """robots.txt 가 선언한 요청 간격(초). 선언이 없으면 None."""

        if self.parser is None:
            return None
        declared = self.parser.crawl_delay(ROBOTS_USER_AGENT)
        return float(declared) if declared is not None else None


class Crawler:
    """정적 HTML 기반 공지 수집기.

    `hash_exists`는 Storage의 `doc_hash_exists`를 연결한다. `url_exists`는 내용이
    달라진 기존 URL을 `changed`로 분류하기 위한 보조 조회다. Storage에 해당
    공개 인터페이스가 추가되기 전에는 생략할 수 있으며, 그 경우 미일치 페이지는
    보수적으로 `new`로 분류한다.
    """

    def __init__(
        self,
        hash_exists: HashExists,
        url_exists: UrlExists | None = None,
        *,
        settings: CrawlSettings | None = None,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = sleep,
        robots_allowed: RobotsAllowed | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.hash_exists = hash_exists
        self.url_exists = url_exists
        self.settings = settings or CrawlSettings()
        self.session = session or requests.Session()
        # requests.Session에는 기본 ``python-requests/...`` User-Agent가 이미 있어
        # setdefault로는 교체되지 않는다. 자동 생성한 세션에만 Crawler 식별값을 넣고,
        # 호출자가 주입한 세션의 헤더는 그대로 존중한다.
        if session is None:
            self.session.headers.update({"User-Agent": USER_AGENT})
        self.sleeper = sleeper
        self.clock = clock
        self.robots_allowed = robots_allowed or self._robots_allowed
        self._policy_by_origin: dict[str, RobotsPolicy] = {}

    @classmethod
    def from_storage(cls, storage: CrawlStorage, **kwargs: object) -> "Crawler":
        """Storage의 공개 조회 인터페이스를 변경 감지 콜백으로 연결한다."""

        return cls(
            hash_exists=storage.doc_hash_exists,
            url_exists=storage.doc_url_exists,
            **kwargs,
        )

    def crawl(self, request: CrawlRequest, adapter: NoticeAdapter) -> CrawlRun:
        """`base_url` 게시판 하나를 수집한다."""

        return self.crawl_boards(request, (Board(request.base_url),), adapter)

    def crawl_boards(self, request: CrawlRequest, boards: Iterable[Board], adapter: NoticeAdapter) -> CrawlRun:
        """하위 게시판(탭) 여러 개를 한 번의 크롤로 수집한다.

        게시판을 하나씩 끝까지 도는 대신 **한 페이지씩 번갈아** 돈다. 목록은
        최신순이므로 이렇게 하면 예산이 부족해도 모든 탭의 최신 공지가 먼저
        확보된다. 순서대로 돌면 공지가 많은 앞쪽 탭이 예산을 다 써서 뒤쪽 탭이
        한 건도 수집되지 않는다.

        예산과 중복 URL 집합은 게시판 사이에서 공유한다. 게시판마다 새로 잡으면
        탭이 늘어난 만큼 총 요청량이 그대로 늘어나고, 여러 탭에 함께 걸린 공지를
        중복 수집하게 된다.
        """

        run = CrawlRun()
        scope = request.scope
        budget = _CrawlBudget(
            max_requests=scope.max_requests if scope else 500,
            deadline=self.clock() + (scope.max_duration_seconds if scope else 600.0),
            clock=self.clock,
        )
        visited_detail_urls: set[str] = set()
        cursors = [_BoardCursor(board=board, listing_url=board.url) for board in boards]

        while cursors and not budget.exhausted:
            for cursor in list(cursors):
                if budget.exhausted:
                    break
                self._crawl_listing_page(request, cursor, adapter, run, budget, visited_detail_urls)
                if cursor.finished:
                    cursors.remove(cursor)
        return run

    def _crawl_listing_page(
        self,
        request: CrawlRequest,
        cursor: "_BoardCursor",
        adapter: NoticeAdapter,
        run: CrawlRun,
        budget: _CrawlBudget,
        visited_detail_urls: set[str],
    ) -> None:
        """게시판 하나의 목록 **한 페이지**를 처리하고 커서를 다음 페이지로 옮긴다."""

        scope = request.scope
        max_listing_pages = scope.max_listing_pages if scope else 10
        max_items = scope.max_items if scope else 300
        board = cursor.board
        listing_url = cursor.listing_url
        assert listing_url is not None  # `finished` 커서는 호출자가 걸러낸다
        cursor.listing_url = None  # 아래에서 다음 페이지를 찾으면 다시 채운다

        canonical_listing_url = normalize_url(listing_url)
        if canonical_listing_url in cursor.visited_listing_urls or cursor.pages_done >= max_listing_pages:
            return
        cursor.visited_listing_urls.add(canonical_listing_url)
        cursor.pages_done += 1

        # 첫 페이지(base_url)뿐 아니라 다음 목록 페이지도 매번 검사한다. 페이지네이션
        # URL 만 막아 둔 robots.txt 를 2페이지부터 그냥 통과시키면 안 된다.
        if not self.robots_allowed(listing_url):
            self._policy_failure(request, listing_url, run)
            return
        listing_html = self._fetch(request, listing_url, run, budget=budget)
        if listing_html is None:
            if budget.exhausted:
                self._budget_failure(request, listing_url, run, budget)
            return
        if cursor.pages_done == 1:
            run.first_listing_html[board.label or DEFAULT_BOARD_LABEL] = listing_html

        # 재크롤 조기 종료 판단용. 목록은 최신순이라 한 페이지가 통째로
        # unchanged 면 뒤쪽은 볼 필요가 없다.
        page_had_items = False
        page_had_updates = False

        for item in adapter.parse_listing(listing_html, listing_url):
            # 건수 상한은 게시판마다 따로 센다. 공유하면 공지가 많은 첫 탭이 상한을
            # 다 써 뒤쪽 탭이 한 건도 수집되지 않는다.
            if cursor.collected >= max_items:
                return
            canonical_url = normalize_detail_url(item.url)
            if canonical_url in visited_detail_urls or not is_allowed(canonical_url, request):
                continue
            visited_detail_urls.add(canonical_url)
            if not self.robots_allowed(canonical_url):
                self._policy_failure(request, canonical_url, run)
                continue
            # 일부 학교는 목록에서 상세 공지로 이동한 요청만 허용한다.
            # 브라우저 클릭과 동일하게 현재 목록 URL을 Referer로 전달한다.
            html = self._fetch(request, canonical_url, run, referer=listing_url, budget=budget)
            if html is None:
                if budget.exhausted:
                    self._budget_failure(request, canonical_url, run, budget)
                    return
                continue
            content_hash = html_hash(html)
            if self.hash_exists(request.school_id, canonical_url, content_hash):
                status = "unchanged"
            elif self.url_exists and self.url_exists(request.school_id, canonical_url):
                status = "changed"
            else:
                status = "new"
            cursor.collected += 1
            run.board_of[canonical_url] = board.label or DEFAULT_BOARD_LABEL
            page_had_items = True
            page_had_updates = page_had_updates or status != "unchanged"

            run.pages.append(
                CrawledPage(
                    crawl_id=request.crawl_id,
                    school_id=request.school_id,
                    source_url=item.url,
                    canonical_url=canonical_url,
                    title_hint=item.title_hint,
                    # 게시판 라벨은 목록이 분류를 주지 않을 때만 쓴다. 홍익대처럼
                    # 행마다 분류가 붙는 학교의 값을 탭 이름으로 덮으면 안 된다.
                    category_hint=item.category_hint or board.label,
                    author_hint=item.author_hint,
                    published_at_hint=item.published_at_hint,
                    raw_html=html,
                    attachments=adapter.parse_attachments(html, canonical_url),
                    content_hash=content_hash,
                    fetched_at=datetime.now(timezone.utc),
                    crawl_status=status,
                )
            )

        # 재크롤에서 이 페이지가 전부 unchanged 였다면 더 오래된 페이지도 마찬가지다.
        if request.mode == "recrawl" and page_had_items and not page_had_updates:
            return

        next_url = adapter.next_listing_url(listing_html, listing_url)
        if next_url is not None and is_allowed(normalize_url(next_url), request):
            cursor.listing_url = next_url

    def pages_for_extractor(self, run: CrawlRun) -> list[CrawledPage]:
        """신규·변경 페이지만 다음 단계로 전달한다."""
        return [page for page in run.pages if page.crawl_status in {"new", "changed"}]

    def _fetch(
        self,
        request: CrawlRequest,
        url: str,
        run: CrawlRun,
        *,
        referer: str | None = None,
        budget: _CrawlBudget | None = None,
    ) -> str | None:
        for attempt in range(self.settings.max_retries + 1):
            # 재시도도 서버에 대한 요청이므로 시도마다 예산을 쓴다.
            if budget is not None and not budget.charge():
                return None
            try:
                headers = {"Referer": referer} if referer else None
                response = self.session.get(url, timeout=self.settings.timeout_seconds, headers=headers)
                # 응답을 받았으면 상태 코드와 무관하게 간격을 지킨다. 200 일 때만 쉬면
                # 죽은 링크가 늘어선 목록에서 무지연으로 연타하게 된다.
                self.sleeper(self._crawl_delay(url))
                if response.status_code == 200:
                    return response.text
                retryable = response.status_code in RETRYABLE_STATUS_CODES
                error_code = f"HTTP_{response.status_code}"
            except requests.RequestException as error:
                retryable = True
                error_code = type(error).__name__.upper()

            if not retryable or attempt == self.settings.max_retries:
                run.failures.append(
                    CrawlFailure(
                        crawl_id=request.crawl_id,
                        school_id=request.school_id,
                        source_url=url,
                        stage="fetch",
                        error_code=error_code,
                        retryable=retryable,
                        occurred_at=datetime.now(timezone.utc),
                    )
                )
                return None
            self.sleeper(self.settings.backoff_seconds * (2**attempt))
        return None

    def _robots_allowed(self, url: str) -> bool:
        """robots.txt 판정. 정책을 확인할 수 없으면 수집하지 않는다(보수적 기본값).

        표준 ``urllib.robotparser`` 대신 ``protego`` 를 쓴다. 표준 파서는 경로 안
        와일드카드(``Disallow: /*?mode=view``)와 ``$`` 앵커를 지원하지 않아 금지된
        URL 을 허용으로 판정하고, Allow/Disallow 우선순위도 RFC 9309 의 최장 일치가
        아니라 파일에 쓰인 순서로 정해 허용된 게시판을 통째로 금지로 판정한다.
        """

        return self._robots_policy(url).allows(url)

    def _crawl_delay(self, url: str) -> float:
        """요청 간 대기 시간. robots.txt 의 ``Crawl-delay`` 와 설정값 중 긴 쪽을 쓴다.

        이미 받아둔 판정만 참고하고 여기서 robots.txt 를 새로 가져오지는 않는다.
        정상 흐름에서는 수집 전에 robots 검사를 거쳐 캐시가 채워져 있고, 호출자가
        ``robots_allowed`` 를 직접 주입했다면 참고할 robots.txt 자체가 없다.
        """

        policy = self._policy_by_origin.get(_origin(url))
        declared = policy.crawl_delay() if policy is not None else None
        if declared is None:
            return self.settings.request_delay_seconds
        return max(self.settings.request_delay_seconds, declared)

    def _robots_policy(self, url: str) -> RobotsPolicy:
        """오리진의 robots.txt 판정을 얻는다. 성공·거부 모두 한 번만 가져와 캐시한다.

        거부 결과를 캐시하지 않으면 robots.txt 가 401/403 인 사이트에 URL 마다 다시
        요청하게 된다 — 수집을 거부한 서버를 오히려 수백 번 두드리는 꼴이다.
        """

        origin = _origin(url)
        cached = self._policy_by_origin.get(origin)
        if cached is not None:
            return cached

        try:
            response = self.session.get(f"{origin}/robots.txt", timeout=self.settings.timeout_seconds)
        except requests.RequestException:
            policy = RobotsPolicy(parser=None, denial_code="ROBOTS_UNREACHABLE")
        else:
            self.sleeper(self.settings.request_delay_seconds)
            policy = _policy_from_status(response)

        self._policy_by_origin[origin] = policy
        return policy

    def _policy_failure(self, request: CrawlRequest, url: str, run: CrawlRun) -> None:
        # 오리진 전체가 막힌 경우에는 그 사유를, 개별 경로가 막힌 경우에는 기본 사유를 남긴다.
        policy = self._policy_by_origin.get(_origin(url))
        error_code = policy.denial_code if policy is not None and policy.denial_code else "ROBOTS_DISALLOWED"
        run.failures.append(
            CrawlFailure(
                crawl_id=request.crawl_id,
                school_id=request.school_id,
                source_url=url,
                stage="policy",
                # 네트워크·서버 오류로 robots.txt 를 못 읽은 것은 정책 거부와 달리 나중에 풀린다.
                error_code=error_code,
                retryable=error_code != "ROBOTS_DISALLOWED",
                occurred_at=datetime.now(timezone.utc),
            )
        )

    @staticmethod
    def _budget_failure(request: CrawlRequest, url: str, run: CrawlRun, budget: _CrawlBudget) -> None:
        """예산 소진으로 중단했다는 사실을 남긴다.

        기록이 없으면 "수집이 왜 여기서 끊겼는지" 알 수 없어 목록이 짧은 것인지
        상한에 걸린 것인지 구분되지 않는다. 다음 크롤에서 이어받으면 되므로
        재시도 가능으로 표시한다.
        """

        run.failures.append(
            CrawlFailure(
                crawl_id=request.crawl_id,
                school_id=request.school_id,
                source_url=url,
                stage="budget",
                error_code=budget.exceeded_code or "BUDGET_EXCEEDED",
                retryable=True,
                occurred_at=datetime.now(timezone.utc),
            )
        )


def _origin(url: str) -> str:
    """robots.txt 는 오리진(스킴+호스트+포트) 단위로 적용된다."""

    split = urlsplit(url)
    return f"{split.scheme}://{split.netloc}"


def _policy_from_status(response: requests.Response) -> RobotsPolicy:
    """robots.txt 응답 상태를 RFC 9309 §2.3.1 대로 판정으로 옮긴다.

    - 200: 규칙을 그대로 따른다.
    - 401·403(접근 거부): 오리진 전체 금지.
    - 그 밖의 4xx(404 등, "robots.txt 없음"): 전면 허용.
    - 5xx: 일시적 전면 금지. 빈 규칙으로 읽어 전면 허용하면 서버가 아플 때
      오히려 더 많이 긁게 된다.
    """

    status = response.status_code
    if status in {401, 403}:
        return RobotsPolicy(parser=None, denial_code="ROBOTS_DISALLOWED")
    if status >= 500:
        return RobotsPolicy(parser=None, denial_code="ROBOTS_UNREACHABLE")
    return RobotsPolicy(parser=Protego.parse(response.text if status == 200 else ""))


def normalize_url(url: str) -> str:
    """fragment와 추적 파라미터를 제거한 비교용 URL을 만든다."""
    split = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = split.path.rstrip("/") or "/"
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), path, urlencode(sorted(query)), ""))


def normalize_detail_url(url: str) -> str:
    """상세 공지 URL에서 목록 페이지 문맥 파라미터를 제거한다.

    일부 K2Web 사이트는 동일 게시글 링크에 목록의 `article.offset`과
    `articleLimit`을 함께 넣는다. 이 값은 본문을 식별하지 않으므로 중복 판정에서
    제외한다. 목록 URL 자체는 `normalize_url`을 사용해 페이지 번호를 유지한다.
    """
    split = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if key.lower() not in DETAIL_CONTEXT_QUERY_KEYS
    ]
    return normalize_url(urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment)))


def is_allowed(url: str, request: CrawlRequest) -> bool:
    """요청 스코프의 호스트·경로 제한을 적용한다."""
    if not request.scope:
        return True
    split = urlsplit(url)
    hosts = {host.lower() for host in request.scope.allowed_hosts}
    if hosts and split.netloc.lower() not in hosts:
        return False
    return not request.scope.path_prefixes or any(split.path.startswith(prefix) for prefix in request.scope.path_prefixes)


def html_hash(html: str) -> str:
    """공백 변화에 덜 민감한 본문 HTML 해시."""
    normalized = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _parse_date(value: str) -> datetime | None:
    """게시판 날짜 표기를 읽는다. 두 자리 연도(`26.08.05`)도 받는다.

    같은 페이지 안에서도 표기가 갈린다 — 아주대는 데스크톱 칸이 `2026-08-05`,
    모바일 요소가 `26.08.05` 다. 두 자리를 못 읽으면 어느 선택자를 고르느냐에
    따라 날짜가 통째로 비어 규격이 실패한다.
    """

    match = re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", value)
    if match is not None:
        value = match.group(0)
    else:
        # 두 자리 연도. 게시판 날짜에 과거 세기가 나올 일은 없으므로 2000년대로 읽는다.
        short = re.search(r"\b(\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", value)
        if short is not None:
            year, month, day = (int(part) for part in short.groups())
            try:
                return datetime(2000 + year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
    for pattern in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _text_or_none(element: object) -> str | None:
    if element is None:
        return None
    text = element.get_text(" ", strip=True)  # type: ignore[union-attr]
    return text or None
