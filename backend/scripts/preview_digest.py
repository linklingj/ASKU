"""게시판 페이지를 축약해 구조만 보는 개발용 명령.

새 학교의 목록·상세가 어떤 마크업인지 파악할 때 쓴다. 원본은 15만 자가 넘어
그대로 열어 보기 어렵다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/preview_digest.py hongik
    PYTHONPATH=backend python3 backend/scripts/preview_digest.py hongik --detail
    PYTHONPATH=backend python3 backend/scripts/preview_digest.py --url https://... -o out.html
"""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit
from uuid import uuid4

from app.crawler import CrawlRun, Crawler, adapter_for
from app.html_digest import digest_html
from app.schemas import CrawlRequest, CrawlScope


SCHOOLS: dict[str, str] = {
    "yonsei": "https://www.yonsei.ac.kr/sc/254/subview.do",
    "sejong": "https://www.sejong.ac.kr/kor/intro/notice1.do",
    "hongik": "https://www.hongik.ac.kr/kr/newscenter/notice.do",
    "skku": "https://www.skku.edu/skku/campus/skk_comm/notice02.do",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="게시판 HTML 구조 축약 미리보기")
    parser.add_argument("school", nargs="?", choices=sorted(SCHOOLS), help="등록된 학교")
    parser.add_argument("--url", help="임의의 목록 URL (학교 대신 지정)")
    parser.add_argument("--detail", action="store_true", help="목록 대신 첫 상세 공지를 축약한다")
    parser.add_argument("-o", "--output", help="결과를 저장할 파일 (기본: 표준 출력)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    listing_url = args.url or (SCHOOLS.get(args.school) if args.school else None)
    if not listing_url:
        raise SystemExit("학교를 지정하거나 --url 을 쓰세요.")

    crawler = Crawler(hash_exists=lambda *_args: False)
    request = CrawlRequest(
        crawl_id=uuid4(),
        school_id=0,
        base_url=listing_url,
        mode="initial",
        scope=CrawlScope(allowed_hosts=[urlsplit(listing_url).hostname or ""]),
    )
    run = CrawlRun()

    if not crawler.robots_allowed(listing_url):
        raise SystemExit(f"robots.txt 가 수집을 허용하지 않습니다: {listing_url}")
    html = crawler._fetch(request, listing_url, run)  # noqa: SLF001 — 개발용 스크립트
    if html is None:
        raise SystemExit(f"목록을 받지 못했습니다: {listing_url}")
    target = listing_url

    if args.detail:
        items = list(adapter_for(listing_url).parse_listing(html, listing_url))
        if not items:
            raise SystemExit("목록에서 상세 링크를 찾지 못했습니다. 어댑터를 확인하세요.")
        target = items[0].url
        if not crawler.robots_allowed(target):
            raise SystemExit(f"robots.txt 가 수집을 허용하지 않습니다: {target}")
        html = crawler._fetch(request, target, run, referer=listing_url)  # noqa: SLF001
        if html is None:
            raise SystemExit(f"상세 페이지를 받지 못했습니다: {target}")

    digest = digest_html(html)
    summary = f"{target}\n원본 {len(html):,}자 → 축약 {len(digest):,}자 ({len(digest) / len(html):.1%})"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(digest)
        print(f"{summary}\n저장: {args.output}")
    else:
        print(f"<!-- {summary} -->")
        print(digest)


if __name__ == "__main__":
    main()
