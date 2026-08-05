"""HTML 구조 축약.

게시판 페이지에서 **선택자를 만들 재료**만 남기고 부피를 줄인다. 목록에 공지가
10건이든 100건이든 구조를 파악하는 데는 몇 건이면 충분하고, 나머지는 토큰만
차지한다. 실측 기준 원본의 15~35%(28~47KB, 약 15~25K 토큰)로 줄어든다.

부피를 한계까지 줄이지는 않는다. 축약본은 학교 등록 시 한 번 쓰는 입력이라 토큰
몇 천을 아끼는 이득보다, 덜 남겨서 구조를 잃는 손해가 훨씬 크다.

용도는 둘이다.

1. 규격 자동 생성(예정) — LLM 에 넣을 입력. 원본을 그대로 넣으면 학교 하나
   등록에 수십만 토큰이 든다.
2. 개발용 — 새 학교의 게시판 구조를 눈으로 파악할 때

**구조를 지우지 않는 것이 원칙이다.** 부피를 줄이자고 형제 요소를 한 종류만
남기면, 세종대처럼 고정공지(`tr.b-top-box`)와 일반공지(`tr`)의 클래스가 다른
경우 그 차이가 사라진다. 이번 수집 범위 이슈의 원인이 정확히 그 차이였다.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


# 구조 파악에 쓸모가 없고 부피만 큰 태그.
DROP_TAGS = ("script", "style", "svg", "link", "meta", "noscript", "iframe", "img")
# 선택자를 만들 재료. 나머지 속성(style·onclick·data-*)은 버린다.
# name·value·action 은 폼 기반 페이지 이동에 필요하다. 연세대는 `form[name=pageForm]`
# 의 hidden input 을 조합해 다음 목록 URL 을 만들므로 이들이 없으면 규칙을 세울 수 없다.
KEEP_ATTRIBUTES = (
    "class", "id", "href", "src", "rel", "title", "role",
    "name", "value", "type", "action", "method",
)
# 같은 모양의 형제를 몇 개까지 남길지. 1개만 남기면 반복 구조인지 알 수 없다.
# 넉넉히 잡는다. 축약본은 학교 등록 시 한 번 쓰는 입력이라 토큰 몇 천은 문제가
# 아닌 반면, 덜 남겨서 구조를 잃으면 규격 자체가 틀어진다.
DEFAULT_SIBLING_SAMPLES = 3
# 링크 목록으로 접을 영역. 게시판 구조와 무관하지만 부피가 크다(연세대 메뉴 32KB).
# 통째로 버리지는 않는다. 세종대의 하위 게시판 탭이 메뉴 안에 있어, 링크를 잃으면
# 게시판 목록을 찾을 수 없다.
LINK_ONLY_TAGS = ("nav", "header", "footer", "aside")
# 접은 영역에서 남길 링크 수.
DEFAULT_LINK_SAMPLES = 60
# 링크 목록에 남길 URL 수.
DEFAULT_INVENTORY_LINKS = 200
# URL 패턴(숫자를 뺀 형태)마다 남길 표본 수. 상세 공지 링크 수백 개를 한 패턴으로
# 접으면서, 세종대 하위 게시판(`notice1~10.do`) 처럼 개수가 적은 묶음은 다 남긴다.
PATTERN_SAMPLES = 12
# 반복 접기에서 제외하는 잎 요소. 부피는 작지만 링크·페이지네이션의 핵심 재료다.
# 접으면 세종대 탭 링크가 잘리고, 연세대 페이지 이동 폼의 hidden input 이 사라져
# 다음 목록 URL 을 만들 수 없다.
NEVER_COLLAPSE_TAGS = ("a", "input", "option", "button")
# 페이지네이션 영역. 접으면 '다음/마지막' 링크가 잘려 페이지 이동 규칙을 알 수 없다.
# 클래스·id 에 이 조각이 들어가면 통째로 보존한다.
PAGING_HINTS = ("paging", "pagination", "pager", "page-nav")
# 텍스트 노드를 자르는 길이. 선택자에는 영향이 없지만, 제목이 통째로 잘리면
# 목록과 상세를 대조하는 판단이 어려워져 여유를 둔다.
DEFAULT_TEXT_CHARS = 80


def digest_html(
    html: str,
    *,
    sibling_samples: int = DEFAULT_SIBLING_SAMPLES,
    text_chars: int = DEFAULT_TEXT_CHARS,
    link_samples: int = DEFAULT_LINK_SAMPLES,
) -> str:
    """구조는 유지한 채 부피만 줄인 HTML 을 만든다."""

    original = BeautifulSoup(html, "html.parser")
    soup = BeautifulSoup(html, "html.parser")
    _drop_noise(soup)
    _strip_attributes(soup)
    _flatten_navigation(soup, link_samples)
    _collapse_repeats(soup, sibling_samples)
    _append_link_inventory(soup, original, DEFAULT_INVENTORY_LINKS)
    _truncate_text(soup, text_chars)
    return _pretty(soup)


def _drop_noise(soup: BeautifulSoup) -> None:
    for node in soup.find_all(DROP_TAGS):
        node.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()


def _strip_attributes(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        tag.attrs = {key: value for key, value in tag.attrs.items() if key in KEEP_ATTRIBUTES}


def _flatten_navigation(soup: BeautifulSoup, link_samples: int) -> None:
    """메뉴·머리말·꼬리말을 링크 목록으로 접는다.

    게시판 파싱에는 이 영역의 중첩 구조가 필요 없지만, 링크 자체는 하위 게시판
    탭을 찾는 단서라 남긴다.
    """

    for region in soup.find_all(LINK_ONLY_TAGS):
        links = region.find_all("a", href=True)
        if len(links) < 3:
            continue  # 링크가 몇 개 없으면 접어도 얻는 게 없다
        replacement = soup.new_tag(region.name)
        replacement.attrs = dict(region.attrs)
        for link in links[:link_samples]:
            anchor = soup.new_tag("a", href=link["href"])
            anchor.string = " ".join(link.get_text(" ", strip=True).split())
            replacement.append(anchor)
        if len(links) > link_samples:
            replacement.append(Comment(f" 링크 {len(links) - link_samples}개 생략 "))
        region.replace_with(replacement)


def _collapse_repeats(soup: BeautifulSoup, samples: int) -> None:
    """같은 모양의 형제 요소를 표본 몇 개만 남기고 접는다.

    "같은 모양"은 `태그명 + class 조합`으로 판단한다. 클래스가 다르면 다른 종류로
    보고 각각 표본을 남긴다 — 고정공지와 일반공지처럼 한쪽만 남으면 안 되는
    경우가 실제로 있다.
    """

    for parent in soup.find_all(True):
        if getattr(parent, "attrs", None) is None:  # 앞서 접히며 사라진 노드
            continue
        if _in_paging_region(parent):
            continue
        children = [node for node in parent.children if isinstance(node, Tag)]
        # 같은 클래스의 형제 중 가장 큰 것은 표본 수와 무관하게 남긴다. 페이지를
        # 같은 위젯 컨테이너로 나열하는 사이트가 있어(연세대 `div._obj _objWidget`),
        # 앞에서부터 세면 정작 게시판이 든 컨테이너가 통째로 잘린다.
        largest: dict[tuple[str, str], Tag] = {}
        for child in children:
            key = (child.name, " ".join(sorted(child.get("class") or [])))
            if key not in largest or len(str(child)) > len(str(largest[key])):
                largest[key] = child

        seen: dict[tuple[str, str], int] = {}
        dropped: dict[tuple[str, str], int] = {}
        for child in children:
            if child.name in NEVER_COLLAPSE_TAGS:
                continue
            key = (child.name, " ".join(sorted(child.get("class") or [])))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > samples and child is not largest.get(key):
                dropped[key] = dropped.get(key, 0) + 1
                child.decompose()
        for (name, classes), count in dropped.items():
            label = f"{name}.{classes}" if classes else name
            parent.append(Comment(f" 같은 구조 {label} {count}개 생략 "))


def _in_paging_region(tag: Tag) -> bool:
    """자신이나 조상이 페이지네이션 영역인가."""

    for node in [tag, *tag.parents]:
        # decompose 된 노드는 attrs 가 None 이라 조회 전에 걸러낸다.
        if not isinstance(node, Tag) or getattr(node, "attrs", None) is None:
            continue
        marker = " ".join([*(node.get("class") or []), node.get("id") or ""]).lower()
        if any(hint in marker for hint in PAGING_HINTS):
            return True
    return False


def _append_link_inventory(soup: BeautifulSoup, original: BeautifulSoup, limit: int) -> None:
    """접히면서 사라진 링크를 URL 목록으로 되살린다.

    구조를 접으면 그 안의 링크도 함께 사라진다. 게시판 탭처럼 링크 자체가 단서인
    경우(세종대 `notice1~10.do`)를 잃지 않도록, 원본의 링크를 경로 기준으로
    중복 제거해 목록으로 붙인다. 구조는 담지 않으므로 부피가 작다.
    """

    # URL 을 숫자만 다른 것끼리 묶어 패턴별로 표본을 남긴다. 상세 공지 링크 수백 개는
    # 한 패턴으로 접히고, 하위 게시판(`notice1~10.do`)은 같은 패턴이라도 개수가 적어
    # 모두 남는다.
    by_pattern: dict[str, dict[str, str]] = {}
    for link in original.find_all("a", href=True):
        href = str(link["href"]).strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        pattern = re.sub(r"\d+", "#", href)
        bucket = by_pattern.setdefault(pattern, {})
        if len(bucket) < PATTERN_SAMPLES:
            bucket.setdefault(href, " ".join(link.get_text(" ", strip=True).split())[:40])

    seen = {href: label for bucket in by_pattern.values() for href, label in bucket.items()}
    if not seen:
        return

    section = soup.new_tag("div", id="asku-link-inventory")
    section.append(Comment(" 접힌 구조 안의 링크 목록(원본 기준, 중복 제거) "))
    for href, label in list(seen.items())[:limit]:
        anchor = soup.new_tag("a", href=href)
        anchor.string = label
        section.append(anchor)
    if len(seen) > limit:
        section.append(Comment(f" 링크 {len(seen) - limit}개 생략 "))
    (soup.body or soup).append(section)


def _truncate_text(soup: BeautifulSoup, limit: int) -> None:
    for text in list(soup.find_all(string=True)):
        if isinstance(text, Comment):
            continue
        squeezed = " ".join(str(text).split())
        if not squeezed:
            text.extract()
            continue
        text.replace_with(NavigableString(squeezed[:limit] + ("…" if len(squeezed) > limit else "")))


def _pretty(soup: BeautifulSoup) -> str:
    """빈 줄을 없앤 들여쓰기 출력. 사람이 읽고 LLM 이 파싱하기 좋은 형태."""

    return "\n".join(line for line in soup.prettify().splitlines() if line.strip())
