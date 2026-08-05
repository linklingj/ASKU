"""RAG 검색 — 그래프 RAG(1단계) → 문서 RAG(2단계) → 실패 순의 2단 fallback.
설계 단일 기준은 docs/01_SYSTEM/07_graph-rag-engine.md.

``GraphRAG`` 는 크롤링 공지(``source_type='web'``)를 벡터 top-k 로 찾고 질문 엔티티의
1-hop 이웃으로 확장한다. Local/Global 라우팅, 커뮤니티 요약, multi-hop 순회, 재랭킹은
하지 않는다(07 §5). ``DocumentRAG`` 는 사용자가 올린 첨부 문서
(``source_type='attachment'``)만 벡터 top-k 로 찾는다 — 그래프 확장은 하지 않는다.
두 엔진 모두 근거 청크가 없거나 최고 유사도가 임계 미만이면 LLM 생성 없이 보류
문구를 반환한다(07 §3, 환각 방지). ``HybridRAG`` 가 그래프 → 문서 순서로 위임하는
오케스트레이션을 맡고, API 계층은 ``HybridRAG.answer()`` 만 호출한다.

저장소 접근은 06_storage.md, LLM 호출은 08_llm-provider.md 인터페이스만 사용한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from app.graph_builder import normalize_entity_key
from app.llm import Embedder, Extractor, Generator
from app.models import SOURCE_TYPE_ATTACHMENT, SOURCE_TYPE_WEB, Document, Entity
from app.prompts import rag_answer_instruction
from app.schemas import RagAnswer, Source

if TYPE_CHECKING:  # storage.py 는 pgvector 를 top-level import 하므로 런타임 의존을 피한다
    from app.storage import Neighbor


# 근거 부족 시 생성 대신 반환하는 보류 문구(07 §3). 답변 지시문에도 끼워 넣는다.
NO_EVIDENCE_ANSWER = "해당 정보를 찾지 못했습니다."

# 프롬프트 문안은 app/prompts.py 가 소유한다. 여기서는 보류 문구를 끼워 조립만 한다.
_ANSWER_INSTRUCTION = rag_answer_instruction(NO_EVIDENCE_ANSWER)


class RagStorage(Protocol):
    """RAG 엔진이 쓰는 Storage 공개 메서드의 최소 계약(06_storage.md).

    구현은 SQL 세부를 노출하지 않는 ``app.storage.Storage`` 다. 엔진은 이 구조적
    계약에만 의존해 저장소 구현·pgvector 의존과 분리된다.
    """

    def vector_search(
        self,
        school_id: int,
        query_embedding: Sequence[float],
        k: int,
        *,
        source_type: str | None = None,
    ) -> list[tuple[Document, float]]: ...

    def entities_by_norm_keys(self, school_id: int, norm_keys: Sequence[str]) -> list[Entity]: ...

    def neighbors(
        self, school_id: int, entity_ids: Sequence[int], *, hops: int = 1
    ) -> list["Neighbor"]: ...

    def get_documents(self, school_id: int, doc_ids: Sequence[int]) -> list[Document]: ...


class GraphRAG:
    """질문 → 크롤링 공지 벡터 top-k + 1-hop 그래프 확장 → 근거 기반 답변(1단계).

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
        """질문에 근거 기반으로 답한다. 근거가 없으면 생성 없이 보류한다.

        보류 응답은 ``source_type=None`` 으로, ``HybridRAG`` 가 문서 RAG 로 넘어갈지
        판단하는 신호가 된다.
        """

        query_vector = self.embedder.embed(question)
        hits = [
            (doc, score)
            for doc, score in self.storage.vector_search(
                school_id, query_vector, self.top_k, source_type=SOURCE_TYPE_WEB
            )
            if score >= self.min_similarity
        ]
        if not hits:
            return RagAnswer(answer=NO_EVIDENCE_ANSWER, sources=[], source_type=None)

        neighbors = self._expand_neighbors(school_id, question)
        context, sources = self._assemble_context(school_id, hits, neighbors)
        prompt = f"{_ANSWER_INSTRUCTION}\n\n[질문]\n{question}"
        answer = self.generator.generate(prompt, context).strip()

        # 컨텍스트 확장에 사용된 실제 그래프 엔티티 ID들 수집
        used_entity_ids: set[str] = set()
        for n in neighbors:
            src_id = getattr(n.source, "entity_id", None) if hasattr(n.source, "entity_id") else None
            tgt_id = getattr(n.target, "entity_id", None) if hasattr(n.target, "entity_id") else None
            if src_id is not None:
                used_entity_ids.add(f"e_{src_id}")
            if tgt_id is not None:
                used_entity_ids.add(f"e_{tgt_id}")

        return RagAnswer(
            answer=answer,
            sources=sources,
            entity_ids=sorted(used_entity_ids),
            source_type="graph",
        )

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


