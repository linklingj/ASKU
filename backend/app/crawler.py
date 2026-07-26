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
from time import sleep
from typing import Callable, Iterable, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from app.schemas import Attachment, CrawledPage, CrawlFailure, CrawlRequest


TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DETAIL_CONTEXT_QUERY_KEYS = {"article.offset", "articlelimit"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
USER_AGENT = "ASKU-Crawler/0.1 (+https://github.com/linklingj/ASKU)"


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
            yield ListingItem(
                url=urljoin(page_url, str(link["href"])),
                title_hint=_text_or_none(link),
                category_hint=values[0] if len(values) >= 1 else None,
                author_hint=values[1] if len(values) >= 2 else None,
                published_at_hint=_parse_date(values[2]) if len(values) >= 3 else None,
            )


HashExists = Callable[[int, str, str], bool]
UrlExists = Callable[[int, str], bool]
RobotsAllowed = Callable[[str], bool]


@dataclass
class CrawlRun:
    pages: list[CrawledPage] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)


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
        self.robots_allowed = robots_allowed or self._robots_allowed
        self._robots_by_origin: dict[str, RobotFileParser] = {}

    def crawl(self, request: CrawlRequest, adapter: NoticeAdapter) -> CrawlRun:
        run = CrawlRun()
        if not self.robots_allowed(request.base_url):
            self._policy_failure(request, request.base_url, run)
            return run
        scope = request.scope
        max_listing_pages = scope.max_listing_pages if scope else 10
        max_items = scope.max_items if scope else 300
        listing_url = request.base_url
        visited_listing_urls: set[str] = set()
        visited_detail_urls: set[str] = set()

        for _ in range(max_listing_pages):
            canonical_listing_url = normalize_url(listing_url)
            if canonical_listing_url in visited_listing_urls:
                break
            visited_listing_urls.add(canonical_listing_url)
            listing_html = self._fetch(request, listing_url, run)
            if listing_html is None:
                break

            for item in adapter.parse_listing(listing_html, listing_url):
                if len(visited_detail_urls) >= max_items:
                    return run
                canonical_url = normalize_detail_url(item.url)
                if canonical_url in visited_detail_urls or not is_allowed(canonical_url, request):
                    continue
                visited_detail_urls.add(canonical_url)
                if not self.robots_allowed(canonical_url):
                    self._policy_failure(request, canonical_url, run)
                    continue
                html = self._fetch(request, canonical_url, run)
                if html is None:
                    continue
                content_hash = html_hash(html)
                if self.hash_exists(request.school_id, canonical_url, content_hash):
                    status = "unchanged"
                elif self.url_exists and self.url_exists(request.school_id, canonical_url):
                    status = "changed"
                else:
                    status = "new"

                run.pages.append(
                    CrawledPage(
                        crawl_id=request.crawl_id,
                        school_id=request.school_id,
                        source_url=item.url,
                        canonical_url=canonical_url,
                        title_hint=item.title_hint,
                        category_hint=item.category_hint,
                        author_hint=item.author_hint,
                        published_at_hint=item.published_at_hint,
                        raw_html=html,
                        attachments=adapter.parse_attachments(html, canonical_url),
                        content_hash=content_hash,
                        fetched_at=datetime.now(timezone.utc),
                        crawl_status=status,
                    )
                )

            next_url = adapter.next_listing_url(listing_html, listing_url)
            if next_url is None or not is_allowed(normalize_url(next_url), request):
                break
            listing_url = next_url
        return run

    def pages_for_extractor(self, run: CrawlRun) -> list[CrawledPage]:
        """신규·변경 페이지만 다음 단계로 전달한다."""
        return [page for page in run.pages if page.crawl_status in {"new", "changed"}]

    def _fetch(self, request: CrawlRequest, url: str, run: CrawlRun) -> str | None:
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.settings.timeout_seconds)
                if response.status_code == 200:
                    self.sleeper(self.settings.request_delay_seconds)
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
        split = urlsplit(url)
        origin = f"{split.scheme}://{split.netloc}"
        parser = self._robots_by_origin.get(origin)
        if parser is None:
            try:
                response = self.session.get(f"{origin}/robots.txt", timeout=self.settings.timeout_seconds)
            except requests.RequestException:
                return False  # 정책을 확인할 수 없으면 수집하지 않는다.
            self.sleeper(self.settings.request_delay_seconds)
            if response.status_code in {401, 403}:
                return False
            parser = RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            self._robots_by_origin[origin] = parser
        return parser.can_fetch("ASKU-Crawler", url)

    @staticmethod
    def _policy_failure(request: CrawlRequest, url: str, run: CrawlRun) -> None:
        run.failures.append(
            CrawlFailure(
                crawl_id=request.crawl_id,
                school_id=request.school_id,
                source_url=url,
                stage="policy",
                error_code="ROBOTS_DISALLOWED",
                retryable=False,
                occurred_at=datetime.now(timezone.utc),
            )
        )


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
