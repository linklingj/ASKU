"""백엔드 API — 프론트엔드와 내부 시스템을 잇는 유일한 진입점(FastAPI).

내부 기능(크롤러·Graph RAG·저장소)을 REST 엔드포인트로 묶어 위임한다.
비즈니스 로직은 직접 구현하지 않는다.

설계 문서: docs/01_SYSTEM/01_backend-api.md
이슈: https://github.com/linklingj/ASKU/issues/15
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.models import School
from app.schemas import (
    CrawlRequest,
    EntityDetailResponse,
    EntityNeighbor,
    ErrorDetail,
    ErrorResponse,
    GraphEdge,
    GraphNode,
    GraphResponse,
    QueryRequest,
    QueryResponse,
    RecrawlResponse,
    SchoolCreateRequest,
    SchoolDetailResponse,
    SchoolDetailStats,
    SchoolListItem,
    SchoolListResponse,
    SchoolResponse,
    StatusProgress,
    StatusResponse,
)

logger = logging.getLogger(__name__)

# ── 의존성 (지연 초기화) ──────────────────────────────────────────────

_storage = None
_crawler = None
_rag_engine = None


def _get_storage():
    """Storage 싱글턴을 반환한다. 앱 시작 시 초기화된다."""
    global _storage
    if _storage is None:
        from app.storage import Storage

        _storage = Storage.from_env()
    return _storage


def _get_rag_engine():
    """GraphRAG 엔진 싱글턴을 반환한다. 최초 호출 시 초기화."""
    global _rag_engine
    if _rag_engine is None:
        from app.llm import GeminiProvider, LocalEmbedder
        from app.rag import GraphRAG

        storage = _get_storage()
        embedder = LocalEmbedder()
        provider = GeminiProvider()
        _rag_engine = GraphRAG(
            storage=storage,
            embedder=embedder,
            extractor=provider,
            generator=provider,
        )
    return _rag_engine


# ── 공통 에러 응답 헬퍼 ───────────────────────────────────────────────


def _error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    """공통 에러 형식 {error: {code, message, details}} 으로 응답한다."""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _school_not_found(school_id: int) -> JSONResponse:
    return _error_response(404, "SCHOOL_NOT_FOUND", "해당 학교를 찾을 수 없습니다.")


# ── 비동기 크롤링 백그라운드 태스크 ────────────────────────────────────


def _run_crawl(school_id: int, base_url: str, mode: str) -> None:
    """백그라운드에서 크롤링 파이프라인을 실행한다.

    crawling → indexing → ready 상태 전이를 추적한다.
    실패 시 failed 또는 partial_failed 로 전이한다.
    """
    storage = _get_storage()
    try:
        from app.crawler import CommonNoticeAdapter, Crawler
        from app.extractor import Extractor as ChunkExtractor
        from app.graph_builder import GraphBuilder

        # 1. 크롤링
        crawl_request = CrawlRequest(
            crawl_id=uuid4(),
            school_id=school_id,
            base_url=base_url,
            mode=mode,
        )
        crawler = Crawler.from_storage(storage)
        adapter = CommonNoticeAdapter()
        run = crawler.crawl(crawl_request, adapter)
        pages = crawler.pages_for_extractor(run)

        if not pages and run.failures:
            storage.update_school_status(school_id, "failed")
            return

        # 2. 인덱싱 (추출 → 그래프 빌드)
        storage.update_school_status(school_id, "indexing")

        from app.llm import GeminiProvider, LocalEmbedder

        embedder = LocalEmbedder()
        provider = GeminiProvider()
        extractor = ChunkExtractor(provider=provider)
        builder = GraphBuilder(storage=storage, embedder=embedder)

        has_failures = len(run.failures) > 0
        for page in pages:
            try:
                chunks = extractor.extract(page)
                for chunk in chunks:
                    builder.build(chunk)
            except Exception:
                has_failures = True
                logger.exception("파이프라인 실패: school_id=%d, url=%s", school_id, page.source_url)

        # 3. 완료
        final_status = "partial_failed" if has_failures else "ready"
        storage.update_school_status(school_id, final_status)

    except Exception:
        logger.exception("크롤링 전체 실패: school_id=%d", school_id)
        try:
            storage.update_school_status(school_id, "failed")
        except Exception:
            logger.exception("상태 갱신 실패: school_id=%d", school_id)


# ── FastAPI 앱 ────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작·종료 시 Storage를 초기화·정리한다."""
    storage = _get_storage()
    try:
        storage.create_schema()
    except Exception:
        logger.warning("스키마 생성 실패 (이미 존재할 수 있음)", exc_info=True)
    yield
    storage.close()


