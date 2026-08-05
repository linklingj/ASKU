"""실제 공지 1건이 Graph Builder를 거쳐 만드는 문서·노드·엣지를 보여주는 개발용 명령.

Postgres와 bge-m3 모델은 쓰지 않는다. Gemini Extractor 결과를 메모리 Storage와
고정 길이 임시 임베딩으로 빌드해 그래프 구조만 눈으로 확인한다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/preview_graph_builder.py sejong
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any, Sequence
from uuid import uuid4

from app.crawler import Crawler
from app.extractor import DocumentExtractor
from app.graph_builder import GraphBuilder
from app.llm import Embedder, GeminiProvider
from app.models import EMBEDDING_DIM
from app.schemas import CrawlRequest, CrawlScope, ExtractedChunk, ExtractionFailure
from preview_crawl import SCHOOLS


class PreviewEmbedder(Embedder):
    """그래프 연결 확인용 임시 벡터. 실제 검색 품질 검증에는 사용하지 않는다."""

    def embed(self, text: str) -> list[float]:
        return [0.0] * EMBEDDING_DIM


class MemoryStorage:
    """GraphBuilder 공개 Storage 계약을 만족하는 비영속 미리보기 저장소."""

    def __init__(self) -> None:
        self._next_doc_id = 1
        self._next_entity_id = 1
        self._next_edge_id = 1
        self.documents: dict[int, dict[str, Any]] = {}
        self.entities: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[int, int, str], dict[str, Any]] = {}

    def upsert_document(
        self,
        school_id: int,
        source_url: str,
        title: str | None,
        content: str,
        chunk_index: int,
        content_hash: str,
        embedding: Sequence[float] | None,
        *,
        crawled_at: datetime | None = None,
    ) -> int:
        doc_id = self._next_doc_id
        self._next_doc_id += 1
        self.documents[doc_id] = {"title": title, "source_url": source_url, "chunk_index": chunk_index}
        return doc_id

    def upsert_entity(self, school_id: int, type: str, name: str, norm_key: str,
                      attributes: dict[str, Any], source_doc_ids: Sequence[int]) -> int:
        existing = self.entities.get(norm_key)
        if existing is None:
            existing = {"entity_id": self._next_entity_id, "type": type, "name": name, "source_doc_ids": []}
            self._next_entity_id += 1
            self.entities[norm_key] = existing
        existing["source_doc_ids"] = sorted(set(existing["source_doc_ids"]).union(source_doc_ids))
        return existing["entity_id"]

    def upsert_edge(self, school_id: int, source_entity_id: int, target_entity_id: int,
                    relation: str, source_doc_ids: Sequence[int]) -> int:
        key = (source_entity_id, target_entity_id, relation)
        existing = self.edges.get(key)
        if existing is None:
            existing = {"edge_id": self._next_edge_id, "source_doc_ids": []}
            self._next_edge_id += 1
            self.edges[key] = existing
        existing["source_doc_ids"] = sorted(set(existing["source_doc_ids"]).union(source_doc_ids))
        return existing["edge_id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASKU Graph Builder 구조 미리보기")
    parser.add_argument("school", choices=sorted(SCHOOLS), help="확인할 학교")
    parser.add_argument("--max-items", type=int, default=1, help="처리할 최대 공지 수 (1~3)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.max_items <= 3:
        raise SystemExit("--max-items는 Gemini API 비용을 위해 1~3 사이여야 합니다.")

    school = SCHOOLS[args.school]
    request = CrawlRequest(
        crawl_id=uuid4(), school_id=0, base_url=school.base_url, mode="initial",
        scope=CrawlScope(
            allowed_hosts=school.allowed_hosts, path_prefixes=school.path_prefixes,
            max_listing_pages=1, max_items=args.max_items,
        ),
    )
    crawler = Crawler(hash_exists=lambda *_args: False)
    pages = crawler.pages_for_extractor(crawler.crawl(request, school.adapter_factory()))
    # 본문 파서는 상세 URL의 호스트로 자동 선택된다(`content_parser_for`).
    extractor = DocumentExtractor(GeminiProvider())
    storage = MemoryStorage()
    builder = GraphBuilder(storage, PreviewEmbedder())

    print("※ 메모리 미리보기입니다. Postgres 저장·실제 bge-m3 임베딩은 수행하지 않습니다.")
    for page in pages:
        extracted = extractor.process(page)
        if isinstance(extracted, ExtractionFailure):
            print(f"\n추출 실패: {page.title_hint} ({extracted.error_code})")
            continue
        for item in extracted:
            result = builder.build(page.school_id, item)
            print(
                f"\n문서 청크 {item.chunk_index}: {result.status} / "
                f"노드 {len(result.entity_ids)}개 / 엣지 {len(result.edge_ids)}개"
            )
            for warning in result.warnings:
                print(f"  경고: {warning}")

    names_by_id = {value["entity_id"]: value["name"] for value in storage.entities.values()}
    print("\n========== 그래프 노드 ==========")
    for value in sorted(storage.entities.values(), key=lambda item: item["entity_id"]):
        print(f"[{value['entity_id']}] {value['type']}: {value['name']} (근거 문서: {value['source_doc_ids']})")
    print("\n========== 그래프 관계 ==========")
    for (source_id, target_id, relation), value in sorted(storage.edges.items()):
        print(
            f"[{value['edge_id']}] {names_by_id[source_id]} -{relation}→ {names_by_id[target_id]} "
            f"(근거 문서: {value['source_doc_ids']})"
        )


if __name__ == "__main__":
    main()
