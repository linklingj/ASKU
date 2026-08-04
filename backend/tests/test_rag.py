"""RAG 엔진(그래프 → 문서 2단 검색)의 DB·모델 독립 계약 테스트.

저장소(RagStorage)와 LLM(Embedder·Extractor·Generator)을 가짜로 주입해 pgvector·
bge-m3·Gemini 없이 질의 흐름과 환각 방지 분기를 검증한다.
"""

from types import SimpleNamespace
import unittest

from app.graph_builder import normalize_entity_key
from app.llm import Embedder, Extraction, Extractor, Generator
from app.models import EMBEDDING_DIM, Document
from app.rag import NO_EVIDENCE_ANSWER, DocumentRAG, GraphRAG, HybridRAG
from app.schemas import ExtractedEntity


class FakeEmbedder(Embedder):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.0] * EMBEDDING_DIM


class FakeExtractor(Extractor):
    def __init__(self, entities: list[ExtractedEntity] | None = None) -> None:
        self._entities = entities or []
        self.inputs: list[str] = []

    def extract(self, text: str) -> Extraction:
        self.inputs.append(text)
        return Extraction(entities=self._entities)


class FakeGenerator(Generator):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, context: str) -> str:
        self.calls.append((prompt, context))
        return "  생성된 답변  "  # 앞뒤 공백으로 strip 적용 확인


class FakeStorage:
    def __init__(self, *, hits=(), entities=(), neighbors=(), documents=()) -> None:
        self._hits = list(hits)  # [(Document, score)]
        self._entities = list(entities)  # [SimpleNamespace(entity_id, norm_key)]
        self._neighbors = list(neighbors)
        self._docs = {doc.doc_id: doc for doc in documents}
        self.calls: list[tuple] = []

    def vector_search(self, school_id, query_embedding, k, *, source_type=None):
        self.calls.append(("vector_search", school_id, source_type))
        hits = self._hits
        if source_type is not None:
            hits = [(d, s) for d, s in hits if getattr(d, "source_type", "web") == source_type]
        return hits[:k]

    def entities_by_norm_keys(self, school_id, norm_keys):
        self.calls.append(("entities_by_norm_keys", school_id, tuple(norm_keys)))
        keys = set(norm_keys)
        return [entity for entity in self._entities if entity.norm_key in keys]

    def neighbors(self, school_id, entity_ids, *, hops=1):
        self.calls.append(("neighbors", school_id, tuple(entity_ids), hops))
        return list(self._neighbors)

    def get_documents(self, school_id, doc_ids):
        self.calls.append(("get_documents", school_id, tuple(doc_ids)))
        return [self._docs[doc_id] for doc_id in doc_ids if doc_id in self._docs]

    def method_names(self):
        return [call[0] for call in self.calls]

    def school_ids(self):
        return {call[1] for call in self.calls}


def doc(doc_id: int, *, title="제목", url=None, content="본문", source_type="web", page=None) -> Document:
    return Document(
        doc_id=doc_id, school_id=1, source_url=url or f"https://ex.edu/{doc_id}",
        title=title, content=content, content_hash="h",
        source_type=source_type, page=page,
    )


def entity(norm_key: str, entity_id: int) -> SimpleNamespace:
    return SimpleNamespace(entity_id=entity_id, norm_key=norm_key)


def neighbor(source_name, relation, target_name, source_doc_ids) -> SimpleNamespace:
    return SimpleNamespace(
        source=SimpleNamespace(name=source_name),
        edge=SimpleNamespace(relation=relation, source_doc_ids=source_doc_ids),
        target=SimpleNamespace(name=target_name),
    )


def engine(storage, *, generator=None, extractor=None, **kwargs) -> GraphRAG:
    return GraphRAG(
        storage, FakeEmbedder(), extractor or FakeExtractor(), generator or FakeGenerator(), **kwargs
    )


