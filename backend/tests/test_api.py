"""Backend API 엔드포인트 테스트.

Storage·Crawler·RAG를 모킹하여 API 동작만 검증한다.
DB 없이 순수 단위 테스트로 실행된다.

NOTE: 다른 테스트 모듈(test_storage 등)이 실제 SQLAlchemy를 사용하므로,
여기서 sys.modules를 오염시키지 않는다. Neighbor dataclass는 직접 정의한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.api
from app.models import Document, Edge, Entity, School
from app.schemas import (
    BuildResult,
    ExtractionFailure,
    RagAnswer,
    Source,
)


# ── Neighbor 스텁 (app.storage import 없이) ───────────────────────────


@dataclass(frozen=True)
class Neighbor:
    """테스트용 Neighbor. app.storage.Neighbor 와 동일한 구조."""

    source: Entity
    edge: Edge
    target: Entity


# ── 팩토리 ─────────────────────────────────────────────────────────────

_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _make_school(**overrides) -> School:
    defaults = {
        "school_id": 1,
        "name": "연세대학교",
        "base_url": "https://www.yonsei.ac.kr/sc/254/subview.do",
        "crawl_schedule": "weekly",
        "status": "ready",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return School(**defaults)


def _make_entity(**overrides) -> Entity:
    defaults = {
        "entity_id": 123,
        "school_id": 1,
        "type": "장학금",
        "name": "국가장학금",
        "norm_key": "국가장학금",
        "attributes": {"마감일": "3/15", "금액": "300만원"},
        "source_doc_ids": [10, 20],
    }
    defaults.update(overrides)
    return Entity(**defaults)


def _make_document(**overrides) -> Document:
    defaults = {
        "doc_id": 10,
        "school_id": 1,
        "source_url": "https://www.yonsei.ac.kr/notice/123",
        "title": "2026 교내 장학금 안내",
        "content": "국가장학금 신청 안내...",
        "chunk_index": 0,
        "content_hash": "abc123",
        "embedding": None,
        "crawled_at": _NOW,
    }
    defaults.update(overrides)
    return Document(**defaults)


# ── 픽스처 ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_storage():
    """Storage 의 모든 공개 메서드를 모킹한다."""
    storage = MagicMock()

    # 기본 반환값 설정
    school = _make_school()
    storage.create_school.return_value = school
    storage.get_school.return_value = school
    storage.list_schools.return_value = [school]
    storage.list_schools_with_entity_counts.return_value = [(school, 1023)]
    storage.try_start_crawl.return_value = _make_school(status="crawling")
    storage.update_school_status.return_value = school
    storage.get_school_stats.return_value = {
        "document_count": 245,
        "entity_count": 1023,
        "last_crawled_at": _NOW,
    }
    storage.create_schema.return_value = None
    storage.close.return_value = None

    # 그래프/엔티티 관련
    entity1 = _make_entity()
    entity2 = _make_entity(entity_id=45, name="학생지원팀", type="부서", norm_key="학생지원팀", source_doc_ids=[10])
    edge1 = Edge(
        edge_id=1,
        school_id=1,
        source_entity_id=123,
        target_entity_id=45,
        relation="담당",
        source_doc_ids=[10],
    )
    storage.get_entities_for_graph.return_value = [entity1, entity2]
    storage.get_edges_for_graph.return_value = [edge1]
    storage.get_entity.return_value = entity1
    storage.get_entity_neighbors.return_value = [
        Neighbor(source=entity1, edge=edge1, target=entity2)
    ]
    storage.get_entity_sources.return_value = [_make_document()]

    return storage


@pytest.fixture
def client(mock_storage):
    """모킹된 Storage로 FastAPI TestClient를 반환한다."""
    with patch("app.api._get_storage", return_value=mock_storage):
        from app.api import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ── POST /schools ──────────────────────────────────────────────────────


class TestCreateSchool:
    def test_201_created(self, client, mock_storage):
        resp = client.post("/schools", json={
            "name": "연세대학교",
            "base_url": "https://www.yonsei.ac.kr/sc/254/subview.do",
            "crawl_schedule": "weekly",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["school_id"] == 1
        assert body["name"] == "연세대학교"
        mock_storage.create_school.assert_called_once()

    def test_422_invalid_url(self, client):
        resp = client.post("/schools", json={
            "name": "테스트대학교",
            "base_url": "not-a-url",
        })
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "INVALID_URL"

    def test_missing_required_fields_returns_400_invalid_request(self, client):
        """필수 필드 누락 시 RequestValidationError → 400 INVALID_REQUEST."""
        resp = client.post("/schools", json={"name": "테스트대학교"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "INVALID_REQUEST"
        assert body["error"]["details"] is not None
        assert "errors" in body["error"]["details"]


# ── GET /schools ───────────────────────────────────────────────────────


class TestListSchools:
    def test_200_list_all(self, client, mock_storage):
        resp = client.get("/schools")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["schools"]) == 1
        assert body["schools"][0]["name"] == "연세대학교"
        assert body["schools"][0]["entity_count"] == 1023

    def test_200_with_query(self, client, mock_storage):
        resp = client.get("/schools?query=연세")
        assert resp.status_code == 200
        mock_storage.list_schools_with_entity_counts.assert_called_with(query="연세")


# ── GET /schools/{id} ──────────────────────────────────────────────────


class TestGetSchool:
    def test_200_school_detail(self, client, mock_storage):
        resp = client.get("/schools/1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["school_id"] == 1
        assert body["stats"]["document_count"] == 245

    def test_404_not_found(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.get("/schools/999")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "SCHOOL_NOT_FOUND"


# ── POST /schools/{id}/query ──────────────────────────────────────────


class TestQuerySchool:
    def test_200_answer(self, client, mock_storage):
        rag_answer = RagAnswer(
            answer="성적우수 장학금 마감일은 2026년 3월 15일입니다.",
            sources=[Source(title="장학금 안내", url="https://example.com/notice")],
            entity_ids=["e_123"],
        )
        with patch("app.api._get_rag_engine") as mock_rag:
            mock_rag.return_value.answer.return_value = rag_answer
            resp = client.post("/schools/1/query", json={"question": "장학금 마감일?"})

        assert resp.status_code == 200
        body = resp.json()
        assert "마감일" in body["answer"]
        assert len(body["sources"]) == 1
        assert body["entity_ids"] == ["e_123"]

    def test_503_not_ready(self, client, mock_storage):
        mock_storage.get_school.return_value = _make_school(status="crawling")
        resp = client.post("/schools/1/query", json={"question": "질문"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "SCHOOL_NOT_READY"

    def test_404_school_not_found(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.post("/schools/999/query", json={"question": "질문"})
        assert resp.status_code == 404

    def test_missing_question_returns_400(self, client):
        """question 필드 누락 시 400 INVALID_REQUEST."""
        resp = client.post("/schools/1/query", json={})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "INVALID_REQUEST"


# ── POST /schools/{id}/recrawl ─────────────────────────────────────────


class TestRecrawl:
    def test_202_accepted(self, client, mock_storage):
        resp = client.post("/schools/1/recrawl")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "crawling"
        assert body["message"] == "재크롤링이 시작되었습니다."

    def test_409_crawl_in_progress(self, client, mock_storage):
        # try_start_crawl 실패 (이미 진행 중)
        mock_storage.try_start_crawl.return_value = None
        mock_storage.get_school.return_value = _make_school(status="crawling")
        resp = client.post("/schools/1/recrawl")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "CRAWL_IN_PROGRESS"

    def test_404_not_found(self, client, mock_storage):
        mock_storage.try_start_crawl.return_value = None
        mock_storage.get_school.return_value = None
        resp = client.post("/schools/999/recrawl")
        assert resp.status_code == 404


# ── GET /schools/{id}/status ───────────────────────────────────────────


class TestGetStatus:
    def test_200_status(self, client, mock_storage):
        mock_storage.get_school.return_value = _make_school(status="indexing")
        resp = client.get("/schools/1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "indexing"
        assert "progress" in body

    def test_404_not_found(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.get("/schools/999/status")
        assert resp.status_code == 404


# ── GET /schools/{id}/graph ────────────────────────────────────────────


class TestGetGraph:
    def test_200_graph(self, client, mock_storage):
        resp = client.get("/schools/1/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) == 2
        assert len(body["edges"]) == 1
        assert body["edges"][0]["relation"] == "담당"
        # 노드 ID는 e_ 접두사
        assert body["nodes"][0]["id"].startswith("e_")

    def test_404_not_found(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.get("/schools/999/graph")
        assert resp.status_code == 404


# ── GET /schools/{id}/entities/{eid} ──────────────────────────────────


class TestGetEntityDetail:
    def test_200_entity_detail(self, client, mock_storage):
        resp = client.get("/schools/1/entities/123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "e_123"
        assert body["type"] == "장학금"
        assert body["name"] == "국가장학금"
        assert len(body["sources"]) == 1
        assert len(body["neighbors"]) == 1
        assert body["neighbors"][0]["relation"] == "담당"

    def test_404_entity_not_found(self, client, mock_storage):
        mock_storage.get_entity.return_value = None
        resp = client.get("/schools/1/entities/999")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_404_school_not_found(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.get("/schools/999/entities/123")
        assert resp.status_code == 404


# ── 백그라운드 파이프라인 테스트 ────────────────────────────────────────


class TestRunCrawlPipeline:
    """_run_crawl 함수의 실제 연동 계약을 검증한다."""

    def test_pipeline_calls_correct_interfaces(self, mock_storage):
        """DocumentExtractor.process → GraphBuilder.build(school_id, chunk) 순서로 호출."""
        from app.schemas import ExtractedChunk

        chunk = ExtractedChunk(
            school_id=1,
            source_url="https://example.com/notice",
            title="테스트",
            content="테스트 본문",
            chunk_index=0,
            content_hash="hash123",
            entities=[],
            relations=[],
            extraction_status="complete",
        )
        build_result = BuildResult(
            school_id=1,
            source_url="https://example.com/notice",
            content_hash="hash123",
            doc_id=1,
            status="complete",
        )

        mock_extractor = MagicMock()
        mock_extractor.process.return_value = [chunk]

        mock_builder = MagicMock()
        mock_builder.build.return_value = build_result

        mock_crawler = MagicMock()
        mock_run = MagicMock()
        mock_run.failures = []
        mock_page = MagicMock()
        mock_page.source_url = "https://example.com/notice"
        mock_crawler.crawl.return_value = mock_run
        mock_crawler.pages_for_extractor.return_value = [mock_page]

        with (
            patch("app.api._get_storage", return_value=mock_storage),
            patch("app.api.Crawler", create=True) as MockCrawlerClass,
            patch("app.crawler.Crawler") as MockCrawlerModule,
            patch("app.crawler.CommonNoticeAdapter", return_value=MagicMock()),
            patch("app.extractor.DocumentExtractor", return_value=mock_extractor),
            patch("app.graph_builder.GraphBuilder", return_value=mock_builder),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.llm.LocalEmbedder", return_value=MagicMock()),
        ):
            MockCrawlerModule.from_storage.return_value = mock_crawler

            from app.api import _run_crawl
            _run_crawl(1, "https://example.com", "initial")

        # process 가 호출되었는지 (extract 가 아니라)
        mock_extractor.process.assert_called_once_with(mock_page)
        # build(school_id, chunk) 시그니처
        mock_builder.build.assert_called_once_with(1, chunk)
        # 성공 시 ready
        mock_storage.update_school_status.assert_any_call(1, "ready")

    def test_build_failure_triggers_partial_failed(self, mock_storage):
        """BuildResult(status='failed')이면 partial_failed 상태가 된다."""
        from app.schemas import ExtractedChunk

        chunk = ExtractedChunk(
            school_id=1,
            source_url="https://example.com/notice",
            content="본문",
            chunk_index=0,
            content_hash="hash123",
            entities=[],
            relations=[],
            extraction_status="complete",
        )
        failed_result = BuildResult(
            school_id=1,
            source_url="https://example.com/notice",
            content_hash="hash123",
            status="failed",
            error_code="EMBED_ERROR",
        )

        mock_extractor = MagicMock()
        mock_extractor.process.return_value = [chunk]

        mock_builder = MagicMock()
        mock_builder.build.return_value = failed_result

        mock_crawler = MagicMock()
        mock_run = MagicMock()
        mock_run.failures = []
        mock_crawler.crawl.return_value = mock_run
        mock_crawler.pages_for_extractor.return_value = [MagicMock(source_url="url")]

        with (
            patch("app.api._get_storage", return_value=mock_storage),
            patch("app.crawler.Crawler") as MockCrawlerModule,
            patch("app.crawler.CommonNoticeAdapter", return_value=MagicMock()),
            patch("app.extractor.DocumentExtractor", return_value=mock_extractor),
            patch("app.graph_builder.GraphBuilder", return_value=mock_builder),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.llm.LocalEmbedder", return_value=MagicMock()),
        ):
            MockCrawlerModule.from_storage.return_value = mock_crawler

            from app.api import _run_crawl
            _run_crawl(1, "https://example.com", "initial")

        # 빌드 실패 → partial_failed
        mock_storage.update_school_status.assert_any_call(1, "partial_failed")

    def test_extraction_failure_triggers_partial_failed(self, mock_storage):
        """ExtractionFailure 반환 시 partial_failed 상태가 된다."""
        extraction_failure = ExtractionFailure(
            school_id=1,
            source_url="https://example.com/notice",
            content_hash="hash123",
            error_code="EMPTY_BODY",
            retryable=False,
        )

        mock_extractor = MagicMock()
        mock_extractor.process.return_value = extraction_failure

        mock_crawler = MagicMock()
        mock_run = MagicMock()
        mock_run.failures = []
        mock_crawler.crawl.return_value = mock_run
        mock_crawler.pages_for_extractor.return_value = [MagicMock(source_url="url")]

        with (
            patch("app.api._get_storage", return_value=mock_storage),
            patch("app.crawler.Crawler") as MockCrawlerModule,
            patch("app.crawler.CommonNoticeAdapter", return_value=MagicMock()),
            patch("app.extractor.DocumentExtractor", return_value=mock_extractor),
            patch("app.graph_builder.GraphBuilder", return_value=MagicMock()),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.llm.LocalEmbedder", return_value=MagicMock()),
        ):
            MockCrawlerModule.from_storage.return_value = mock_crawler

            from app.api import _run_crawl
            _run_crawl(1, "https://example.com", "initial")

        # 추출 실패 → partial_failed
        mock_storage.update_school_status.assert_any_call(1, "partial_failed")
