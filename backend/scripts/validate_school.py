"""등록된 학교의 수집 품질을 점검하는 개발용 명령.

목록과 상세 몇 건을 실제로 받아 파서가 동작하는지 판정한다. 학교를 추가하거나
파서를 고친 뒤, 사람이 페이지를 열어보는 대신 이걸 돌린다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/validate_school.py hongik
    PYTHONPATH=backend python3 backend/scripts/validate_school.py --all
"""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit
from uuid import uuid4

from app.crawler import Crawler, CrawlRun, adapter_for, boards_for
from app.extractor import content_parser_for
from app.schemas import CrawlRequest, CrawlScope
from app.validation import ValidationReport, validate_detail, validate_listing


SCHOOLS: dict[str, str] = {
    "yonsei": "https://www.yonsei.ac.kr/sc/254/subview.do",
    "sejong": "https://www.sejong.ac.kr/kor/intro/notice1.do",
    "hongik": "https://www.hongik.ac.kr/kr/newscenter/notice.do",
    "skku": "https://www.skku.edu/skku/campus/skk_comm/notice02.do",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학교별 수집 품질 검증")
    parser.add_argument("school", nargs="?", choices=sorted(SCHOOLS), help="점검할 학교")
    parser.add_argument("--all", action="store_true", help="등록된 학교 전체를 점검한다")
    parser.add_argument("--details", type=int, default=2, help="게시판마다 확인할 상세 공지 수 (기본 2)")
    parser.add_argument("--boards", type=int, default=1, help="확인할 게시판 수 (기본 1, 0이면 전체)")
    return parser.parse_args()


def validate_school(name: str, base_url: str, *, detail_count: int, board_count: int) -> list[ValidationReport]:
    """게시판마다 목록 1페이지와 상세 몇 건만 받아 판정한다.

    운영 크롤과 달리 페이지네이션을 따라가지 않는다. 검증에 필요한 것은 파서가
    구조와 맞는지이지 수집량이 아니며, 남의 서버를 덜 두드리는 편이 낫다.
    """

    adapter = adapter_for(base_url)
    boards = boards_for(base_url)
    if board_count:
        boards = boards[:board_count]
    # 검증은 저장소에 쓰지 않는다. 모든 페이지를 새 수집으로 취급한다.
    crawler = Crawler(hash_exists=lambda *_args: False)
    request = CrawlRequest(
        crawl_id=uuid4(),
        school_id=0,
        base_url=base_url,
        mode="initial",
        scope=CrawlScope(allowed_hosts=[_host(base_url)]),
    )
    run = CrawlRun()
    reports: list[ValidationReport] = []

    for board in boards:
        report = ValidationReport(target=f"{name}/{board.label or '기본'}")
        listing_html = _fetch(crawler, request, board.url, run, report)
        if listing_html is None:
            reports.append(report)
            continue

        validate_listing(adapter, listing_html, board.url, report=report)
        items = list(adapter.parse_listing(listing_html, board.url))
        titles = [item.title_hint or "" for item in items]

        for item in items[:detail_count]:
            html = _fetch(crawler, request, item.url, run, report, referer=board.url)
            if html is None:
                continue
            document = content_parser_for(item.url).parse(html)
            others = [title for title in titles if title and title != item.title_hint]
            validate_detail(document, item, other_titles=others, report=report)
        reports.append(report)
    return reports


def _fetch(
    crawler: Crawler,
    request: CrawlRequest,
    url: str,
    run: CrawlRun,
    report: ValidationReport,
    *,
    referer: str | None = None,
) -> str | None:
    """운영 크롤과 같은 정책(robots·요청 간격·User-Agent)으로 한 페이지를 받는다."""

    if not crawler.robots_allowed(url):
        report.add("ROBOTS_DISALLOWED", f"robots.txt 가 막았다: {url}")
        return None
    before = len(run.failures)
    html = crawler._fetch(request, url, run, referer=referer)  # noqa: SLF001 — 개발용 스크립트
    if html is None:
        code = run.failures[-1].error_code if len(run.failures) > before else "FETCH_FAILED"
        report.add(code, f"페이지를 받지 못했다: {url}")
    return html


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def main() -> None:
    args = parse_args()
    if not args.school and not args.all:
        raise SystemExit("학교를 지정하거나 --all 을 쓰세요.")
    targets = SCHOOLS if args.all else {args.school: SCHOOLS[args.school]}

    failed = 0
    for name, base_url in targets.items():
        for report in validate_school(
            name, base_url, detail_count=args.details, board_count=args.boards
        ):
            print(report.summary())
            for finding in report.findings:
                print(f"    - {finding}")
            failed += 0 if report.passed else 1
    if failed:
        raise SystemExit(f"\n{failed}개 게시판이 검증을 통과하지 못했습니다.")
    print("\n모두 통과했습니다.")


if __name__ == "__main__":
    main()