class GraphRagTests(unittest.TestCase):
    def test_answer_combines_vector_chunks_and_one_hop_neighbors_with_sources(self) -> None:
        chunk = doc(1, title="장학금 안내", url="https://ex.edu/1", content="장학금 신청 본문")
        neighbor_doc = doc(2, title="담당 부서", url="https://ex.edu/2", content="학생지원팀 안내")
        norm_key = normalize_entity_key("장학금", "ASKU 장학금")
        storage = FakeStorage(
            hits=[(chunk, 0.9)],
            entities=[entity(norm_key, 201)],
            neighbors=[neighbor("ASKU 장학금", "담당", "학생지원팀", [2])],
            documents=[neighbor_doc],  # 이웃 근거 문서는 top-k 밖 → get_documents 로 조회
        )
        embedder = FakeEmbedder()
        extractor = FakeExtractor([ExtractedEntity(type="장학금", name="ASKU 장학금")])
        generator = FakeGenerator()

        result = GraphRAG(storage, embedder, extractor, generator).answer(1, "장학금 신청 언제야?")

        self.assertEqual(result.answer, "생성된 답변")  # strip 적용
        self.assertEqual(len(generator.calls), 1)
        prompt, context = generator.calls[0]
        self.assertIn("장학금 신청 언제야?", prompt)
        self.assertIn("장학금 신청 본문", context)  # 벡터 청크 본문
        self.assertIn("https://ex.edu/1", context)
        self.assertIn("ASKU 장학금 —담당→ 학생지원팀 (출처: https://ex.edu/2)", context)  # 이웃 사실 문장
        self.assertEqual(
            [(source.title, source.url) for source in result.sources],
            [("장학금 안내", "https://ex.edu/1"), ("담당 부서", "https://ex.edu/2")],  # top-k 우선
        )
        self.assertIn(("get_documents", 1, (2,)), storage.calls)  # top-k 밖 근거만 조회
        self.assertEqual(embedder.inputs, ["장학금 신청 언제야?"])
        self.assertEqual(extractor.inputs, ["장학금 신청 언제야?"])

    def test_no_hits_returns_no_evidence_without_extract_or_generate(self) -> None:
        storage = FakeStorage(hits=[])
        extractor = FakeExtractor([ExtractedEntity(type="장학금", name="ASKU")])
        generator = FakeGenerator()

        result = engine(storage, generator=generator, extractor=extractor).answer(1, "질문")

        self.assertEqual(result.answer, NO_EVIDENCE_ANSWER)
        self.assertEqual(result.sources, [])
        self.assertEqual(generator.calls, [])  # 생성 없이 보류
        self.assertEqual(extractor.inputs, [])  # 근거 없음 판정이 추출보다 먼저

    def test_only_chunks_at_or_above_threshold_enter_context(self) -> None:
        keep = doc(1, url="https://ex.edu/keep", content="keep-본문")
        drop = doc(2, url="https://ex.edu/drop", content="drop-본문")
        storage = FakeStorage(hits=[(keep, 0.5), (drop, 0.1)])
        generator = FakeGenerator()

        result = engine(storage, generator=generator, min_similarity=0.3).answer(1, "q")

        _, context = generator.calls[0]
        self.assertIn("keep-본문", context)
        self.assertNotIn("drop-본문", context)  # 임계 미만 청크는 컨텍스트·출처에서 제외
        self.assertEqual([source.url for source in result.sources], ["https://ex.edu/keep"])

    def test_all_hits_below_threshold_returns_no_evidence(self) -> None:
        storage = FakeStorage(hits=[(doc(1), 0.2)])
        generator = FakeGenerator()

        result = engine(storage, generator=generator, min_similarity=0.3).answer(1, "질문")

        self.assertEqual(result.answer, NO_EVIDENCE_ANSWER)
        self.assertEqual(generator.calls, [])

    def test_question_without_entities_skips_graph_expansion(self) -> None:
        storage = FakeStorage(hits=[(doc(1), 0.8)])
        generator = FakeGenerator()

        result = engine(storage, generator=generator, extractor=FakeExtractor([])).answer(1, "q")

        self.assertEqual(result.answer, "생성된 답변")
        self.assertNotIn("entities_by_norm_keys", storage.method_names())
        self.assertNotIn("neighbors", storage.method_names())
        _, context = generator.calls[0]
        self.assertNotIn("[관계]", context)

    def test_extracted_entity_absent_from_graph_skips_neighbors(self) -> None:
        storage = FakeStorage(hits=[(doc(1), 0.8)], entities=[])  # 그래프에 매칭 엔티티 없음
        extractor = FakeExtractor([ExtractedEntity(type="장학금", name="없는엔티티")])

        result = engine(storage, extractor=extractor).answer(1, "q")

        self.assertIn("entities_by_norm_keys", storage.method_names())
        self.assertNotIn("neighbors", storage.method_names())
        self.assertEqual(result.answer, "생성된 답변")

    def test_vector_search_is_scoped_to_web_source_type(self) -> None:
        storage = FakeStorage(hits=[(doc(1), 0.9)])

        engine(storage).answer(1, "q")

        self.assertIn(("vector_search", 1, "web"), storage.calls)

    def test_all_storage_queries_are_scoped_to_the_given_school(self) -> None:
        neighbor_doc = doc(2, url="https://ex.edu/2")
        norm_key = normalize_entity_key("장학금", "ASKU")
        storage = FakeStorage(
            hits=[(doc(1), 0.9)],
            entities=[entity(norm_key, 201)],
            neighbors=[neighbor("ASKU", "담당", "팀", [2])],
            documents=[neighbor_doc],
        )
        extractor = FakeExtractor([ExtractedEntity(type="장학금", name="ASKU")])

        engine(storage, extractor=extractor).answer(7, "q")

        self.assertEqual(storage.method_names().count("vector_search"), 1)
        self.assertEqual(storage.school_ids(), {7})  # 모든 조회가 요청 학교로 격리

    def test_sources_deduplicate_by_url(self) -> None:
        chunk = doc(1, title="장학금", url="https://ex.edu/1", content="c")
        norm_key = normalize_entity_key("장학금", "ASKU")
        storage = FakeStorage(
            hits=[(chunk, 0.9)],
            entities=[entity(norm_key, 201)],
            neighbors=[neighbor("ASKU", "담당", "팀", [1])],  # 이웃 근거가 top-k 청크와 같은 문서
            documents=[chunk],
        )
        extractor = FakeExtractor([ExtractedEntity(type="장학금", name="ASKU")])
        generator = FakeGenerator()

        result = engine(storage, generator=generator, extractor=extractor).answer(1, "q")

        self.assertEqual([source.url for source in result.sources], ["https://ex.edu/1"])  # 중복 제거
        self.assertIn(("get_documents", 1, ()), storage.calls)  # 이웃 근거가 이미 top-k 에 있어 추가 조회 없음
        _, context = generator.calls[0]
        self.assertIn("ASKU —담당→ 팀 (출처: https://ex.edu/1)", context)


