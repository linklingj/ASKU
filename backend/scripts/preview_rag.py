"""실제 bge-m3 임베딩과 Gemini LLM을 연결해 Graph RAG 질의를 끝까지 돌려보는 개발용 명령.

Postgres 없이, 크롤 → 추출 → 그래프 빌드 결과를 메모리 저장소에 **실제 임베딩과 함께**
쌓은 뒤 질문을 같은 임베딩·LLM으로 답한다. 벡터 검색(코사인)·1-hop 그래프 확장·근거
없음 보류는 실제로 동작하고, 저장소만 메모리다(preview_graph_builder.py와 같은 결).

필요:
  - 패키지: ``pip install google-genai FlagEmbedding``
  - 환경변수: ``GEMINI_API_KEY``, ``GEMINI_MODEL`` (backend/.env 또는 셸에 export)
  - 첫 실행 시 bge-m3 모델(~2GB) 다운로드

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/preview_rag.py sejong \
        --question "계약직원 지원 자격이 뭐야?" --max-items 3
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from app.crawler import Crawler
from app.extractor import DocumentExtractor
from app.graph_builder import GraphBuilder
from app.llm import GeminiProvider, LocalEmbedder
from app.models import Document, Edge, Entity
from app.rag import GraphRAG
from app.schemas import CrawlRequest, CrawlScope, ExtractionFailure
from preview_crawl import SCHOOLS


@dataclass
class _Neighbor:
    """저장소 ``Neighbor``를 흉내내는 미리보기용 1-hop 결과(엔진이 읽는 필드만)."""

    source: Entity
    edge: Edge
    target: Entity


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class MemoryRagStorage:
    """GraphBuilder 쓰기 계약과 RagStorage 읽기 계약을 함께 만족하는 비영속 저장소.

    실제 임베딩을 보관하고 코사인으로 벡터 검색한다. Postgres/pgvector는 쓰지 않는다.
    """

    def __init__(self) -> None:
        self._next_doc_id = 1
        self._next_entity_id = 1
        self._next_edge_id = 1
        self.documents: dict[int, Document] = {}
        self._doc_key: dict[tuple, int] = {}
        self.entities: dict[str, Entity] = {}  # norm_key -> Entity
        self._entity_by_id: dict[int, Entity] = {}
        self.edges: dict[tuple[int, int, str], Edge] = {}

    # ── GraphBuilder 쓰기 계약 ──────────────────────────────
    def upsert_document(
        self, school_id: int, source_url: str, title: str | None, content: str,
        chunk_index: int, content_hash: str, embedding: Sequence[float] | None,
        *, crawled_at: datetime | None = None,
    ) -> int:
        key = (school_id, source_url, content_hash, chunk_index)
        if key in self._doc_key:
            doc_id = self._doc_key[key]
        else:
            doc_id = self._next_doc_id
            self._next_doc_id += 1
            self._doc_key[key] = doc_id
        self.documents[doc_id] = Document(
            doc_id=doc_id, school_id=school_id, source_url=source_url, title=title,
            content=content, chunk_index=chunk_index, content_hash=content_hash,
            embedding=list(embedding) if embedding is not None else None, crawled_at=crawled_at,
        )
        return doc_id

    def upsert_entity(
        self, school_id: int, type: str, name: str, norm_key: str,
        attributes: dict[str, Any], source_doc_ids: Sequence[int],
    ) -> int:
        existing = self.entities.get(norm_key)
        if existing is None:
            entity = Entity(
                entity_id=self._next_entity_id, school_id=school_id, type=type, name=name,
                norm_key=norm_key, attributes=dict(attributes), source_doc_ids=list(source_doc_ids),
            )
            self._next_entity_id += 1
            self.entities[norm_key] = entity
            self._entity_by_id[entity.entity_id] = entity
            return entity.entity_id
        existing.attributes = {**existing.attributes, **attributes}
        existing.source_doc_ids = sorted(set(existing.source_doc_ids).union(source_doc_ids))
        return existing.entity_id

    def upsert_edge(
        self, school_id: int, source_entity_id: int, target_entity_id: int,
        relation: str, source_doc_ids: Sequence[int],
    ) -> int:
        key = (source_entity_id, target_entity_id, relation)
        existing = self.edges.get(key)
        if existing is None:
            edge = Edge(
                edge_id=self._next_edge_id, school_id=school_id, source_entity_id=source_entity_id,
                target_entity_id=target_entity_id, relation=relation, source_doc_ids=list(source_doc_ids),
            )
            self._next_edge_id += 1
            self.edges[key] = edge
            return edge.edge_id
        existing.source_doc_ids = sorted(set(existing.source_doc_ids).union(source_doc_ids))
        return existing.edge_id

    # ── RagStorage 읽기 계약 ────────────────────────────────
    def vector_search(self, school_id: int, query_embedding: Sequence[float], k: int) -> list[tuple[Document, float]]:
        scored = [
            (doc, _cosine(query_embedding, doc.embedding))
            for doc in self.documents.values()
            if doc.school_id == school_id and doc.embedding is not None
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def entities_by_norm_keys(self, school_id: int, norm_keys: Sequence[str]) -> list[Entity]:
        keys = set(norm_keys)
        return [e for e in self.entities.values() if e.school_id == school_id and e.norm_key in keys]

    def neighbors(self, school_id: int, entity_ids: Sequence[int], *, hops: int = 1) -> list[_Neighbor]:
        ids = set(entity_ids)
        result = []
        for edge in self.edges.values():
            if edge.school_id != school_id:
                continue
            if edge.source_entity_id in ids or edge.target_entity_id in ids:
                result.append(_Neighbor(
                    source=self._entity_by_id[edge.source_entity_id],
                    edge=edge,
                    target=self._entity_by_id[edge.target_entity_id],
                ))
        return result

    def get_documents(self, school_id: int, doc_ids: Sequence[int]) -> list[Document]:
        return [self.documents[i] for i in doc_ids if i in self.documents and self.documents[i].school_id == school_id]


def _load_env(path: Path) -> None:
    """의존성 없이 backend/.env 의 KEY=VALUE 를 환경변수로 로드(이미 있으면 유지)."""

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASKU Graph RAG 실제 임베딩·LLM 질의 미리보기")
    parser.add_argument("school", choices=sorted(SCHOOLS), help="색인할 학교")
    parser.add_argument("--question", required=True, help="질문")
    parser.add_argument("--max-items", type=int, default=3, help="색인할 최대 공지 수 (1~5)")
    parser.add_argument("--min-similarity", type=float, default=0.3, help="근거 유사도 임계값")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.max_items <= 5:
        raise SystemExit("--max-items는 Gemini API 비용을 위해 1~5 사이여야 합니다.")
    _load_env(Path(__file__).resolve().parents[1] / ".env")

    school = SCHOOLS[args.school]
    print("※ 메모리 미리보기입니다. Postgres 없이 실제 bge-m3 임베딩·Gemini 로 질의합니다.")
    print("bge-m3 모델 로드 중... (첫 실행은 다운로드로 시간이 걸립니다)")
    embedder = LocalEmbedder()
    gemini = GeminiProvider()
    storage = MemoryRagStorage()

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
    extractor = DocumentExtractor(gemini)
    builder = GraphBuilder(storage, embedder)

    print(f"\n=== 색인: {school.base_url} (최대 {args.max_items}건) ===")
    for page in pages:
        extracted = extractor.process(page)
        if isinstance(extracted, ExtractionFailure):
            print(f"  추출 실패: {page.title_hint} ({extracted.error_code})")
            continue
        for item in extracted:
            result = builder.build(page.school_id, item)
            print(f"  청크 {item.chunk_index}: {result.status} / 노드 {len(result.entity_ids)} / 엣지 {len(result.edge_ids)}")
    print(f"색인 완료: 문서 {len(storage.documents)} / 엔티티 {len(storage.entities)} / 엣지 {len(storage.edges)}")

    engine = GraphRAG(storage, embedder, gemini, gemini, min_similarity=args.min_similarity)
    result = engine.answer(0, args.question)

    print(f"\n=== 질문 ===\n{args.question}")
    print(f"\n=== 답변 ===\n{result.answer}")
    print("\n=== 근거(sources) ===")
    if not result.sources:
        print("  (없음)")
    for source in result.sources:
        print(f"  - {source.title or '(제목 없음)'} :: {source.url}")


if __name__ == "__main__":
    main()
