"""실제 대학 공지를 Gemini Extractor로 처리해 터미널에서 확인하는 개발용 명령.

실행 예시:
    export GEMINI_API_KEY="..."
    export GEMINI_MODEL="..."
    PYTHONPATH=backend python3 backend/scripts/preview_extract.py yonsei
"""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from app.crawler import Crawler
from app.extractor import DocumentExtractor
from app.llm import GeminiProvider
from app.schemas import CrawlRequest, CrawlScope, ExtractedChunk, ExtractionFailure
from preview_crawl import SCHOOLS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASKU Extractor 실제 공지 미리보기")
    parser.add_argument("school", choices=sorted(SCHOOLS), help="확인할 학교")
    parser.add_argument("--max-items", type=int, default=1, help="처리할 최대 공지 수 (기본값: 1)")
    parser.add_argument("--show-content", action="store_true", help="정제된 본문도 함께 출력")
    parser.add_argument("--summary", action="store_true", help="본문 미리보기와 추출 요약만 출력")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_items < 1 or args.max_items > 5:
        raise SystemExit("--max-items는 실제 API 비용을 위해 1~5 사이여야 합니다.")

    try:
        extractor = DocumentExtractor(GeminiProvider())
    except (ImportError, ValueError) as error:
        raise SystemExit(
            "Gemini 설정이 필요합니다. GEMINI_API_KEY와 GEMINI_MODEL을 설정하고 "
            "google-genai를 설치하세요."
        ) from error

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
    crawler = Crawler(hash_exists=lambda *_args: False)
    run = crawler.crawl(request, school.adapter_factory())
    pages = crawler.pages_for_extractor(run)
    print(f"학교: {school.name} / 수집: {len(pages)}건 / 수집 실패: {len(run.failures)}건")

    for index, page in enumerate(pages, start=1):
        print(f"\n========== {index}. {page.title_hint or '(제목 없음)'} ==========")
        result = extractor.process(page)
        if isinstance(result, ExtractionFailure):
            print("추출 실패")
            print(result.model_dump_json(indent=2))
            continue
        for chunk in result:
            print(
                f"청크 {chunk.chunk_index} / 상태: {chunk.extraction_status} / "
                f"본문: {len(chunk.content)}자 / 엔티티: {len(chunk.entities)}개 / 관계: {len(chunk.relations)}개"
            )
            if args.summary:
                preview = chunk.content.replace("\n", " ")[:240]
                print(f"본문 미리보기: {preview}{'…' if len(chunk.content) > 240 else ''}")
                print("엔티티: " + ", ".join(f"{item.type}={item.name}" for item in chunk.entities[:12]))
                print("관계: " + ", ".join(
                    f"{item.source} -{item.relation}→ {item.target}" for item in chunk.relations[:12]
                ))
                continue
            if args.show_content:
                print(f"본문:\n{chunk.content}\n")
            payload = {
                "entities": [item.model_dump() for item in chunk.entities],
                "relations": [item.model_dump() for item in chunk.relations],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
