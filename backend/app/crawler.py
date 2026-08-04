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

import requests
from bs4 import BeautifulSoup
from protego import Protego

from app.schemas import Attachment, CrawledPage, CrawlFailure, CrawlRequest


TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DETAIL_CONTEXT_QUERY_KEYS = {"article.offset", "articlelimit"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
USER_AGENT = "ASKU-Crawler/0.1 (+https://github.com/linklingj/ASKU)"
# robots.txt 의 User-agent 매칭에 쓰는 제품 토큰(버전·URL 없는 이름 부분).
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
    # 첨부파일 1건의 다운로드 상한. 대형 첨부 하나가 워커 메모리를 잠식하고
    # 청킹·임베딩까지 오래 붙잡는 것을 막는다(03_crawler.md §6).
    max_attachment_bytes: int = 50 * 1024 * 1024
    attachment_chunk_bytes: int = 64 * 1024


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
    """세종대 K2Web 공지 목록용 학교별 오버라이드."""

    def parse_listing(self, html: str, page_url: str) -> Iterable[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("tr.b-top-box"):
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


HashExists = Callable[[int, str, str], bool]
UrlExists = Callable[[int, str], bool]
RobotsAllowed = Callable[[str], bool]


class CrawlStorage(Protocol):
    """변경 감지에 필요한 Storage의 최소 공개 인터페이스."""

    def doc_hash_exists(self, school_id: int, source_url: str, content_hash: str) -> bool: ...

    def doc_url_exists(self, school_id: int, source_url: str) -> bool: ...


@dataclass(frozen=True)
class DownloadedAttachment:
    """실제로 내려받은 첨부파일. ``url``은 근거 링크이자 저장소의 ``source_url``이 된다."""

    url: str
    filename: str
    content: bytes


@dataclass
class CrawlRun:
    pages: list[CrawledPage] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)
    # 같은 첨부가 여러 공지에 걸려 있어도 실행당 한 번만 받는다(중복 다운로드·임베딩 방지).
    # 성공·실패와 무관하게 시도한 URL을 담아 재시도 폭주도 함께 막는다.
    attempted_attachment_urls: set[str] = field(default_factory=set)


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
        self._robots_by_origin: dict[str, Protego] = {}

    @classmethod
    def from_storage(cls, storage: CrawlStorage, **kwargs: object) -> "Crawler":
        """Storage의 공개 조회 인터페이스를 변경 감지 콜백으로 연결한다."""

        return cls(
            hash_exists=storage.doc_hash_exists,
            url_exists=storage.doc_url_exists,
            **kwargs,
        )

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
                # 일부 학교는 목록에서 상세 공지로 이동한 요청만 허용한다.
                # 브라우저 클릭과 동일하게 현재 목록 URL을 Referer로 전달한다.
                html = self._fetch(request, canonical_url, run, referer=listing_url)
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

    def _fetch(self, request: CrawlRequest, url: str, run: CrawlRun, *, referer: str | None = None) -> str | None:
        response = self._request(request, url, run, referer=referer)
        return response.text if response is not None else None

    def fetch_pdf_attachments(
        self, request: CrawlRequest, page: CrawledPage, run: CrawlRun
    ) -> list[DownloadedAttachment]:
        """공지 페이지의 첨부파일 중 PDF만 실제로 내려받는다.

        HWP/DOC 등 다른 첨부 타입은 여전히 URL 힌트만 남기고 받지 않는다(PdfIngestor가
        PDF 전용이기 때문). 같은 URL은 실행(``run``)당 한 번만 시도한다 — 같은 첨부가
        여러 공지에 걸려 있을 때 중복 다운로드·임베딩을 막는다. 다운로드 실패와 크기
        초과는 ``CrawlFailure``로 기록하고 나머지 첨부·페이지 처리는 계속한다
        (03_crawler.md §6).

        첨부도 페이지와 똑같이 정책 검사를 거친다: 허용 호스트 밖이면 건너뛰고,
        ``robots.txt``가 막으면 정책 거부로 기록한다. 경로 제한(``path_prefixes``)은
        적용하지 않는다 — ``is_allowed_host`` 참고.
        """

        downloaded: list[DownloadedAttachment] = []
        for attachment in page.attachments:
            if not _looks_like_pdf(attachment.url):
                continue
            if attachment.url in run.attempted_attachment_urls:
                continue
            run.attempted_attachment_urls.add(attachment.url)
            if not is_allowed_host(attachment.url, request):
                continue
            if not self.robots_allowed(attachment.url):
                self._policy_failure(request, attachment.url, run)
                continue
            content = self._download_attachment(request, attachment.url, run, referer=page.canonical_url)
            if content is None:
                continue
            downloaded.append(
                DownloadedAttachment(
                    url=attachment.url,
                    filename=_attachment_filename(attachment),
                    content=content,
                )
            )
        return downloaded

    def _download_attachment(
        self, request: CrawlRequest, url: str, run: CrawlRun, *, referer: str | None = None
    ) -> bytes | None:
        """첨부 본문을 상한(``max_attachment_bytes``)까지만 스트리밍으로 읽는다.

        ``Content-Length``가 있으면 본문을 읽기 전에 먼저 거르고, 없거나 거짓이면
        읽는 도중 누적 크기로 다시 막는다.
        """

        response = self._request(request, url, run, referer=referer, stream=True)
        if response is None:
            return None

        limit = self.settings.max_attachment_bytes
        try:
            declared = str(response.headers.get("Content-Length") or "")
            if declared.isdigit() and int(declared) > limit:
                self._attachment_too_large(request, url, run)
                return None

            buffered = bytearray()
            for block in response.iter_content(self.settings.attachment_chunk_bytes):
                if not block:
                    continue
                buffered.extend(block)
                if len(buffered) > limit:
                    self._attachment_too_large(request, url, run)
                    return None
            return bytes(buffered)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _request(
        self, request: CrawlRequest, url: str, run: CrawlRun, *, referer: str | None = None, stream: bool = False
    ) -> requests.Response | None:
        for attempt in range(self.settings.max_retries + 1):
            try:
                headers = {"Referer": referer} if referer else None
                response = self.session.get(
                    url, timeout=self.settings.timeout_seconds, headers=headers, stream=stream
                )
                if response.status_code == 200:
                    self.sleeper(self.settings.request_delay_seconds)
                    return response
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
        """robots.txt 판정. 파서는 오리진별로 한 번만 만들어 재사용한다.

        표준 ``urllib.robotparser`` 대신 ``protego``를 쓴다. 표준 파서는 와일드카드
        (``Disallow: /*?mode=download``)를 경로 문자열로 URL 인코딩해 규칙을 무력화하고,
        더 구체적인 ``Allow``가 상위 ``Disallow``를 덮어쓰는 우선순위도 처리하지 못한다.
        실제 대학 사이트(세종대)에서 두 오판이 모두 재현됐다 — 전자는 금지된 첨부
        다운로드를 허용으로, 후자는 허용된 경로를 금지로 잘못 판정한다.
        """

        parser = self._robots_parser(url)
        # 정책을 확인할 수 없으면 수집하지 않는다(보수적 기본값).
        return False if parser is None else parser.can_fetch(url, ROBOTS_USER_AGENT)

    def _robots_parser(self, url: str) -> Protego | None:
        """오리진의 robots.txt 파서를 얻는다. 가져올 수 없으면 ``None``."""

        split = urlsplit(url)
        origin = f"{split.scheme}://{split.netloc}"
        if origin in self._robots_by_origin:
            return self._robots_by_origin[origin]
        try:
            response = self.session.get(f"{origin}/robots.txt", timeout=self.settings.timeout_seconds)
        except requests.RequestException:
            return None  # 정책을 확인할 수 없으면 수집하지 않는다(호출자가 거부 처리).
        self.sleeper(self.settings.request_delay_seconds)
        if response.status_code in {401, 403}:
            return None
        parser = Protego.parse(response.text if response.status_code == 200 else "")
        self._robots_by_origin[origin] = parser
        return parser

    @staticmethod
    def _attachment_too_large(request: CrawlRequest, url: str, run: CrawlRun) -> None:
        run.failures.append(
            CrawlFailure(
                crawl_id=request.crawl_id,
                school_id=request.school_id,
                source_url=url,
                stage="fetch",
                error_code="ATTACHMENT_TOO_LARGE",
                retryable=False,
                occurred_at=datetime.now(timezone.utc),
            )
        )

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


def is_allowed_host(url: str, request: CrawlRequest) -> bool:
    """요청 스코프의 호스트 제한만 적용한다.

    첨부파일 다운로드용이다. 첨부는 목록·상세와 다른 경로(`/files`, `/download` 등)에서
    서빙되는 것이 일반적이므로 `path_prefixes`는 적용하지 않는다. 그 제한은 크롤러가
    순회할 페이지 범위를 묶기 위한 것이지, 특정 공지의 첨부 위치를 정하는 것이 아니다.
    """
    if not request.scope:
        return True
    hosts = {host.lower() for host in request.scope.allowed_hosts}
    return not hosts or urlsplit(url).netloc.lower() in hosts


def is_allowed(url: str, request: CrawlRequest) -> bool:
    """요청 스코프의 호스트·경로 제한을 적용한다."""
    if not request.scope:
        return True
    if not is_allowed_host(url, request):
        return False
    path = urlsplit(url).path
    return not request.scope.path_prefixes or any(path.startswith(prefix) for prefix in request.scope.path_prefixes)


def _looks_like_pdf(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".pdf")


def _attachment_filename(attachment: Attachment) -> str:
    """다운로드 파일명을 정한다. 힌트가 있으면 우선 쓰고, 없으면 URL 마지막 조각을 쓴다."""
    name = (attachment.name_hint or "").strip()
    if not name:
        name = attachment.url.split("?", 1)[0].rsplit("/", 1)[-1] or "attachment.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name


def html_hash(html: str) -> str:
    """공백 변화에 덜 민감한 본문 HTML 해시."""
    normalized = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _parse_date(value: str) -> datetime | None:
    match = re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", value)
    if match is not None:
        value = match.group(0)
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
