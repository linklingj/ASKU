"""Backend API 엔드포인트 테스트.

Storage·Crawler·RAG를 모킹하여 API 동작만 검증한다.
DB 없이 순수 단위 테스트로 실행된다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import sys
from unittest.mock import MagicMock

# pgvector·SQLAlchemy가 설치되지 않은 환경에서도 테스트가 돌도록
# import 전에 스텁 모듈을 등록한다.
for _mod in (
    "pgvector", "pgvector.sqlalchemy",
    "sqlalchemy", "sqlalchemy.dialects", "sqlalchemy.dialects.postgresql",
    "sqlalchemy.engine",
):
    sys.modules.setdefault(_mod, MagicMock())

import pytest
from fastapi.testclient import TestClient

from app.models import Document, Edge, Entity, School
from app.schemas import RagAnswer, Source
from app.storage import Neighbor


# ── 픽스처 ─────────────────────────────────────────────────────────────

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


@pytest.fixture
def mock_storage():
    """Storage 의 모든 공개 메서드를 모킹한다."""
    storage = MagicMock()

    # 기본 반환값 설정
    school = _make_school()
    storage.create_school.return_value = school
    storage.get_school.return_value = school
    storage.list_schools.return_value = [school]
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

        with TestClient(app) as c:
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

    def test_missing_required_fields(self, client):
        resp = client.post("/schools", json={"name": "테스트대학교"})
        assert resp.status_code == 422


# ── GET /schools ───────────────────────────────────────────────────────


class TestListSchools:
    def test_200_list_all(self, client, mock_storage):
        resp = client.get("/schools")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["schools"]) == 1
        assert body["schools"][0]["name"] == "연세대학교"

    def test_200_with_query(self, client, mock_storage):
        resp = client.get("/schools?query=연세")
        assert resp.status_code == 200
        mock_storage.list_schools.assert_called_with(query="연세")


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
        )
        with patch("app.api._get_rag_engine") as mock_rag:
            mock_rag.return_value.answer.return_value = rag_answer
            resp = client.post("/schools/1/query", json={"question": "장학금 마감일?"})

        assert resp.status_code == 200
        body = resp.json()
        assert "마감일" in body["answer"]
        assert len(body["sources"]) == 1

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


# ── POST /schools/{id}/recrawl ─────────────────────────────────────────


class TestRecrawl:
    def test_202_accepted(self, client, mock_storage):
        resp = client.post("/schools/1/recrawl")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "crawling"
        assert body["message"] == "재크롤링이 시작되었습니다."

    def test_409_crawl_in_progress(self, client, mock_storage):
        mock_storage.get_school.return_value = _make_school(status="crawling")
        resp = client.post("/schools/1/recrawl")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "CRAWL_IN_PROGRESS"

    def test_404_not_found(self, client, mock_storage):
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
