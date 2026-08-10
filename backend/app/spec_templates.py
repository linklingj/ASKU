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

from app.adapter_spec import (
    AdapterSpec,
    DetailSpec,
    FormPagination,
    LinkPagination,
    ListingSpec,
    OffsetPagination,
)

# 첨부 링크는 대부분 확장자로 알아본다.
_FILE_LINKS = "a[href$='.pdf'], a[href$='.hwp'], a[href$='.hwpx'], a[href$='.doc'], a[href$='.docx']"


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


# 학교 하나만 쓰는 규격. 위 템플릿이 **제품별**이라면 이쪽은 **학교별**이다.
#
# 알려진 게시판 제품을 쓰지 않아 손으로 쓴 규격들이다. 여기에 두는 이유는 저장이
# DB 에만 남으면 그 지식이 사람 손에만 존재하기 때문이다 — DB 를 새로 만들거나
# 다른 환경에 올리면 학교들이 전부 공용 폴백으로 떨어진다.
#
# 같은 게시판 제품을 쓰는 학교가 뒤에 또 나오면 그때 `TEMPLATES` 로 올린다.
HOST_SPECS: dict[str, AdapterSpec] = {
    # 서울대. 목록·상세 모두 평범한 표 구조다.
    "www.snu.ac.kr": AdapterSpec(
        host="www.snu.ac.kr",
        listing=ListingSpec(
            row="table tbody tr",
            detail_link="td.col-title a[href]",
            title=[".txt"],
            date=["td.col-date"],
            pagination=OffsetPagination(param="page", step=1, start=1),
        ),
        detail=DetailSpec(
            body=[".board-view", ".content"],
            title=["h3.title", ".board-view h3"],
            attachment=_FILE_LINKS,
        ),
    ),
    # 국민대. 목록이 표가 아니라 `ul > li` 이고 페이지 번호가 1 부터다.
    "www.kookmin.ac.kr": AdapterSpec(
        host="www.kookmin.ac.kr",
        listing=ListingSpec(
            row="div.board_list > ul > li",
            detail_link="a[href*='view.do']",
            title=["p.title"],
            author=[".board_etc span:nth-of-type(2)"],
            date=[".board_etc span:nth-of-type(1)"],
            category=[".ctg_name"],
            pagination=OffsetPagination(param="currentPageNo", step=1, start=1),
        ),
        detail=DetailSpec(
            body=[".view_cont", ".board_view"],
            title=[".board_view .title", ".board_view"],
            attachment=_FILE_LINKS,
        ),
    ),
    # 고려대. 상세 링크가 `href="#1"` 이고 글 번호는 `onclick` 에만 있다.
    "www.korea.ac.kr": AdapterSpec(
        host="www.korea.ac.kr",
        listing=ListingSpec(
            row="table tbody tr",
            detail_link="td.td-title a",
            detail_link_attr="onclick",
            detail_link_pattern=r"jf_view\('([^']+)','([^']+)','([^']+)'",
            detail_link_template="/portalBoard/{2}/{1}/{0}/portalBoardView.do",
            title=["td.td-title a"],
            author=["td.td-write"],
            date=["td.td-date"],
            pagination=OffsetPagination(param="page", step=1, start=1),
        ),
        detail=DetailSpec(
            body=["div.txt", "#content"],
            title=[".title strong"],
            # 첨부는 확장자가 아니라 포털 게시판 링크로 나간다.
            attachment=f"a[href*='/ctt/bb/bulletin'], {_FILE_LINKS}",
        ),
    ),
    # 경희대. 글 번호가 `href="javascript:view('322701','')"` 에 있다.
    "www.khu.ac.kr": AdapterSpec(
        host="www.khu.ac.kr",
        listing=ListingSpec(
            row="table tbody tr",
            detail_link="td.tal a",
            detail_link_pattern=r"view\('(\d+)'",
            detail_link_template="view.do?boardId={0}",
            title=["td.tal a p", "td.tal a"],
            # 작성자·등록일 칸에는 클래스가 없어 자리로 짚는다.
            author=["td:nth-of-type(3)"],
            date=["td:nth-of-type(4)"],
            category=["span.category"],
            pagination=OffsetPagination(param="pageIndex", step=1, start=1),
        ),
        detail=DetailSpec(
            body=["div.row.contents", "div.board02"],
            title=["div.board02 div.tit p.txt06"],
            attachment="div.addFile a",
        ),
    ),
    # 한양대. 표가 아니라 `div` 목록이라 표 기준으로 찾으면 0행으로 보인다.
    # 렌더링은 필요 없다 — 정적 HTML 에 공지 20건이 그대로 들어 있다.
    "www.hanyang.ac.kr": AdapterSpec(
        host="www.hanyang.ac.kr",
        listing=ListingSpec(
            row="div.hyu-list-body-item",
            detail_link="h4 a[href]",
            title=["h4 a"],
            # `span.date` 에는 '/ 2026. 8. 10' 처럼 구분자가 붙는 자리가 있다.
            date=["div.hyu-list-body-item-col span.date", "span.date"],
            # 배지가 셋(주요알림·캠퍼스·분류) 붙는다. 캠퍼스 배지만 클래스가 하나 더 있다.
            category=["span.badge-light:not(.custom-bg)"],
            pagination=OffsetPagination(
                param="_kr_ac_hanyang_noticeBoard_web_portlet_NoticeBoardPortlet_cur", step=1, start=1
            ),
        ),
        detail=DetailSpec(
            # 상세도 목록과 같은 클래스를 재사용한다. 본문 칸만 문단을 직접 갖는다.
            body=["div.hyu-list-body-item-col:has(> p)"],
            title=["div.hyu-list-body-item-col h4"],
            attachment=_FILE_LINKS,
        ),
    ),
    # 중앙대. 목록이 스크립트로 그려져 정적 HTML 에는 공지가 0건이다.
    # 상세는 정적으로 나오므로 목록만 렌더링한다.
    "www.cau.ac.kr": AdapterSpec(
        host="www.cau.ac.kr",
        render="listing",
        listing=ListingSpec(
            row="div.lineList_ul li",
            detail_link="div.txtL a[href*='fn_goDetail']",
            detail_link_pattern=r"fn_goDetail\('(\d+)'",
            detail_link_template=(
                "/cms/FR_CON/BoardView.do?MENU_ID=100&CONTENTS_NO=1&SITE_NO=2"
                "&BOARD_SEQ=4&BBS_SEQ={0}&pageNo=1"
            ),
            title=["div.txtL a"],
            date=["li.date", "span.date"],
            pagination=OffsetPagination(param="pageNo", step=1, start=1),
        ),
        detail=DetailSpec(
            body=["p.view_txt", ".view_cont"],
            # 제목 `dt` 에는 클래스가 없어, 본문을 품은 `dl` 로 범위를 좁힌다.
            title=["dl:has(p.view_txt) dt"],
            attachment=f"div.fileArea a, {_FILE_LINKS}",
        ),
    ),
}


def host_spec(host: str) -> AdapterSpec | None:
    """이 학교만 쓰는 규격. 없으면 None 이고 템플릿 대조로 넘어간다."""

    return HOST_SPECS.get(host.lower())