class DocumentRagTests(unittest.TestCase):
    def test_answer_uses_pdf_only_vector_hits_with_page_citation(self) -> None:
        pdf_chunk = doc(1, title="수강편람", url="attachment:guide:abc123", content="졸업 요건 본문", source_type="pdf", page=12)
        storage = FakeStorage(hits=[(pdf_chunk, 0.9)])
        generator = FakeGenerator()

        result = DocumentRAG(storage, FakeEmbedder(), generator).answer(1, "졸업 요건이 뭐야?")

        self.assertEqual(result.answer, "생성된 답변")
        self.assertEqual(result.source_type, "document")
        self.assertEqual(result.entity_ids, [])
        _, context = generator.calls[0]
        self.assertIn("졸업 요건 본문", context)
        self.assertNotIn("[관계]", context)  # 그래프 확장 없음
        self.assertEqual(
            [(source.title, source.url) for source in result.sources],
            [("수강편람 - 12페이지", "attachment:guide:abc123")],
        )
        self.assertIn(("vector_search", 1, "pdf"), storage.calls)
        self.assertNotIn("entities_by_norm_keys", storage.method_names())
        self.assertNotIn("neighbors", storage.method_names())

    def test_no_pdf_hits_returns_no_evidence_without_generate(self) -> None:
        storage = FakeStorage(hits=[])
        generator = FakeGenerator()

        result = DocumentRAG(storage, FakeEmbedder(), generator).answer(1, "질문")

        self.assertEqual(result.answer, NO_EVIDENCE_ANSWER)
        self.assertIsNone(result.source_type)
        self.assertEqual(generator.calls, [])

    def test_hits_below_threshold_are_excluded(self) -> None:
        weak = doc(1, source_type="pdf", content="약한 근거")
        storage = FakeStorage(hits=[(weak, 0.1)])
        generator = FakeGenerator()

        result = DocumentRAG(storage, FakeEmbedder(), generator, min_similarity=0.3).answer(1, "q")

        self.assertIsNone(result.source_type)
        self.assertEqual(generator.calls, [])


class HybridRagTests(unittest.TestCase):
    def test_returns_graph_answer_when_graph_stage_succeeds(self) -> None:
        web_chunk = doc(1, source_type="web", content="웹 근거")
        pdf_chunk = doc(2, source_type="pdf", content="PDF 근거")
        storage = FakeStorage(hits=[(web_chunk, 0.9), (pdf_chunk, 0.9)])
        graph_rag = engine(storage)
        document_rag = DocumentRAG(storage, FakeEmbedder(), FakeGenerator())

        result = HybridRAG(graph_rag, document_rag).answer(1, "q")

        self.assertEqual(result.source_type, "graph")
        # 그래프 단계가 이미 답했으므로 문서 단계 vector_search(source_type="pdf")는 호출되지 않아야 한다
        self.assertNotIn(("vector_search", 1, "pdf"), storage.calls)

    def test_falls_back_to_document_stage_when_graph_stage_has_no_evidence(self) -> None:
        pdf_chunk = doc(1, title="수강편람", source_type="pdf", content="PDF 근거")
        storage = FakeStorage(hits=[(pdf_chunk, 0.9)])  # 웹 문서 없음 → 그래프 단계는 보류
        graph_rag = engine(storage)
        document_rag = DocumentRAG(storage, FakeEmbedder(), FakeGenerator())

        result = HybridRAG(graph_rag, document_rag).answer(1, "q")

        self.assertEqual(result.source_type, "document")
        self.assertIn(("vector_search", 1, "web"), storage.calls)
        self.assertIn(("vector_search", 1, "pdf"), storage.calls)

    def test_returns_final_failure_when_both_stages_have_no_evidence(self) -> None:
        storage = FakeStorage(hits=[])
        graph_rag = engine(storage)
        document_rag = DocumentRAG(storage, FakeEmbedder(), FakeGenerator())

        result = HybridRAG(graph_rag, document_rag).answer(1, "q")

        self.assertEqual(result.answer, NO_EVIDENCE_ANSWER)
        self.assertIsNone(result.source_type)
        self.assertEqual(result.sources, [])


if __name__ == "__main__":
    unittest.main()
