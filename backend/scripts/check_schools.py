"""여러 학교의 수집 사이클을 한 번에 점검하는 개발용 명령.

학교를 등록하기 전에 **실제로 수집되는지** 확인한다. 서비스가 하는 일을 그대로
따라간다 — 규격 확보(전용 클래스 → 템플릿 → 선택 시 LLM) → 목록 수집 → 상세 수집
→ 본문 정제 → 검증.

저장소에 쓰지 않는다. 확인만 하고 끝난다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/check_schools.py \\
        "한양대=https://www.hanyang.ac.kr/web/www/-53" \\
        "국민대=https://www.kookmin.ac.kr/user/kmuNews/notice/1/index.do"

    PYTHONPATH=backend python3 backend/scripts/check_schools.py --file schools.txt --autogen
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from app.adapter_spec import AdapterSpec
from app.crawler import ADAPTER_REGISTRY, Crawler, CrawlRun, adapter_for, boards_for
from app.extractor import content_parser_for
from app.schemas import CrawlRequest, CrawlScope
from app.settings import load_env
from app.spec_generator import PageSample, collect_samples, generate_spec, match_template
from app.validation import ValidationReport, validate_detail, validate_listing


@dataclass
class Outcome:
    """학교 한 곳의 점검 결과."""

    name: str
    origin: str = "-"          # 규격 출처: dedicated / template:이름 / generated / none
    listing_rows: int = 0
    title_ratio: float = 0.0
    date_ratio: float = 0.0
    details: int = 0
    body_chars: int = 0        # 상세 표본의 평균 본문 길이
    note: str = ""

    @property
    def usable(self) -> bool:
        """수집·검색이 성립하는 최소 조건."""

        return self.listing_rows > 0 and self.title_ratio >= 0.9 and self.body_chars >= 50

    def row(self) -> str:
        mark = "OK " if self.usable else "-- "
        return (
            f"{mark}{self.name:8} {self.origin:18} 목록 {self.listing_rows:>3}행 "
            f"제목 {self.title_ratio:>4.0%} 날짜 {self.date_ratio:>4.0%} "
            f"본문 {self.body_chars:>5}자  {self.note}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="여러 학교의 수집 사이클 점검")
    parser.add_argument("targets", nargs="*", help="'이름=URL' 형식. 여러 개 지정 가능")
    parser.add_argument("--file", help="'이름=URL' 을 줄마다 적은 파일")
    parser.add_argument("--details", type=int, default=2, help="학교마다 확인할 상세 공지 수 (기본 2)")
    parser.add_argument("--autogen", action="store_true", help="템플릿이 없으면 LLM 으로 규격을 만들어 본다")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    return parser.parse_args()


def read_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    raw = list(args.targets)
    if args.file:
        with open(args.file, encoding="utf-8") as file:
            raw += [line.strip() for line in file if line.strip() and not line.startswith("#")]
    targets = []
    for item in raw:
        name, _, url = item.partition("=")
        if not url:
            name, url = urlsplit(item).hostname or item, item
        targets.append((name.strip(), url.strip()))
    if not targets:
        raise SystemExit("점검할 대상이 없습니다. '이름=URL' 을 넘기거나 --file 을 쓰세요.")
    return targets


def check_school(name: str, url: str, *, detail_count: int, autogen: bool) -> Outcome:
    """서비스와 같은 순서로 규격을 정하고, 그 규격으로 실제 수집해 본다."""

    outcome = Outcome(name=name)
    host = (urlsplit(url).hostname or "").lower()
    crawler = Crawler(hash_exists=lambda *_args: False)
    request = CrawlRequest(
        crawl_id=uuid4(),
        school_id=0,
        base_url=url,
        mode="initial",
        scope=CrawlScope(allowed_hosts=[host], max_listing_pages=1, max_items=detail_count),
    )

    if not crawler.robots_allowed(url):
        outcome.note = "robots.txt 가 수집을 허용하지 않음"
        return outcome

    spec: AdapterSpec | None = None
    if host in ADAPTER_REGISTRY:
        outcome.origin = "dedicated"
    else:
        samples = collect_samples(crawler, request, url, detail_count)
        if samples is None:
            outcome.note = "목록·상세 표본을 얻지 못함(구조 불일치 또는 JS 렌더링)"
            return outcome
        listing_sample, detail_samples = samples

        matched = match_template(host, listing_sample, detail_samples)
        if matched is not None:
            template_name, spec, _report = matched
            outcome.origin = f"template:{template_name}"
        elif autogen:
            spec = _generate(host, listing_sample, detail_samples, outcome)
            if spec is None:
                return outcome
            outcome.origin = "generated"
        else:
            outcome.origin = "none"
            outcome.note = "템플릿 불일치 — 공용 파서로 진행(--autogen 으로 LLM 시도)"

    return _measure(outcome, crawler, request, url, spec, detail_count)


def _generate(host: str, listing: PageSample, details: list[PageSample], outcome: Outcome) -> AdapterSpec | None:
    try:
        from app.llm import GeminiProvider

        result = generate_spec(GeminiProvider(), host, listing, details)
    except (ImportError, ValueError) as error:
        outcome.note = f"Gemini 설정 필요: {error}"
        return None
    if not result.accepted:
        outcome.origin = "generated(실패)"
        outcome.note = result.summary()
        return None
    if result.findings:
        outcome.note = "경고: " + "; ".join(f.code for f in result.findings[:2])
    return result.spec


def _measure(
    outcome: Outcome, crawler: Crawler, request: CrawlRequest, url: str, spec, detail_count: int
) -> Outcome:
    """정해진 규격으로 실제 수집·정제해 지표를 잰다."""

    adapter = adapter_for(url, spec)
    boards = boards_for(url, spec)
    run = crawler.crawl_boards(request, boards, adapter)

    listing_html = next(iter(run.first_listing_html.values()), None)
    if listing_html is None:
        outcome.note = outcome.note or "목록을 받지 못함"
        return outcome

    report = ValidationReport(target=outcome.name)
    validate_listing(adapter, listing_html, boards[0].url, report=report)
    outcome.listing_rows = report.listing_rows
    outcome.title_ratio = report.title_ratio
    outcome.date_ratio = report.date_ratio

    lengths = []
    titles = [item.title_hint or "" for item in adapter.parse_listing(listing_html, boards[0].url)]
    for page in run.pages[:detail_count]:
        document = content_parser_for(page.canonical_url, spec).parse(page.raw_html)
        lengths.append(len(document.content))
        others = [title for title in titles if title and title != page.title_hint]
        from app.crawler import ListingItem

        validate_detail(
            document,
            ListingItem(url=page.canonical_url, title_hint=page.title_hint, published_at_hint=page.published_at_hint),
            other_titles=others,
            report=report,
        )
    outcome.details = len(lengths)
    outcome.body_chars = sum(lengths) // len(lengths) if lengths else 0

    problems = [f.code for f in report.findings]
    if problems and not outcome.note:
        outcome.note = "지적: " + ", ".join(dict.fromkeys(problems))[:70]
    return outcome


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.WARNING, format="  %(message)s")
    args = parse_args()

    outcomes = []
    for name, url in read_targets(args):
        try:
            outcome = check_school(name, url, detail_count=args.details, autogen=args.autogen)
        except Exception as error:  # 한 학교의 실패가 나머지 점검을 막지 않는다
            outcome = Outcome(name=name, note=f"오류: {type(error).__name__} {error}"[:90])
        outcomes.append(outcome)
        if not args.json:
            print(outcome.row(), flush=True)

    if args.json:
        print(json.dumps([vars(o) | {"usable": o.usable} for o in outcomes], ensure_ascii=False, indent=2))
        return
    usable = sum(1 for o in outcomes if o.usable)
    print(f"\n수집 가능 {usable}/{len(outcomes)}곳")


if __name__ == "__main__":
    main()
