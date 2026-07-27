from datetime import datetime, timezone
import unittest

from app.graph_builder import GraphBuilder, normalize_entity_key
from app.llm import Embedder
from app.models import EMBEDDING_DIM
from app.schemas import ExtractedChunk, ExtractedEntity, ExtractedRelation


class FakeEmbedder(Embedder):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        if self.error:
            raise self.error
        return [0.0] * EMBEDDING_DIM


class FakeStorage:
    def __init__(self, *, document_error: Exception | None = None, entity_errors: set[str] | None = None,
                 edge_error: Exception | None = None) -> None:
        self.document_error = document_error
        self.entity_errors = entity_errors or set()
        self.edge_error = edge_error
        self.calls: list[tuple] = []
        self.entities: dict[str, int] = {}
        self.edges: dict[tuple[int, int, str], int] = {}

    def upsert_document(self, *args, **kwargs) -> int:
        self.calls.append(("document", args, kwargs))
        if self.document_error:
            raise self.document_error
        return 101

    def upsert_entity(self, school_id, type, name, norm_key, attributes, source_doc_ids) -> int:
        self.calls.append(("entity", school_id, type, name, norm_key, attributes, source_doc_ids))
        if name in self.entity_errors:
            raise RuntimeError("entity down")
        return self.entities.setdefault(norm_key, len(self.entities) + 201)

    def upsert_edge(self, school_id, source_entity_id, target_entity_id, relation, source_doc_ids) -> int:
        self.calls.append(("edge", school_id, source_entity_id, target_entity_id, relation, source_doc_ids))
        if self.edge_error:
            raise self.edge_error
        key = (source_entity_id, target_entity_id, relation)
        return self.edges.setdefault(key, len(self.edges) + 301)


def chunk(*, school_id: int = 1, status: str = "complete", entities=None, relations=None) -> ExtractedChunk:
    return ExtractedChunk(
        school_id=school_id,
        source_url="https://example.edu/notices/1",
        title="장학금 안내",
        content="장학금 신청 본문",
        chunk_index=0,
        content_hash="hash",
        crawled_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        entities=entities or [
            ExtractedEntity(type="공지", name="장학금 안내"),
            ExtractedEntity(type="장학금", name="ASKU 장학금", attributes={"금액": "100만원"}),
        ],
        relations=relations or [ExtractedRelation(source="장학금 안내", relation="안내", target="ASKU 장학금")],
        extraction_status=status,
    )


class GraphBuilderTests(unittest.TestCase):
    def test_normalize_key_includes_type_and_ignores_spacing_punctuation_case(self) -> None:
        self.assertEqual(normalize_entity_key("부서·기관", "학생 생활-상담소"), "부서기관:학생생활상담소")
        self.assertNotEqual(normalize_entity_key("부서", "상담소"), normalize_entity_key("시설", "상담소"))

    def test_build_embeds_and_persists_document_entities_and_edge_with_evidence(self) -> None:
        storage, embedder = FakeStorage(), FakeEmbedder()
        result = GraphBuilder(storage, embedder).build(1, chunk())

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.doc_id, 101)
        self.assertEqual(result.entity_ids, [201, 202])
        self.assertEqual(result.edge_ids, [301])
        self.assertEqual(embedder.inputs, ["장학금 신청 본문"])
        self.assertEqual([call[0] for call in storage.calls], ["document", "entity", "entity", "edge"])
        self.assertEqual(storage.calls[1][-1], [101])
        self.assertEqual(storage.calls[-1][-1], [101])

    def test_same_normalized_entity_is_merged_and_relation_uses_the_same_id(self) -> None:
        entities = [
            ExtractedEntity(type="공지", name="공지"),
            ExtractedEntity(type="부서·기관", name="학생생활상담소"),
            ExtractedEntity(type="부서·기관", name="학생 생활 상담소"),
        ]
        relations = [ExtractedRelation(source="공지", relation="게시", target="학생 생활 상담소")]
        storage = FakeStorage()
        result = GraphBuilder(storage, FakeEmbedder()).build(1, chunk(entities=entities, relations=relations))

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.entity_ids, [201, 202])
        self.assertEqual(storage.calls[-1][2:5], (201, 202, "게시"))

    def test_ambiguous_relation_endpoint_is_skipped_as_partial(self) -> None:
        entities = [
            ExtractedEntity(type="공지", name="공지"),
            ExtractedEntity(type="부서·기관", name="상담소"),
            ExtractedEntity(type="시설", name="상담소"),
        ]
        relations = [ExtractedRelation(source="공지", relation="안내", target="상담소")]
        storage = FakeStorage()
        result = GraphBuilder(storage, FakeEmbedder()).build(1, chunk(entities=entities, relations=relations))

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.edge_ids, [])
        self.assertTrue(any("여러 타입" in warning for warning in result.warnings))

    def test_partial_input_and_entity_failure_keep_successful_data(self) -> None:
        storage = FakeStorage(entity_errors={"ASKU 장학금"})
        result = GraphBuilder(storage, FakeEmbedder()).build(1, chunk(status="partial"))

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.doc_id, 101)
        self.assertEqual(result.entity_ids, [201])
        self.assertEqual(result.edge_ids, [])
        self.assertTrue(any("입력 ExtractedChunk" in warning for warning in result.warnings))

    def test_edge_failure_and_invalid_entity_keep_document_as_partial(self) -> None:
        entities = [
            ExtractedEntity(type="공지", name="공지"),
            ExtractedEntity(type="장학금", name="장학금"),
            ExtractedEntity(type="", name="잘못된 엔티티"),
        ]
        relations = [ExtractedRelation(source="공지", relation="안내", target="장학금")]
        storage = FakeStorage(edge_error=RuntimeError("edge down"))
        result = GraphBuilder(storage, FakeEmbedder()).build(1, chunk(entities=entities, relations=relations))

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.doc_id, 101)
        self.assertEqual(result.entity_ids, [201, 202])
        self.assertEqual(result.edge_ids, [])
        self.assertTrue(any("빈 타입" in warning for warning in result.warnings))
        self.assertTrue(any("edge:공지-안내->장학금" in warning for warning in result.warnings))

    def test_embedding_or_document_failure_returns_failed_result(self) -> None:
        embedding_failed = GraphBuilder(FakeStorage(), FakeEmbedder(RuntimeError("offline"))).build(1, chunk())
        document_failed = GraphBuilder(FakeStorage(document_error=RuntimeError("db down")), FakeEmbedder()).build(1, chunk())

        self.assertEqual((embedding_failed.status, embedding_failed.error_code), ("failed", "EMBEDDING_FAILED"))
        self.assertEqual((document_failed.status, document_failed.error_code), ("failed", "DOCUMENT_STORE_FAILED"))

    def test_school_id_mismatch_is_rejected_before_embedding(self) -> None:
        embedder = FakeEmbedder()
        result = GraphBuilder(FakeStorage(), embedder).build(2, chunk(school_id=1))

        self.assertEqual((result.status, result.error_code), ("failed", "SCHOOL_ID_MISMATCH"))
        self.assertEqual(embedder.inputs, [])


if __name__ == "__main__":
    unittest.main()
