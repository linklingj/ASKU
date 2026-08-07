"""Storage의 DB 독립 계약 테스트.

Postgres/pgvector 통합 테스트는 DATABASE_URL이 구성된 환경에서 별도로 실행한다.
"""

import sys
import unittest
from unittest.mock import MagicMock

for _m in [
    "psycopg", "psycopg.rows", "psycopg_binary", "psycopg.adapt"
]:
    try:
        __import__(_m)
    except ImportError:
        mock_mod = MagicMock()
        mock_mod.__version__ = "3.1.0"
        mock_mod.adapt = MagicMock()
        sys.modules[_m] = mock_mod

from app.models import EMBEDDING_DIM, SOURCE_TYPE_WEB
from app.storage import (
    Storage,
    _assert_entities_belong_to_school,
    _merge_ids,
    _validate_embedding,
    attachments,
    documents,
    edges,
    entities,
)


class StorageContractTests(unittest.TestCase):
    def test_source_document_ids_are_merged_without_duplicates(self) -> None:
        self.assertEqual(_merge_ids([3, 1], [2, 3]), [1, 2, 3])

    def test_embedding_dimension_is_enforced(self) -> None:
        _validate_embedding([0.0] * EMBEDDING_DIM)

        with self.assertRaisesRegex(ValueError, str(EMBEDDING_DIM)):
            _validate_embedding([0.0])

    def test_schema_contains_the_required_unique_constraints(self) -> None:
        document_constraints = {constraint.name for constraint in documents.constraints}
        entity_constraints = {constraint.name for constraint in entities.constraints}
        edge_constraints = {constraint.name for constraint in edges.constraints}

        self.assertIn("uq_documents_school_url_hash_chunk", document_constraints)
        self.assertIn("uq_entities_school_norm_key", entity_constraints)
        self.assertIn("uq_edges_school_source_target_relation", edge_constraints)
        # 같은 학교에 같은 파일을 다시 올려도 첨부 행이 늘지 않는다
        self.assertIn(
            "uq_attachments_school_file_hash",
            {constraint.name for constraint in attachments.constraints},
        )

    def test_documents_carry_attachment_provenance(self) -> None:
        """첨부 청크는 검색 풀(source_type)·인용 페이지·소속 첨부를 함께 남긴다."""

        self.assertIn("source_type", documents.c)
        self.assertIn("page", documents.c)
        self.assertIn("attachment_id", documents.c)
        # 기존 크롤링 청크가 마이그레이션 후에도 그래프 RAG 풀에 남도록 기본값은 'web'
        self.assertIn(f"'{SOURCE_TYPE_WEB}'", str(documents.c.source_type.server_default.arg))
        self.assertFalse(documents.c.source_type.nullable)

    def test_school_scoped_queries_handle_empty_or_invalid_input_without_a_database(self) -> None:
        storage = Storage("postgresql+psycopg://asku:asku@localhost:5432/asku")
        try:
            self.assertEqual(storage.get_documents(1, []), [])
            self.assertEqual(storage.neighbors(1, []), [])
            with self.assertRaisesRegex(ValueError, "1-hop"):
                storage.neighbors(1, [1], hops=2)
        finally:
            storage.close()

    def test_edge_endpoints_must_belong_to_the_requested_school(self) -> None:
        _assert_entities_belong_to_school(1, [10, 20], [20, 10])

        with self.assertRaisesRegex(ValueError, "school_id=1"):
            _assert_entities_belong_to_school(1, [10, 20], [10])
