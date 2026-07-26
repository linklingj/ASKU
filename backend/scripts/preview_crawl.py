"""학교별 공지 수집 결과를 터미널에서 확인하는 개발용 명령.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/preview_crawl.py hongik
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from app.crawler import (
    Crawler,
    HongikNoticeAdapter,
    NoticeAdapter,
    SejongNoticeAdapter,
    SkkuNoticeAdapter,
    YonseiNoticeAdapter,
)
from app.schemas import CrawlRequest, CrawlScope


@dataclass(frozen=True)
class SchoolPreview:
    name: str
    base_url: str
    allowed_hosts: list[str]
    path_prefixes: list[str]
    adapter_factory: Callable[[], NoticeAdapter]


SCHOOLS: dict[str, SchoolPreview] = {
    "yonsei": SchoolPreview(
        name="연세대",
        base_url="https://www.yonsei.ac.kr/sc/254/subview.do",
        allowed_hosts=["www.yonsei.ac.kr"],
        path_prefixes=["/sc/", "/bbs/sc/"],
        adapter_factory=YonseiNoticeAdapter,
    ),
    "sejong": SchoolPreview(
        name="세종대",
        base_url="https://www.sejong.ac.kr/kor/intro/notice1.do",
        allowed_hosts=["www.sejong.ac.kr"],
        path_prefixes=["/kor/intro/"],
        adapter_factory=SejongNoticeAdapter,
    ),
    "hongik": SchoolPreview(
        name="홍익대",
        base_url="https://www.hongik.ac.kr/kr/newscenter/notice.do",
        allowed_hosts=["www.hongik.ac.kr"],
        path_prefixes=["/kr/newscenter/"],
        adapter_factory=HongikNoticeAdapter,
    ),
    "skku": SchoolPreview(
        name="성균관대",
        base_url="https://www.skku.edu/skku/campus/skk_comm/notice02.do",
        allowed_hosts=["www.skku.edu"],
        path_prefixes=["/skku/campus/skk_comm/"],
        adapter_factory=SkkuNoticeAdapter,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASKU 공지 수집 미리보기")
    parser.add_argument("school", choices=sorted(SCHOOLS), help="확인할 학교")
    parser.add_argument("--max-items", type=int, default=5, help="표시할 최대 공지 수 (기본값: 5)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_items < 1 or args.max_items > 30:
        raise SystemExit("--max-items는 1~30 사이여야 합니다.")

    school = SCHOOLS[args.school]
    request = CrawlRequest(
        crawl_id=uuid4(),
        school_id=0,
        base_url=school.base_url,
        mode="initial",
        scope=CrawlScope(
            allowed_hosts=school.allowed_hosts,
            path_prefixes=school.path_prefixes,
            max_listing_pages=1,
            max_items=args.max_items,
        ),
    )
    # 미리보기는 저장소에 쓰지 않는다. 모든 페이지를 새 수집 결과로만 표시한다.
    run = Crawler(hash_exists=lambda *_args: False).crawl(request, school.adapter_factory())

    print(f"학교: {school.name}")
    print(f"수집: {len(run.pages)}건 / 실패: {len(run.failures)}건")
    for index, page in enumerate(run.pages, start=1):
        category = f"[{page.category_hint}] " if page.category_hint else ""
        print(f"\n{index}. {category}{page.title_hint or '(제목 없음)'}")
        print(f"   작성: {page.author_hint or '-'} / 날짜: {page.published_at_hint or '-'}")
        print(f"   원문: {page.canonical_url}")
    for failure in run.failures:
        print(f"\n실패: {failure.stage} {failure.error_code} ({failure.source_url})")


if __name__ == "__main__":
    main()
