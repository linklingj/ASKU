"""새 학교의 수집 규격을 자동 생성하는 개발용 명령.

목록과 상세 몇 건을 받아 Gemini 에 보여 주고 규격을 만든다. 검증을 통과한 것만
출력하며, `--save` 를 붙이면 저장소에 등록한다.

기본은 저장하지 않는다. 자동 생성한 규격이 곧바로 크롤에 쓰이면 잘못된 선택자로
남의 서버를 긁게 되므로, 몇 개 학교로 품질을 확인한 뒤 저장을 켜는 편이 안전하다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/generate_spec.py https://www.ajou.ac.kr/kr/ajou/notice.do
    PYTHONPATH=backend python3 backend/scripts/generate_spec.py <URL> --save
"""

from __future__ import annotations

import argparse
import json
import logging
from urllib.parse import urlsplit
from uuid import uuid4

from app.crawler import CrawlRun, Crawler, adapter_for
from app.llm import GeminiProvider
from app.schemas import CrawlRequest, CrawlScope
from app.settings import load_env
from app.spec_generator import PageSample, generate_spec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학교 수집 규격 자동 생성")
    parser.add_argument("url", help="학교 공지 목록 URL")
    parser.add_argument("--details", type=int, default=2, help="규격 생성에 쓸 상세 공지 수 (기본 2)")
    parser.add_argument("--attempts", type=int, default=3, help="최대 시도 횟수 (기본 3)")
    parser.add_argument("--save", action="store_true", help="검증을 통과하면 저장소에 등록한다")
    parser.add_argument(
        "--model",
        help="규격 생성에 쓸 모델. 기본은 GEMINI_MODEL. "
        "기본 모델이 과부하(503)일 때 가벼운 모델로 바꿔 시험할 수 있다",
    )
    return parser.parse_args()


def collect_samples(crawler: Crawler, url: str, count: int) -> tuple[PageSample, list[PageSample]]:
    """목록 1페이지와 상세 몇 건을 받는다. 운영과 같은 robots·요청 간격을 지킨다."""

    request = CrawlRequest(
        crawl_id=uuid4(),
        school_id=0,
        base_url=url,
        mode="initial",
        scope=CrawlScope(allowed_hosts=[urlsplit(url).hostname or ""]),
    )
    run = CrawlRun()
    if not crawler.robots_allowed(url):
        raise SystemExit(f"robots.txt 가 수집을 허용하지 않습니다: {url}")
    listing_html = crawler._fetch(request, url, run)  # noqa: SLF001 — 개발용 스크립트
    if listing_html is None:
        raise SystemExit(f"목록을 받지 못했습니다: {url}")

    # 표본 링크는 공통 어댑터로 찾는다. 규격이 아직 없으니 완벽할 필요는 없고,
    # 상세 페이지 몇 장만 확보하면 된다.
    items = list(adapter_for(url).parse_listing(listing_html, url))
    details: list[PageSample] = []
    for item in items:
        if len(details) >= count:
            break
        if not crawler.robots_allowed(item.url):
            continue
        html = crawler._fetch(request, item.url, run, referer=url)  # noqa: SLF001
        if html is not None:
            details.append(PageSample(item.url, html))
    if not details:
        raise SystemExit(
            "상세 페이지 표본을 얻지 못했습니다. 공통 어댑터가 목록에서 링크를 찾지 못했을 수 있습니다."
        )
    return PageSample(url, listing_html), details


def main() -> None:
    load_env()  # backend/.env 에 키를 두면 매번 export 하지 않아도 된다
    # 시도마다 진행 상황을 흘려 보낸다. 중간에 끊겨도 어디까지 갔는지 남는다.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    host = (urlsplit(args.url).hostname or "").lower()
    crawler = Crawler(hash_exists=lambda *_args: False)

    listing, details = collect_samples(crawler, args.url, args.details)
    print(f"표본: 목록 1건 + 상세 {len(details)}건")

    try:
        drafter = GeminiProvider(model=args.model) if args.model else GeminiProvider()
    except (ImportError, ValueError) as error:
        raise SystemExit(
            "Gemini 설정이 필요합니다. GEMINI_API_KEY·GEMINI_MODEL 을 설정하고 google-genai 를 설치하세요."
        ) from error

    result = generate_spec(drafter, host, listing, details, max_attempts=args.attempts)
    print(f"결과: {result.summary()}")
    for report in result.reports:
        print(f"  시도 결과 — {report.summary()}")
        for finding in report.findings:
            print(f"      - {finding}")

    if not result.accepted:
        raise SystemExit("\n검증을 통과한 규격을 만들지 못했습니다. 사람 검토가 필요합니다.")

    payload = result.spec.model_dump(mode="json")
    print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))

    if args.save:
        from app.storage import Storage

        storage = Storage.from_env()
        try:
            storage.upsert_adapter_spec(host, payload, source="generated")
        finally:
            storage.close()
        print(f"\n저장했습니다: {host}")
    else:
        print("\n저장하지 않았습니다. 등록하려면 --save 를 붙이세요.")


if __name__ == "__main__":
    main()
