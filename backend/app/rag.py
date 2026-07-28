"""Graph RAG 엔진 — 벡터 top-k 검색과 질문 엔티티 1-hop 그래프 확장으로 근거
컨텍스트를 만들고, 근거에만 기반한 답변을 생성한다. 설계 단일 기준은
docs/01_SYSTEM/07_graph-rag-engine.md.

hybrid 단일 전략(벡터 top-k + 1-hop 이웃)만 쓴다. Local/Global 라우팅, 커뮤니티
요약, multi-hop 순회, 재랭킹은 하지 않는다(07 §5). 근거 청크가 없거나 최고 유사도가
임계 미만이면 LLM 생성 없이 보류 문구를 반환한다(07 §3, 환각 방지).

저장소 접근은 06_storage.md, LLM 호출은 08_llm-provider.md 인터페이스만 사용한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from app.graph_builder import normalize_entity_key
from app.llm import Embedder, Extractor, Generator
from app.models import Document, Entity
from app.schemas import RagAnswer, Source

if TYPE_CHECKING:  # storage.py 는 pgvector 를 top-level import 하므로 런타임 의존을 피한다
    from app.storage import Neighbor


# 근거 부족 시 생성 대신 반환하는 보류 문구(07 §3).
NO_EVIDENCE_ANSWER = "해당 정보를 찾지 못했습니다."

# "컨텍스트에만 근거해 답하라"는 제약은 호출자(엔진) 책임이다(08_llm-provider.md).
_ANSWER_INSTRUCTION = (
    "너는 대학 공지 안내 도우미다. 아래 [컨텍스트]의 근거에만 기반해 한국어로 답하라.\n"
    "- 컨텍스트에 없는 내용은 추측하지 말고 모른다고 답하라.\n"
    "- 날짜·금액·자격 등 조건은 근거에 있는 값을 그대로 인용하라.\n"
    f'- 근거가 부족하면 "{NO_EVIDENCE_ANSWER}" 라고 답하라.'
)


class RagStorage(Protocol):
    """Graph RAG 엔진이 쓰는 Storage 공개 메서드의 최소 계약(06_storage.md).

    구현은 SQL 세부를 노출하지 않는 ``app.storage.Storage`` 다. 엔진은 이 구조적
    계약에만 의존해 저장소 구현·pgvector 의존과 분리된다.
    """

    def vector_search(
        self, school_id: int, query_embedding: Sequence[float], k: int
    ) -> list[tuple[Document, float]]: ...

    def entities_by_norm_keys(self, school_id: int, norm_keys: Sequence[str]) -> list[Entity]: ...

    def neighbors(
        self, school_id: int, entity_ids: Sequence[int], *, hops: int = 1
    ) -> list["Neighbor"]: ...

    def get_documents(self, school_id: int, doc_ids: Sequence[int]) -> list[Document]: ...


class GraphRAG:
    """질문 → 벡터 top-k + 1-hop 그래프 확장 → 근거 기반 답변.

    모든 저장소 조회는 ``school_id`` 로 격리된다(07 §2). 유사도 ``min_similarity``
    이상인 청크가 하나도 없으면 생성 없이 ``NO_EVIDENCE_ANSWER`` 를 반환한다(07 §3).
    답변에는 근거 ``sources``(제목·URL)를 항상 함께 반환한다.
    """

    def __init__(
        self,
        storage: RagStorage,
        embedder: Embedder,
        extractor: Extractor,
        generator: Generator,
        *,
        top_k: int = 5,
        # ponytail: 환각 방지 보정 노브. 실제 pgvector·bge-m3 데이터로 튜닝한다.
        min_similarity: float = 0.3,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.extractor = extractor
        self.generator = generator
        self.top_k = top_k
        self.min_similarity = min_similarity

    def answer(self, school_id: int, question: str) -> RagAnswer:
        """질문에 근거 기반으로 답한다. 근거가 없으면 생성 없이 보류한다."""

        query_vector = self.embedder.embed(question)
        hits = [
            (doc, score)
            for doc, score in self.storage.vector_search(school_id, query_vector, self.top_k)
            if score >= self.min_similarity
        ]
        if not hits:
            return RagAnswer(answer=NO_EVIDENCE_ANSWER, sources=[])

        neighbors = self._expand_neighbors(school_id, question)
        context, sources = self._assemble_context(school_id, hits, neighbors)
        prompt = f"{_ANSWER_INSTRUCTION}\n\n[질문]\n{question}"
        answer = self.generator.generate(prompt, context).strip()
        return RagAnswer(answer=answer, sources=sources)

    def _expand_neighbors(self, school_id: int, question: str) -> list["Neighbor"]:
        """질문에서 뽑은 엔티티를 norm_key 로 그래프에 매핑해 1-hop 이웃을 가져온다."""

        extraction = self.extractor.extract(question)
        norm_keys = {
            normalize_entity_key(entity.type, entity.name)
            for entity in extraction.entities
            if entity.type.strip() and entity.name.strip()
        }
        if not norm_keys:
            return []
        matched = self.storage.entities_by_norm_keys(school_id, sorted(norm_keys))
        entity_ids = [entity.entity_id for entity in matched if entity.entity_id is not None]
        if not entity_ids:
            return []
        return self.storage.neighbors(school_id, entity_ids, hops=1)

    def _assemble_context(
        self,
        school_id: int,
        hits: Sequence[tuple[Document, float]],
        neighbors: Sequence["Neighbor"],
    ) -> tuple[str, list[Source]]:
        """벡터 top-k 청크를 뼈대로, 1-hop 이웃을 사실 문장으로 덧붙인 컨텍스트를 만든다."""

        docs_by_id: dict[int, Document] = {doc.doc_id: doc for doc, _ in hits if doc.doc_id is not None}
        missing = sorted(
            {doc_id for neighbor in neighbors for doc_id in neighbor.edge.source_doc_ids}
            - docs_by_id.keys()
        )
        for doc in self.storage.get_documents(school_id, missing):
            if doc.doc_id is not None:
                docs_by_id[doc.doc_id] = doc

        blocks = [
            f"[근거 {rank}] {doc.title or '(제목 없음)'}\n출처: {doc.source_url}\n{doc.content}"
            for rank, (doc, _score) in enumerate(hits, start=1)
        ]
        if neighbors:
            facts = []
            for neighbor in neighbors:
                url = self._first_source_url(neighbor, docs_by_id)
                suffix = f" (출처: {url})" if url else ""
                facts.append(
                    f"- {neighbor.source.name} —{neighbor.edge.relation}→ {neighbor.target.name}{suffix}"
                )
            blocks.append("[관계]\n" + "\n".join(facts))

        return "\n\n".join(blocks), self._collect_sources(hits, neighbors, docs_by_id)

    @staticmethod
    def _first_source_url(neighbor: "Neighbor", docs_by_id: dict[int, Document]) -> str | None:
        for doc_id in neighbor.edge.source_doc_ids:
            doc = docs_by_id.get(doc_id)
            if doc is not None:
                return doc.source_url
        return None

    @staticmethod
    def _collect_sources(
        hits: Sequence[tuple[Document, float]],
        neighbors: Sequence["Neighbor"],
        docs_by_id: dict[int, Document],
    ) -> list[Source]:
        """근거 출처를 URL 기준 중복 없이, 벡터 top-k 순서를 우선해 모은다."""

        sources: dict[str, Source] = {}

        def add(title: str | None, url: str | None) -> None:
            if url and url not in sources:
                sources[url] = Source(title=title, url=url)

        for doc, _score in hits:
            add(doc.title, doc.source_url)
        for neighbor in neighbors:
            for doc_id in neighbor.edge.source_doc_ids:
                doc = docs_by_id.get(doc_id)
                if doc is not None:
                    add(doc.title, doc.source_url)
        return list(sources.values())