app = FastAPI(
    title="ASKU Backend API",
    description="대학 웹사이트 자동 QA 시스템 — 백엔드 REST API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — MVP에서는 모든 오리진 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 전역 예외 핸들러 ──────────────────────────────────────────────────


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTPException을 공통 에러 형식으로 변환한다."""
    return _error_response(exc.status_code, "HTTP_ERROR", str(exc.detail))


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """예상치 못한 예외를 INTERNAL_ERROR로 변환한다."""
    logger.exception("서버 내부 오류")
    return _error_response(500, "INTERNAL_ERROR", "서버 내부 오류가 발생했습니다.")


# ── 엔드포인트 ─────────────────────────────────────────────────────────


@app.post("/schools", status_code=201, response_model=SchoolResponse)
def create_school(body: SchoolCreateRequest, background_tasks: BackgroundTasks):
    """새 학교를 등록하고 초기 크롤링을 비동기로 시작한다."""
    storage = _get_storage()

    # URL 형식 간단 검증
    if not body.base_url.startswith(("http://", "https://")):
        return _error_response(422, "INVALID_URL", "base_url 형식이 올바르지 않습니다.")

    school = storage.create_school(
        School(
            name=body.name,
            base_url=body.base_url,
            crawl_schedule=body.crawl_schedule,
        )
    )
    # 상태를 crawling으로 전이
    storage.update_school_status(school.school_id, "crawling")

    # 백그라운드에서 크롤링 시작
    background_tasks.add_task(_run_crawl, school.school_id, school.base_url, "initial")

    # 최신 상태로 다시 조회
    updated = storage.get_school(school.school_id)
    return SchoolResponse(
        school_id=updated.school_id,
        name=updated.name,
        base_url=updated.base_url,
        crawl_schedule=updated.crawl_schedule,
        status=updated.status,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@app.get("/schools", response_model=SchoolListResponse)
def list_schools(query: str | None = Query(default=None, description="학교명 검색어")):
    """등록된 학교를 이름으로 검색한다. 빈 쿼리 시 전체 목록."""
    storage = _get_storage()
    schools = storage.list_schools(query=query)
    return SchoolListResponse(
        schools=[
            SchoolListItem(
                school_id=s.school_id,
                name=s.name,
                status=s.status,
                updated_at=s.updated_at,
            )
            for s in schools
        ]
    )


@app.get("/schools/{school_id}", response_model=SchoolDetailResponse)
def get_school(school_id: int):
    """학교의 상세 정보와 현재 상태를 반환한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    stats = storage.get_school_stats(school_id)
    return SchoolDetailResponse(
        school_id=school.school_id,
        name=school.name,
        base_url=school.base_url,
        crawl_schedule=school.crawl_schedule,
        status=school.status,
        stats=SchoolDetailStats(**stats),
        created_at=school.created_at,
        updated_at=school.updated_at,
    )


@app.post("/schools/{school_id}/query", response_model=QueryResponse)
def query_school(school_id: int, body: QueryRequest):
    """선택한 학교의 지식그래프를 기반으로 질문에 답변한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    # 아직 준비되지 않은 학교에 질의 시
    if school.status not in ("ready", "partial_failed"):
        return _error_response(
            503,
            "SCHOOL_NOT_READY",
            "학교 데이터가 아직 준비되지 않았습니다. 크롤링·인덱싱이 완료될 때까지 기다려주세요.",
        )

    rag = _get_rag_engine()
    result = rag.answer(school_id, body.question)
    return QueryResponse(
        answer=result.answer,
        sources=result.sources,
        entity_ids=[],  # MVP: entity_ids 하이라이트는 확장 과제
    )


@app.post("/schools/{school_id}/recrawl", status_code=202, response_model=RecrawlResponse)
def recrawl_school(school_id: int, background_tasks: BackgroundTasks):
    """해당 학교의 크롤링을 수동으로 다시 실행한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    # 이미 크롤링 중이면 409
    if school.status in ("crawling", "indexing"):
        return _error_response(409, "CRAWL_IN_PROGRESS", "이미 크롤링이 진행 중입니다.")

    storage.update_school_status(school_id, "crawling")
    background_tasks.add_task(_run_crawl, school_id, school.base_url, "recrawl")

    return RecrawlResponse(
        school_id=school_id,
        status="crawling",
        message="재크롤링이 시작되었습니다.",
    )


@app.get("/schools/{school_id}/status", response_model=StatusResponse)
def get_school_status(school_id: int):
    """현재 크롤링·인덱싱 진행 상태를 반환한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    stats = storage.get_school_stats(school_id)
    return StatusResponse(
        school_id=school_id,
        status=school.status,
        progress=StatusProgress(
            crawled_pages=0,  # MVP: 상세 진행도 추적은 미구현
            total_pages=0,
            indexed_documents=stats.get("document_count", 0),
        ),
        started_at=school.updated_at,
    )


# ── 프론트엔드 소비용 확장 엔드포인트 (02_frontend.md §4) ──────────────


@app.get("/schools/{school_id}/graph", response_model=GraphResponse)
def get_school_graph(school_id: int):
    """코어 서브그래프(차수 상위 노드·엣지)를 반환한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    # 차수 상위 엔티티 가져오기
    top_entities = storage.get_entities_for_graph(school_id, limit=100)
    if not top_entities:
        return GraphResponse(nodes=[], edges=[])

    entity_ids = [e.entity_id for e in top_entities]

    # 이 엔티티들 사이의 엣지
    graph_edges = storage.get_edges_for_graph(school_id, entity_ids)

    # 각 엔티티의 degree 계산 (이 서브그래프 내에서)
    degree_map: dict[int, int] = {}
    for e in graph_edges:
        degree_map[e.source_entity_id] = degree_map.get(e.source_entity_id, 0) + 1
        degree_map[e.target_entity_id] = degree_map.get(e.target_entity_id, 0) + 1

    nodes = [
        GraphNode(
            id=f"e_{e.entity_id}",
            type=e.type,
            name=e.name,
            degree=degree_map.get(e.entity_id, 0),
            doc_count=len(e.source_doc_ids),
        )
        for e in top_entities
    ]
    edges_out = [
        GraphEdge(
            source=f"e_{e.source_entity_id}",
            target=f"e_{e.target_entity_id}",
            relation=e.relation,
        )
        for e in graph_edges
    ]
    return GraphResponse(nodes=nodes, edges=edges_out)


@app.get("/schools/{school_id}/entities/{entity_id}", response_model=EntityDetailResponse)
def get_entity_detail(school_id: int, entity_id: int):
    """노드 상세·이웃·근거를 반환한다."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    entity = storage.get_entity(school_id, entity_id)
    if entity is None:
        return _error_response(404, "ENTITY_NOT_FOUND", "해당 엔티티를 찾을 수 없습니다.")

    # 근거 문서
    source_docs = storage.get_entity_sources(school_id, entity_id)
    sources = [
        {"title": doc.title, "url": doc.source_url}
        for doc in source_docs
    ]

    # 이웃
    neighbor_list = storage.get_entity_neighbors(school_id, entity_id)
    neighbors_out = []
    for n in neighbor_list:
        # 상대방 엔티티 결정
        if n.source.entity_id == entity_id:
            other = n.target
        else:
            other = n.source
        neighbors_out.append(
            EntityNeighbor(
                id=f"e_{other.entity_id}",
                name=other.name,
                relation=n.edge.relation,
            )
        )

    return EntityDetailResponse(
        id=f"e_{entity.entity_id}",
        type=entity.type,
        name=entity.name,
        attributes=entity.attributes,
        sources=sources,
        neighbors=neighbors_out,
    )


# ── 진입점 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=True)
