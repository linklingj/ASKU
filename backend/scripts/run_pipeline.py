"""학교 한 곳을 크롤부터 엔티티 추출까지 실제로 돌려 보는 개발용 명령.

`check_schools.py` 가 **규격이 맞는지**를 확인한다면, 이쪽은 **데이터가 실제로
나오는지**를 확인한다. 여러 페이지를 돌고, 본문을 정제하고, 원하면 LLM 까지
불러 엔티티와 관계를 뽑아 화면에 보여 준다.

저장소에 쓰지 않는다. 서비스와 같은 코드 경로를 쓰되 결과만 출력한다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/run_pipeline.py \\
        "고려대=https://www.korea.ac.kr/ko/566/subview.do"

    # 여러 학교를 이어서. 엔티티 추출까지 하려면 --extract
    # (Gemini 를 부른다. backend/.env 의 GEMINI_API_KEY 필요)
    PYTHONPATH=backend python3 backend/scripts/run_pipeline.py \\
        "고려대=https://www.korea.ac.kr/ko/566/subview.do" \\
        "경희대=https://www.khu.ac.kr/kor/user/bbs/BMSR00040/list.do?menuNo=200317" \\
        --extract

`--limit` 은 수집할 공지 수다. 기본 10 건으로 두어 남의 서버에 부담을 주지 않는다.
`--extract` 는 공지 건당 LLM 을 부르므로 `--extract-limit` 로 따로 제한한다.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit
from uuid import uuid4

from app.crawler import Crawler, CrawlRun, DEFAULT_BOARD_LABEL, adapter_for, boards_for
from app.extractor import DocumentExtractor, content_parser_for
from app.rendering import PlaywrightRenderer
from app.schemas import CrawlRequest, CrawlScope
from app.settings import load_env
from app.spec_templates import host_spec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학교 한 곳의 수집·추출 파이프라인을 돌려 본다")
    parser.add_argument("schools", nargs="+", help="'이름=URL' 형식. 여러 개를 나열할 수 있다")
    parser.add_argument("--limit", type=int, default=10, help="수집할 공지 수 (기본 10)")
    parser.add_argument("--discover", action="store_true", help="하위 게시판(탭)을 찾아 함께 돈다")
    parser.add_argument("--extract", action="store_true", help="엔티티 추출까지 실행한다 (LLM 호출)")
    parser.add_argument("--extract-limit", type=int, default=2, help="추출할 공지 수 (기본 2)")
    return parser.parse_args()


def main() -> None:
    load_env()
    args = parse_args()
    for entry in args.schools:
        name, _, url = entry.partition("=")
        if not url:
            raise SystemExit(f"'이름=URL' 형식으로 넘겨 주세요: {entry}")
        run_school(name, url, args)
        print()


def run_school(name: str, url: str, args: argparse.Namespace) -> None:
    host = (urlsplit(url).hostname or "").lower()
    # 학교별 규격은 저장소에 있다. 없으면 전용 클래스나 공용 파서로 떨어진다 —
    # 여기서는 템플릿 대조·LLM 생성을 하지 않는다. 그쪽은 check_schools.py 가 본다.
    spec = host_spec(host)
    # 목록을 자바스크립트로 그리는 학교(중앙대)는 브라우저가 있어야 한다.
    # 넘기지 않으면 목록 0행으로 조용히 끝난다.
    renderer = PlaywrightRenderer() if spec is not None and spec.render != "off" else None
    crawler = Crawler(hash_exists=lambda *_: False, renderer=renderer)
    request = CrawlRequest(
        crawl_id=uuid4(), school_id=0, base_url=url, mode="initial",
        scope=CrawlScope(allowed_hosts=[host], max_items=args.limit),
    )

    adapter = adapter_for(url, spec)
    if args.discover and spec is not None:
        # 서비스는 학교를 등록할 때 한 번 돌려 게시판을 규격에 박아 둔다.
        from app.spec_generator import PageSample, discover_boards

        listing_html = crawler._fetch(request, url, CrawlRun())
        if listing_html:
            spec = discover_boards(spec, PageSample(url, listing_html), crawler, request)
    boards = boards_for(url, spec)
    run = crawler.crawl_boards(request, boards, adapter)
    pages = crawler.pages_for_extractor(run)
    if renderer is not None:
        renderer.close()

    print(f"[{name}] {url}")
    print(f"  규격 {'학교별' if spec else '없음'} · 어댑터 {type(adapter).__name__} · 게시판 {len(boards)}개")
    for board in boards:
        # 게시판별 수집 건수. 여러 개면 라운드 로빈으로 고르게 도는지 여기서 보인다.
        label = board.label or DEFAULT_BOARD_LABEL
        collected = sum(1 for page in run.pages if run.board_of.get(page.canonical_url) == label)
        print(f"      {label:18} {collected:4}건  {board.url[:72]}")
    print(f"  수집 {len(run.pages)}건 (신규 {len(pages)}건)\n")

    parser = content_parser_for(url, spec)
    for page in pages:
        try:
            content = parser.parse(page.raw_html).content
        except ValueError:
            # 규격이 어긋나면 서비스는 공용 파서로 폴백한다. 여기서는 그 사실을 드러낸다.
            content = ""
        date = page.published_at_hint.date() if page.published_at_hint else "-"
        mark = " ⚠︎본문없음" if not content else ""
        print(f"  · {(page.title_hint or '')[:44]:46} {date}  {len(content):5}자  첨부 {len(page.attachments)}{mark}")

    if not args.extract:
        print("\n  (엔티티 추출은 --extract 로 실행합니다)")
        return

    from app.llm import GeminiProvider

    print()
    extractor = DocumentExtractor(GeminiProvider(), parsers={0: parser}, sleeper=lambda _: None)
    for page in pages[: args.extract_limit]:
        result = extractor.process(page)
        if not isinstance(result, list):
            print(f"  추출 실패 [{result.error_code}] {(page.title_hint or '')[:40]}")
            continue
        for chunk in result:
            print(f"  ▸ {chunk.title[:52]}  [{chunk.extraction_status}]")
            for entity in chunk.entities:
                print(f"      {entity.type:10} {entity.name[:52]}")
            for relation in chunk.relations:
                print(f"      {relation.source[:18]} —{relation.relation}→ {relation.target[:26]}")
            print()


if __name__ == "__main__":
    main()
