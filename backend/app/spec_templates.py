"""알려진 게시판 제품의 수집 규격 템플릿.

국내 대학 상당수가 같은 게시판 제품을 쓴다. 지원 중인 네 학교 중 셋이 K2Web
계열이고 아주대도 같다. 이런 학교에 LLM 을 부르는 것은 낭비다 — 규격을 이미
알고 있으므로 대보고 통과하면 그대로 쓰면 된다.

템플릿은 **후보**일 뿐이다. 채택 여부는 자동 생성 규격과 **같은 검증기**로
판정한다. 통과 기준이 달라지면 어느 쪽이 나은지 비교할 수 없다.

같은 제품이라도 학교마다 채우는 자리가 달라, 선택자를 여러 개 나열해 흡수한다.
아주대는 `.b-date` 요소가 모바일용이라 비어 있고 실제 날짜는 마지막 칸에 있다.
"""

from __future__ import annotations

from app.adapter_spec import AdapterSpec, DetailSpec, FormPagination, LinkPagination, ListingSpec


# K2Web 계열(세종대·홍익대·아주대 등). `b-` 접두 클래스가 특징이다.
K2WEB = AdapterSpec(
    host="",  # 적용할 때 호스트를 채운다
    listing=ListingSpec(
        row="table tbody tr",
        detail_link=".b-title-box a[href*='mode=view'][href*='articleNo']",
        title=[".b-title"],
        author=[".b-writer", "td:nth-last-of-type(2)"],
        date=[".b-date", "td:last-of-type"],
        category=[".b-mini-cate", ".b-cate"],
        pagination=LinkPagination(selector="li.next a[href], a[title='다음 페이지로 이동하기']"),
    ),
    detail=DetailSpec(
        body=[".b-content-box .fr-view", ".b-content-box"],
        title=[".b-title"],
        attachment="a[href$='.pdf'], a[href$='.hwp'], a[href$='.hwpx'], a[href$='.doc'], a[href$='.docx']",
    ),
)


# `artclView.do` 계열(연세대·건국대 등). 상세 URL 과 페이지 이동 방식이 같고,
# 목록 마크업만 학교마다 표 또는 `ul > li` 로 갈린다.
ARTCLVIEW = AdapterSpec(
    host="",
    listing=ListingSpec(
        row=[".boardWrap > ul > li", "table tbody tr"],
        detail_link="a[href*='artclView.do']",
        title=[".title", ".td-subject a", "td.td-subject"],
        author=[".etc-area", ".td-write"],
        date=[".date-area", ".td-date"],
        category=[".notice-title", ".td-num"],
        # 목록 번호 칸이 분류 자리에 걸리는 학교가 있다. 숫자·'공지'는 분류가 아니다.
        category_ignore=r"\d+|공지",
        pagination=FormPagination(
            form_selector="form[name='pageForm'][action]",
            next_selector="._paging ._listNext[href]",
            page_pattern=r"page_link\('(\d+)'\)",
        ),
    ),
    detail=DetailSpec(
        body=[".artclView", ".view-con", ".board-view .txt", "#board-view"],
        title=[".artclViewTitle", ".view-title", ".title"],
        attachment="a[href$='.pdf'], a[href$='.hwp'], a[href$='.hwpx'], a[href$='.doc'], a[href$='.docx']",
    ),
)


# 시도 순서. 구체적인 것을 먼저 두어, 느슨한 템플릿이 먼저 통과해 버리지 않게 한다.
TEMPLATES: tuple[tuple[str, AdapterSpec], ...] = (("k2web", K2WEB), ("artclview", ARTCLVIEW))


def templates_for(host: str) -> list[tuple[str, AdapterSpec]]:
    """호스트에 적용할 템플릿 후보를 만든다."""

    return [(name, spec.model_copy(update={"host": host.lower()})) for name, spec in TEMPLATES]
