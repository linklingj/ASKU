"""학교별 수집 규격.

학교마다 파이썬 어댑터를 짜는 대신, 선택자와 페이지네이션 방식을 **데이터로**
적어 둔다. 코드는 사람만 쓸 수 있지만 데이터는 자동 생성할 수 있어, 나중에
규격을 LLM 이 만들어도 실행 경로는 그대로다.

규격 하나가 크롤러(목록)와 Extractor(본문)를 함께 덮는다. 같은 HTML 을 보고
얻는 정보라 나누면 학교마다 두 번 분석하게 된다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _as_selector_list(value: str | list[str] | None) -> list[str]:
    """선택자를 목록으로 정규화한다. 문자열 하나도 받아들인다."""

    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


# 선택자 후보 목록. JSON 에는 문자열 하나로 적어도 되고 여러 개를 나열해도 된다.
# 규격을 사람이 손으로 쓰는 경우가 많아 단순한 형태를 계속 허용한다.
SelectorList = Annotated[list[str], BeforeValidator(_as_selector_list)]


class BoardSpec(BaseModel):
    """수집할 게시판 하나. 공지가 탭으로 쪼개진 학교는 여러 개가 된다."""

    url: str
    label: str | None = None


class LinkPagination(BaseModel):
    """'다음' 링크의 href 를 따라간다. 가장 흔한 방식."""

    type: Literal["link"] = "link"
    selector: str


class OffsetPagination(BaseModel):
    """URL 파라미터를 일정하게 늘린다.

    '다음' 링크가 없거나 JavaScript 로만 동작하는 게시판의 폴백이다. 마지막
    페이지를 넘어가도 같은 목록을 돌려주는 사이트가 있어, 크롤러의 중복 URL·
    신규 0건 판정이 종료 조건 역할을 한다.
    """

    type: Literal["offset"] = "offset"
    param: str
    step: int = Field(default=10, ge=1)
    # 파라미터가 URL 에 없을 때의 현재 값. 게시판마다 기준이 다르다 — 건너뛴 글
    # 수를 세는 방식(`article.offset`)은 0 에서 시작하고, 페이지 번호를 쓰는 방식
    # (`currentPageNo`)은 1 에서 시작한다. 0 으로 두면 후자에서 1페이지를 다시
    # 요청해 제자리에 머문다.
    start: int = 0


class FormPagination(BaseModel):
    """폼의 hidden input 을 모아 다음 페이지 URL 을 만든다.

    연세대 K2Web 이 이 방식이다. 페이지 번호는 '다음' 링크의 JavaScript 호출
    (`page_link('2')`)에서 뽑는다.
    """

    type: Literal["form"] = "form"
    form_selector: str
    next_selector: str
    page_param: str = "page"
    # 다음 링크의 href 에서 페이지 번호를 뽑는 정규식. 그룹 하나를 반드시 포함한다.
    page_pattern: str = r"\((?:'|\")(\d+)(?:'|\")\)"


Pagination = LinkPagination | OffsetPagination | FormPagination


class ListingSpec(BaseModel):
    """목록 페이지에서 공지 한 건을 읽는 방법.

    `row` 는 공지 한 건에 해당하는 요소, 나머지는 그 안에서 찾을 선택자다.
    `detail_link` 만 필수이고 메타데이터는 게시판이 제공하지 않으면 비운다.
    """

    # 행 선택자도 후보를 나열할 수 있다. **행이 하나라도 잡히는 첫 후보**를 쓴다.
    # 같은 게시판 제품이라도 학교가 목록을 표로 그리기도 하고 `ul > li` 로 그리기도
    # 한다(건국대 대 연세대). 후보를 두면 템플릿 하나로 둘 다 덮는다.
    row: SelectorList
    detail_link: str
    # 메타데이터 선택자는 여러 개를 두고 **값이 나올 때까지** 앞에서부터 시도한다.
    # 같은 게시판 제품을 써도 학교마다 채우는 자리가 다르다 — 아주대는 `.b-date`
    # 요소가 모바일용이라 비어 있고 실제 날짜는 마지막 칸에 있는 반면, 세종대는
    # `.b-date` 에 값이 있다. 후보를 나열하면 규격 하나로 둘 다 덮는다.
    title: SelectorList = Field(default_factory=list)
    author: SelectorList = Field(default_factory=list)
    date: SelectorList = Field(default_factory=list)
    category: SelectorList = Field(default_factory=list)
    # 분류 칸에 분류가 아닌 값이 들어오는 게시판을 위한 제외 패턴(정규식).
    # 성균관대는 같은 자리에 고정공지면 '공지', 일반 글이면 'No.2149' 를 넣는다.
    # 이를 거르지 않으면 글 번호가 분류로 저장돼 검색 결과에 섞인다.
    category_ignore: str | None = None
    pagination: Pagination | None = Field(default=None, discriminator="type")


class DetailSpec(BaseModel):
    """상세 페이지에서 본문을 읽는 방법.

    `body` 는 여러 개를 두고 앞에서부터 시도한다. 사이트가 게시판 종류에 따라
    다른 컨테이너를 쓰는 경우가 있다.
    """

    body: SelectorList = Field(default_factory=list)
    title: SelectorList = Field(default_factory=list)
    attachment: str | None = None


class AdapterSpec(BaseModel):
    """호스트 하나의 수집 규격."""

    host: str
    boards: list[BoardSpec] = Field(default_factory=list)
    listing: ListingSpec
    detail: DetailSpec = Field(default_factory=DetailSpec)
    # 규격 출처. 자동 생성한 규격과 사람이 쓴 규격을 구분해 추적한다.
    source: Literal["human", "generated"] = "human"
    version: int = 1