class DocumentRAG:
    """질문 → 업로드 첨부 문서(``source_type='attachment'``) 벡터 top-k → 답변(2단계).

    그래프 확장은 하지 않는다(07 §5 — 문서 RAG 는 벡터 top-k 전용). 그래프 RAG 가
    근거를 찾지 못했을 때 ``HybridRAG`` 가 두 번째로 호출하는 fallback 단계다.
    첨부에는 원문 링크가 없어 인용은 ``파일명 - N페이지`` 형태로 만든다.
    """

    def __init__(
        self,
        storage: RagStorage,
        embedder: Embedder,
        generator: Generator,
        *,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.generator = generator
        self.top_k = top_k
        self.min_similarity = min_similarity

    def answer(self, school_id: int, question: str) -> RagAnswer:
        """첨부 문서에서만 근거를 찾는다. 근거가 없으면 생성 없이 보류한다."""

        query_vector = self.embedder.embed(question)
        hits = [
            (doc, score)
            for doc, score in self.storage.vector_search(
                school_id, query_vector, self.top_k, source_type=SOURCE_TYPE_ATTACHMENT
            )
            if score >= self.min_similarity
        ]
        if not hits:
            return RagAnswer(answer=NO_EVIDENCE_ANSWER, sources=[], source_type=None)

        context = "\n\n".join(
            f"[근거 {rank}] {self._citation(doc)}\n{doc.content}"
            for rank, (doc, _score) in enumerate(hits, start=1)
        )
        prompt = f"{_ANSWER_INSTRUCTION}\n\n[질문]\n{question}"
        answer = self.generator.generate(prompt, context).strip()

        return RagAnswer(answer=answer, sources=self._collect_sources(hits), source_type="document")

    @staticmethod
    def _citation(doc: Document) -> str:
        title = doc.title or "(제목 없음)"
        return f"{title} - {doc.page}페이지" if doc.page is not None else title

    @staticmethod
    def _collect_sources(hits: Sequence[tuple[Document, float]]) -> list[Source]:
        """근거 출처를 문서당 한 줄로 모으되, 근거로 쓴 페이지는 모두 병합한다.

        같은 첨부의 여러 페이지가 함께 히트하면 ``"수강편람.pdf - 3, 15페이지"`` 처럼
        한 출처에 페이지를 모아 적는다. URL 기준으로만 중복을 제거하면 뒤쪽 페이지
        인용이 통째로 사라지기 때문이다. 문서 순서는 벡터 top-k 순서를 따르고,
        페이지 번호는 읽기 순서로 정렬한다.
        """

        titles: dict[str, str] = {}
        pages_by_url: dict[str, set[int]] = {}
        for doc, _score in hits:
            url = doc.source_url
            if url not in titles:
                titles[url] = doc.title or "(제목 없음)"
                pages_by_url[url] = set()
            if doc.page is not None:
                pages_by_url[url].add(doc.page)

        sources: list[Source] = []
        for url, title in titles.items():
            pages = sorted(pages_by_url[url])
            label = f"{title} - {', '.join(str(page) for page in pages)}페이지" if pages else title
            sources.append(Source(title=label, url=url))
        return sources


class HybridRAG:
    """그래프 RAG → (근거 없음) → 문서 RAG → (근거 없음) → 실패의 2단 검색 오케스트레이션.

    각 단계는 ``RagAnswer.source_type`` 으로 성공 여부를 신호한다(``None`` 이면 보류).
    두 단계 모두 보류하면 마지막 보류 응답을 그대로 돌려준다 — 답변은 보류 문구,
    ``sources`` 는 빈 목록, ``source_type`` 은 ``None`` 이다(07 §3).
    API 계층은 이 클래스의 ``answer()`` 만 호출한다.
    """

    def __init__(self, graph_rag: GraphRAG, document_rag: DocumentRAG) -> None:
        self.graph_rag = graph_rag
        self.document_rag = document_rag

    def answer(self, school_id: int, question: str) -> RagAnswer:
        graph_result = self.graph_rag.answer(school_id, question)
        if graph_result.source_type == "graph":
            return graph_result
        return self.document_rag.answer(school_id, question)
