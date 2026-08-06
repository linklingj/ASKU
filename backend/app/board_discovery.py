"""하위 게시판(탭) 자동 발견.

한 학교의 공지가 `일반공지 / 장학 / 채용` 처럼 여러 게시판으로 나뉜 경우가 흔하다.
등록한 URL 하나만 돌면 나머지를 통째로 놓친다.

**라벨로 후보를 넓게 모으고, 실제로 긁어 보고 채택한다.** 라벨만 믿으면 '인공지능
대학'(공지가 우연히 들어감)이나 '자유게시판'까지 들어오고, URL 패턴만 믿으면
세종대(`notice1~10.do`)처럼 규칙적인 학교 말고는 찾지 못한다. 실제로 목록이
읽히는지 보는 것이 가장 확실하다.

LLM 을 쓰지 않는다. 판정은 수집 규격과 검증기가 한다.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.crawler import Board, NoticeAdapter, normalize_url
from app.validation import MIN_TITLE_RATIO, validate_listing


LOGGER = logging.getLogger(__name__)

# 게시판 링크로 볼 라벨. 넓게 잡고 실제 수집으로 걸러낸다.
BOARD_LABEL = re.compile(r"공지|알림|소식|공고|게시판")
# 라벨에 이 말이 들어가면 공지 게시판이 아니다. 실제 수집에서도 대개 걸리지만,
# 미리 빼면 남의 서버에 보내는 요청이 준다.
LABEL_EXCLUDE = re.compile(r"자유게시판|소식지|신문|웹진|Q&A|문의|FAQ|자료실|갤러리")
# 라벨이 길면 게시판 이름이 아니라 공지 제목일 가능성이 크다.
MAX_LABEL_CHARS = 16
# 확인할 후보 수 상한. 후보마다 요청이 한 번 나간다.
DEFAULT_MAX_CANDIDATES = 12
# 게시판으로 인정할 최소 행 수. 한두 줄짜리는 목록이 아니라 안내 페이지다.
MIN_BOARD_ROWS = 3


def find_boards(
    listing_html: str,
    base_url: str,
    adapter: NoticeAdapter,
    fetch,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> tuple[Board, ...]:
    """같은 학교의 다른 공지 게시판을 찾는다. 기준 URL 은 항상 첫 번째로 둔다.

    `fetch(url)` 은 호출자가 넘긴다. robots·요청 간격·User-Agent 같은 정책을
    Crawler 가 계속 소유하게 하기 위해서다.
    """

    boards = [Board(base_url, _own_label(listing_html, base_url))]
    # 기준 게시판의 지문도 넣는다. 정렬·필터 파라미터를 붙여 자기 자신을 다시
    # 가리키는 링크가 흔하다(서울대 `?sc=y`).
    seen = {normalize_url(base_url), _listing_fingerprint(listing_html, base_url, adapter)}
    for url, label in _candidates(listing_html, base_url, adapter, max_candidates):
        canonical = normalize_url(url)
        if canonical in seen:
            continue  # 같은 게시판을 정렬·필터 파라미터만 달리해 가리키는 링크
        html = fetch(url)
        if html is None:
            continue
        if not _looks_like_board(html, url, adapter):
            LOGGER.debug("게시판이 아님(목록이 읽히지 않음): %s (%s)", url, label)
            continue
        # 목록 내용이 같으면 같은 게시판이다. 서울대는 `?sc=y` 를 붙인 링크를
        # 따로 두는데, 그대로 받아들이면 같은 게시판을 두 번 돌게 된다.
        fingerprint = _listing_fingerprint(html, url, adapter)
        if fingerprint in seen:
            continue
        seen.add(canonical)
        seen.add(fingerprint)
        boards.append(Board(url, label))
    return tuple(boards)


def _listing_fingerprint(html: str, url: str, adapter: NoticeAdapter) -> str:
    """목록의 첫 상세 링크들. 같은 게시판인지 판별하는 지문으로 쓴다."""

    try:
        items = list(adapter.parse_listing(html, url))[:3]
    except Exception:
        return url
    return "|".join(normalize_url(item.url) for item in items)


def _candidates(listing_html: str, base_url: str, adapter: NoticeAdapter, limit: int) -> list[tuple[str, str]]:
    """라벨로 게시판 후보를 모은다. 같은 호스트만 본다."""

    soup = BeautifulSoup(listing_html, "html.parser")
    host = urlsplit(base_url).hostname
    current = base_url.split("#")[0]
    links = soup.select("a[href]")
    detail_patterns = _detail_link_patterns(listing_html, base_url, adapter)
    found: dict[str, str] = {}

    for link in links:
        label = " ".join(link.get_text(" ", strip=True).split())
        if not label or len(label) > MAX_LABEL_CHARS:
            continue
        if not BOARD_LABEL.search(label) or LABEL_EXCLUDE.search(label):
            continue
        href = str(link["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlsplit(url).hostname != host or url == current:
            continue
        if _numeric_pattern(url) in detail_patterns:
            continue  # 공지 제목이 '공지'로 시작해 후보로 새어 들어온 경우
        found.setdefault(url, label)
    return list(found.items())[:limit]


def _detail_link_patterns(listing_html: str, base_url: str, adapter: NoticeAdapter) -> set[str]:
    """상세 공지 링크의 URL 형태를 어댑터로 직접 알아낸다.

    제목에 '공지' 가 들어간 공지가 흔해, 라벨만 보면 공지 제목이 게시판 후보로
    새어 들어온다.

    링크 반복 횟수로 판단하면 안 된다. 세종대 하위 게시판(`notice1~10.do`)도 같은
    형태가 열 번 나와, 진짜 탭까지 상세 링크로 오인하게 된다. 어댑터가 목록에서
    뽑아낸 링크만 상세로 본다.
    """

    try:
        items = list(adapter.parse_listing(listing_html, base_url))
    except Exception:  # 어댑터가 이 페이지와 맞지 않으면 걸러낼 것도 없다
        return set()
    return {_numeric_pattern(item.url) for item in items}


def _numeric_pattern(url: str) -> str:
    return re.sub(r"\d+", "#", url.split("#")[0])


def _looks_like_board(html: str, url: str, adapter: NoticeAdapter) -> bool:
    """이 페이지가 공지 목록인지 실제 파싱으로 판정한다.

    규격·어댑터가 목록으로 읽어내지 못하면 수집해도 얻을 것이 없다. 자유게시판·
    소식지·학과 소개 페이지가 여기서 걸러진다.
    """

    report = validate_listing(adapter, html, url)
    return report.listing_rows >= MIN_BOARD_ROWS and report.title_ratio >= MIN_TITLE_RATIO


def _own_label(listing_html: str, base_url: str) -> str | None:
    """기준 게시판의 이름. 메뉴에서 현재 페이지를 가리키는 링크의 텍스트를 쓴다."""

    soup = BeautifulSoup(listing_html, "html.parser")
    current = base_url.split("#")[0]
    for link in soup.select("a[href]"):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        if urljoin(base_url, href).split("#")[0] != current:
            continue
        label = " ".join(link.get_text(" ", strip=True).split())
        if label and len(label) <= MAX_LABEL_CHARS and BOARD_LABEL.search(label):
            return label
    return None
