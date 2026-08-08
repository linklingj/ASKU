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
from app.models import Attachment, Document, Edge, Entity, School
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


def _make_attachment(**overrides) -> Attachment:
    defaults = {
        "attachment_id": 7,
        "school_id": 1,
        "filename": "수강편람.pdf",
        "content_type": "application/pdf",
        "byte_size": 1024,
        "file_hash": "hash",
        "status": "pending",
        "uploaded_at": _NOW,
    }
    defaults.update(overrides)
    return Attachment(**defaults)


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
        "attachment_count": 2,
        "last_crawled_at": _NOW,
    }
    storage.create_schema.return_value = None
    storage.close.return_value = None

    # 첨부(사용자 업로드) 기본값 — 첨부 없음
    storage.count_ready_attachments.return_value = 0
    storage.list_attachments.return_value = []
    storage.create_attachment.side_effect = lambda **kwargs: _make_attachment(**kwargs)
    storage.delete_attachment.return_value = True

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


# ── 관리자 인증·학교 관리 ──────────────────────────────────────────────


class TestAdminSchoolManagement:
    @pytest.fixture
    def admin_headers(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
        monkeypatch.setenv("ADMIN_TOKEN_SECRET", "test-secret")
        response = client.post("/admin/login", json={"password": "test-password"})
        assert response.status_code == 200
        return {"Authorization": "Bearer " + response.json()["token"]}

    def test_login_rejects_incorrect_password(self, client):
        with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-password", "ADMIN_TOKEN_SECRET": "test-secret"}):
            response = client.post("/admin/login", json={"password": "incorrect"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_ADMIN_CREDENTIALS"

    def test_update_requires_admin_token(self, client):
        with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-password", "ADMIN_TOKEN_SECRET": "test-secret"}):
            response = client.patch("/schools/1", json={"image_url": "https://example.com/logo.png"})
        assert response.status_code == 401

    def test_admin_can_update_image_url(self, client, mock_storage, admin_headers):
        mock_storage.update_school.return_value = _make_school(image_url="https://example.com/logo.png")
        response = client.patch(
            "/schools/1", json={"image_url": "https://example.com/logo.png"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["image_url"] == "https://example.com/logo.png"
        mock_storage.update_school.assert_called_once_with(1, image_url="https://example.com/logo.png")

    def test_admin_can_delete_school(self, client, mock_storage, admin_headers):
        mock_storage.delete_school.return_value = True
        response = client.delete("/schools/1", headers=admin_headers)
        assert response.status_code == 204
        mock_storage.delete_school.assert_called_once_with(1)

    def test_delete_rejects_school_with_active_crawl(self, client, mock_storage, admin_headers):
        mock_storage.get_school.return_value = _make_school(status="crawling")
        response = client.delete("/schools/1", headers=admin_headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CRAWL_IN_PROGRESS"
        mock_storage.delete_school.assert_not_called()


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

    def test_source_type_is_passed_through(self, client, mock_storage):
        """어느 단계(그래프·문서)가 답을 냈는지 응답에 그대로 노출한다."""
        rag_answer = RagAnswer(
            answer="졸업 요건은 130학점입니다.",
            sources=[Source(title="수강편람.pdf - 12페이지", url="attachment://7")],
            source_type="document",
        )
        with patch("app.api._get_rag_engine") as mock_rag:
            mock_rag.return_value.answer.return_value = rag_answer
            resp = client.post("/schools/1/query", json={"question": "졸업 학점?"})

        assert resp.status_code == 200
        assert resp.json()["source_type"] == "document"

    def test_no_evidence_answer_reports_null_source_type(self, client, mock_storage):
        with patch("app.api._get_rag_engine") as mock_rag:
            mock_rag.return_value.answer.return_value = RagAnswer(answer="해당 정보를 찾지 못했습니다.")
            resp = client.post("/schools/1/query", json={"question": "질문"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["source_type"] is None
        assert body["sources"] == []

    def test_503_not_ready(self, client, mock_storage):
        mock_storage.get_school.return_value = _make_school(status="crawling")
        resp = client.post("/schools/1/query", json={"question": "질문"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "SCHOOL_NOT_READY"

    def test_ready_attachment_opens_query_before_crawl_finishes(self, client, mock_storage):
        """크롤링이 끝나지 않아도 색인된 첨부가 있으면 문서 RAG 로 답할 수 있다."""
        mock_storage.get_school.return_value = _make_school(status="failed")
        mock_storage.count_ready_attachments.return_value = 1
        with patch("app.api._get_rag_engine") as mock_rag:
            mock_rag.return_value.answer.return_value = RagAnswer(answer="답", source_type="document")
            resp = client.post("/schools/1/query", json={"question": "질문"})

        assert resp.status_code == 200

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

    def test_empty_crawl_without_failures_triggers_failed(self, mock_storage):
        """수집 페이지가 0건이고 실패 기록도 없으면 (URL 오입력·리다이렉트·빈 목록) failed 로 전이한다."""
        mock_crawler = MagicMock()
        mock_run = MagicMock()
        mock_run.failures = []  # 실패 기록 없음
        mock_run.pages = []
        mock_crawler.crawl.return_value = mock_run
        mock_crawler.pages_for_extractor.return_value = []  # 추출할 페이지 0건

        with (
            patch("app.api._get_storage", return_value=mock_storage),
            patch("app.crawler.Crawler") as MockCrawlerModule,
            patch("app.crawler.CommonNoticeAdapter", return_value=MagicMock()),
            patch("app.extractor.DocumentExtractor", return_value=MagicMock()),
            patch("app.graph_builder.GraphBuilder", return_value=MagicMock()),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.llm.LocalEmbedder", return_value=MagicMock()),
        ):
            MockCrawlerModule.from_storage.return_value = mock_crawler

            from app.api import _run_crawl
            _run_crawl(1, "https://example.com", "initial")

        # 0페이지 → failed (ready·indexing 로 새면 안 됨)
        mock_storage.update_school_status.assert_called_once_with(1, "failed")


# ── 첨부 문서 (POST/GET/DELETE /schools/{id}/attachments) ──────────────


class TestUploadAttachments:
    def test_202_accepted_and_ingest_scheduled(self, client, mock_storage):
        with patch("app.api._run_attachment_ingest") as mock_ingest:
            resp = client.post(
                "/schools/1/attachments",
                files=[("files", ("수강편람.pdf", b"%PDF-1.4 fake", "application/pdf"))],
            )

        assert resp.status_code == 202
        body = resp.json()
        assert [item["filename"] for item in body["accepted"]] == ["수강편람.pdf"]
        assert body["accepted"][0]["status"] == "pending"
        assert body["rejected"] == []
        # 파싱·임베딩은 응답을 막지 않고 백그라운드에서 이어진다
        mock_ingest.assert_called_once()
        attachment, data = mock_ingest.call_args.args
        assert attachment.filename == "수강편람.pdf"
        assert data == b"%PDF-1.4 fake"

    def test_file_hash_is_derived_from_bytes(self, client, mock_storage):
        """같은 파일을 다시 올리면 같은 해시 → 저장소가 기존 첨부를 재사용한다."""
        from hashlib import sha256

        payload = b"%PDF-1.4 fake"
        with patch("app.api._run_attachment_ingest"):
            client.post("/schools/1/attachments", files=[("files", ("a.pdf", payload, "application/pdf"))])

        kwargs = mock_storage.create_attachment.call_args.kwargs
        assert kwargs["file_hash"] == sha256(payload).hexdigest()
        assert kwargs["byte_size"] == len(payload)
        assert kwargs["school_id"] == 1

    def test_supported_file_is_accepted_while_unsupported_one_is_rejected(self, client, mock_storage):
        """한 파일이 걸려도 나머지는 계속 처리한다."""
        with patch("app.api._run_attachment_ingest") as mock_ingest:
            resp = client.post(
                "/schools/1/attachments",
                files=[
                    ("files", ("학칙.hwp", b"hwp-bytes", "application/x-hwp")),
                    ("files", ("사진.png", b"png-bytes", "image/png")),
                ],
            )

        assert resp.status_code == 202
        body = resp.json()
        assert [item["filename"] for item in body["accepted"]] == ["학칙.hwp"]
        assert body["rejected"][0]["filename"] == "사진.png"
        assert body["rejected"][0]["code"] == "UNSUPPORTED_FILE_TYPE"
        assert mock_ingest.call_count == 1

    def test_415_when_nothing_is_processable(self, client, mock_storage):
        resp = client.post(
            "/schools/1/attachments",
            files=[("files", ("사진.png", b"png-bytes", "image/png"))],
        )

        assert resp.status_code == 415
        body = resp.json()
        assert body["error"]["code"] == "NO_SUPPORTED_ATTACHMENT"
        mock_storage.create_attachment.assert_not_called()

    def test_empty_file_is_rejected(self, client, mock_storage):
        resp = client.post("/schools/1/attachments", files=[("files", ("빈.pdf", b"", "application/pdf"))])

        assert resp.status_code == 415
        assert resp.json()["error"]["details"]["rejected"][0]["code"] == "EMPTY_FILE"

    def test_oversized_file_is_rejected(self, client, mock_storage):
        from app.api import MAX_ATTACHMENT_BYTES

        oversized = b"a" * (MAX_ATTACHMENT_BYTES + 1)
        resp = client.post("/schools/1/attachments", files=[("files", ("큰.pdf", oversized, "application/pdf"))])

        assert resp.status_code == 415
        assert resp.json()["error"]["details"]["rejected"][0]["code"] == "FILE_TOO_LARGE"

    def test_404_when_school_missing(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        resp = client.post("/schools/999/attachments", files=[("files", ("a.pdf", b"x", "application/pdf"))])

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCHOOL_NOT_FOUND"


class TestListAttachments:
    def test_200_lists_attachments_with_index_state(self, client, mock_storage):
        mock_storage.list_attachments.return_value = [
            _make_attachment(status="ready", page_count=120, chunk_count=340),
            _make_attachment(attachment_id=8, filename="학칙.hwp", status="failed", error_code="HWP_ENCRYPTED"),
        ]

        resp = client.get("/schools/1/attachments")

        assert resp.status_code == 200
        items = resp.json()["attachments"]
        assert [item["status"] for item in items] == ["ready", "failed"]
        assert items[0]["chunk_count"] == 340
        assert items[1]["error_code"] == "HWP_ENCRYPTED"

    def test_404_when_school_missing(self, client, mock_storage):
        mock_storage.get_school.return_value = None
        assert client.get("/schools/999/attachments").status_code == 404


class TestDeleteAttachment:
    def test_204_deletes_attachment_and_chunks(self, client, mock_storage):
        resp = client.delete("/schools/1/attachments/7")

        assert resp.status_code == 204
        mock_storage.delete_attachment.assert_called_once_with(1, 7)

    def test_404_when_attachment_missing(self, client, mock_storage):
        mock_storage.delete_attachment.return_value = False
        resp = client.delete("/schools/1/attachments/999")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ATTACHMENT_NOT_FOUND"
