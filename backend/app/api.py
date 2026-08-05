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
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.models import Attachment, School
from app.schemas import (
    AttachmentItem,
    AttachmentListResponse,
    AttachmentUploadResponse,
    CrawlQuality,
    CrawlQualityBoard,
    CrawlQualityFinding,
    CrawlRequest,
    CrawlScope,
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
    RejectedAttachment,
    SchoolCreateRequest,
    SchoolDetailResponse,
    SchoolDetailStats,
    SchoolListItem,
    SchoolListResponse,
    SchoolResponse,
    StatusProgressDetail,
    StatusResponse,
)

logger = logging.getLogger(__name__)

# ── 의존성 (지연 초기화) ──────────────────────────────────────────────

_storage = None
_crawler = None
_rag_engine = None
_embedder = None


def _get_storage():
    """Storage 싱글턴을 반환한다. 앱 시작 시 초기화된다."""
    global _storage
    if _storage is None:
        from app.storage import Storage

        _storage = Storage.from_env()
    return _storage


def _get_rag_engine():
    """HybridRAG 엔진 싱글턴을 반환한다(그래프 RAG → 문서 RAG fallback). 최초 호출 시 초기화."""
    global _rag_engine
    if _rag_engine is None:
        from app.llm import GeminiProvider, LocalEmbedder
        from app.rag import DocumentRAG, GraphRAG, HybridRAG

        storage = _get_storage()
        embedder = LocalEmbedder()
        provider = GeminiProvider()
        graph_rag = GraphRAG(
            storage=storage,
            embedder=embedder,
            extractor=provider,
            generator=provider,
        )
        document_rag = DocumentRAG(storage=storage, embedder=embedder, generator=provider)
        _rag_engine = HybridRAG(graph_rag=graph_rag, document_rag=document_rag)
    return _rag_engine


def _get_embedder():
    """첨부 색인용 Embedder 싱글턴. 모델 로딩이 무거워 요청마다 만들지 않는다."""
    global _embedder
    if _embedder is None:
        from app.llm import LocalEmbedder

        _embedder = LocalEmbedder()
    return _embedder


# ── 공통 에러 응답 헬퍼 ───────────────────────────────────────────────


def _error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    """공통 에러 형식 {error: {code, message, details}} 으로 응답한다."""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _record_crawl_quality(storage, school_id: int, crawl_id: str, run, adapter, boards) -> None:
    """크롤 직후 수집 품질을 판정해 이력으로 남긴다.

    파서가 사이트 구조와 어긋나도 크롤은 0건 성공으로 끝나므로, 지표를 남기지
    않으면 개편을 알아챌 방법이 없다. 부가 작업이라 실패해도 크롤은 계속한다.
    """

    try:
        from app.validation import validate_crawl

        reports = validate_crawl(
            run,
            adapter,
            boards,
            previous_rows=lambda board: storage.get_previous_listing_rows(
                school_id, board, before_crawl_id=crawl_id
            ),
        )
        storage.record_crawl_quality(school_id, crawl_id, reports)
        for report in reports:
            if not report.passed:
                logger.warning("수집 품질 경고: school_id=%d %s", school_id, report.summary())
    except Exception:
        logger.exception("수집 품질 기록 실패: school_id=%d", school_id)


def _crawl_quality(storage, school_id: int) -> CrawlQuality:
    """마지막 크롤의 게시판별 수집 품질을 응답 모델로 옮긴다."""

    try:
        rows = storage.get_latest_crawl_quality(school_id)
    except Exception:  # 지표는 부가 정보다. 조회 실패가 학교 상세를 막아선 안 된다.
        logger.exception("수집 품질 조회 실패: school_id=%d", school_id)
        return CrawlQuality()
    rows = list(rows or [])
    if not rows:
        return CrawlQuality()

    boards = [
        CrawlQualityBoard(
            board=row["board"],
            listing_rows=row["listing_rows"],
            title_ratio=row["title_ratio"],
            date_ratio=row["date_ratio"],
            checked_details=row["checked_details"],
            findings=[CrawlQualityFinding(**finding) for finding in row["findings"]],
        )
        for row in rows
    ]
    return CrawlQuality(
        status="ok" if all(board.passed for board in boards) else "warning",
        checked_at=max(row["recorded_at"] for row in rows),
        boards=boards,
    )


def _school_not_found(school_id: int) -> JSONResponse:
    return _error_response(404, "SCHOOL_NOT_FOUND", "해당 학교를 찾을 수 없습니다.")


# ── 비동기 크롤링 백그라운드 태스크 ────────────────────────────────────


# ── 비동기 크롤링 진행 추적 메타데이터 ────────────────────────────────────

_PROGRESS_MAP: dict[int, dict] = {}


def _run_crawl(school_id: int, base_url: str, mode: str) -> None:
    """백그라운드에서 크롤링 파이프라인을 실행한다.

    crawling → indexing → ready 상태 전이를 추적한다.
    실패 시 failed 또는 partial_failed 로 전이한다.
    """
    storage = _get_storage()
    crawl_ok = False
    _PROGRESS_MAP[school_id] = {
        "pages": 0,
        "chunks": 0,
        "entities": 0,
        "edges": 0,
        "progress": 0.1,
        "stage": "crawling",
        "message": "공지 페이지 수집 중...",
    }
    try:
        from app.crawler import Crawler, adapter_for, boards_for
        from app.extractor import DocumentExtractor
        from app.graph_builder import GraphBuilder
        from app.schemas import ExtractionFailure

        # 1. 크롤링
        crawl_request = CrawlRequest(
            crawl_id=uuid4(),
            school_id=school_id,
            base_url=base_url,
            mode=mode,
            # 경로는 제한하지 않는다. 같은 학교 안에서 다른 게시판으로 넘어가는
            # 공지(예: 홍익대 목록에 섞인 대학원 공지)를 놓치지 않기 위해서다.
            scope=CrawlScope(allowed_hosts=[urlsplit(base_url).hostname or ""]),
        )
        crawler = Crawler.from_storage(storage)
        adapter = adapter_for(base_url)
        # 세종대처럼 공지가 여러 탭으로 쪼개진 학교는 등록된 게시판을 모두 돈다.
        boards = boards_for(base_url)
        run = crawler.crawl_boards(crawl_request, boards, adapter)
        _record_crawl_quality(storage, school_id, str(crawl_request.crawl_id), run, adapter, boards)
        pages = crawler.pages_for_extractor(run)

        # 전체 방문/수집된 페이지 수
        _PROGRESS_MAP[school_id]["pages"] = len(run.pages)
        _PROGRESS_MAP[school_id]["progress"] = 0.3

        # 재크롤 시 관측 URL로 연속 미관측 카운트를 갱신 (목록이 비면 전량 bump 방지로 스킵)
        if mode == "recrawl" and run.pages:
            observed_urls: list[str] = []
            for page in run.pages:
                if page.source_url:
                    observed_urls.append(page.source_url)
                if page.canonical_url:
                    observed_urls.append(page.canonical_url)
            try:
                storage.record_url_observations(school_id, observed_urls)
            except Exception:
                logger.exception("미관측 카운트 갱신 실패: school_id=%d", school_id)

        # 추출할 페이지가 하나도 없으면 인덱싱할 것이 없다 → 전체 실패로 처리한다.
        # 실패 기록이 있으면 크롤링 오류, 없으면 URL 오입력·리다이렉트·빈 목록 등으로
        # 수집이 0건인 경우다(둘 다 'ready'로 새면 안 됨).
        if not pages:
            storage.update_school_status(school_id, "failed")
            _PROGRESS_MAP[school_id]["stage"] = "failed"
            _PROGRESS_MAP[school_id]["progress"] = 0.0
            _PROGRESS_MAP[school_id]["message"] = (
                "크롤링 실패" if run.failures else "수집된 페이지가 없습니다 (공지 URL을 확인해 주세요)"
            )
            return

        # 2. 인덱싱 (추출 → 그래프 빌드)
        storage.update_school_status(school_id, "indexing")
        _PROGRESS_MAP[school_id]["stage"] = "extracting"
        _PROGRESS_MAP[school_id]["progress"] = 0.4
        _PROGRESS_MAP[school_id]["message"] = "본문 파싱 및 엔티티 추출 중..."

        from app.llm import GeminiProvider, LocalEmbedder

        embedder = LocalEmbedder()
        provider = GeminiProvider()
        extractor = DocumentExtractor(llm_extractor=provider)
        builder = GraphBuilder(storage=storage, embedder=embedder)

        has_failures = len(run.failures) > 0
        total_pages = max(len(pages), 1)

        for i, page in enumerate(pages):
            try:
                _PROGRESS_MAP[school_id]["stage"] = "extracting"
                result = extractor.process(page)
                if isinstance(result, ExtractionFailure):
                    has_failures = True
                    logger.warning(
                        "추출 실패: school_id=%d, url=%s, code=%s",
                        school_id, page.source_url, result.error_code,
                    )
                    continue

                _PROGRESS_MAP[school_id]["stage"] = "building"
                for chunk in result:
                    _PROGRESS_MAP[school_id]["chunks"] += 1
                    build_result = builder.build(school_id, chunk)
                    if build_result.status == "failed":
                        has_failures = True
                    else:
                        _PROGRESS_MAP[school_id]["entities"] += len(build_result.entity_ids)
                        _PROGRESS_MAP[school_id]["edges"] += len(build_result.edge_ids)
                        if build_result.status == "partial":
                            has_failures = True

                # 진행률 계산 (0.4 ~ 0.9)
                curr_p = 0.4 + (0.5 * (i + 1) / total_pages)
                _PROGRESS_MAP[school_id]["progress"] = round(curr_p, 2)

            except Exception:
                has_failures = True
                logger.exception("파이프라인 실패: school_id=%d, url=%s", school_id, page.source_url)

        # 3. 완료
        final_status = "partial_failed" if has_failures else "ready"
        storage.update_school_status(school_id, final_status)

        _PROGRESS_MAP[school_id]["stage"] = "done" if final_status == "ready" else final_status
        _PROGRESS_MAP[school_id]["progress"] = 1.0
        _PROGRESS_MAP[school_id]["message"] = "인덱싱이 완료되었습니다."
        crawl_ok = final_status in ("ready", "partial_failed")

    except Exception:
        logger.exception("크롤링 전체 실패: school_id=%d", school_id)
        _PROGRESS_MAP[school_id] = {
            "pages": 0, "chunks": 0, "entities": 0, "edges": 0,
            "progress": 0.0, "stage": "failed", "message": "파이프라인 전체 실패",
        }
        try:
            storage.update_school_status(school_id, "failed")
        except Exception:
            logger.exception("상태 갱신 실패: school_id=%d", school_id)
    finally:
        try:
            from app.scheduler import get_scheduler

            get_scheduler().on_crawl_finished(school_id, success=crawl_ok)
        except Exception:
            logger.exception("스케줄러 완료 통지 실패: school_id=%d", school_id)


# ── FastAPI 앱 ────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작·종료 시 Storage 및 Scheduler를 초기화·정리한다."""
    storage = _get_storage()
    try:
        storage.create_schema()
    except Exception:
        logger.warning("스키마 생성 실패 (이미 존재할 수 있음)", exc_info=True)

    from app.scheduler import get_scheduler

    scheduler = get_scheduler()
    try:
        await scheduler.start()
    except Exception:
        logger.warning("스케줄러 시작 실패", exc_info=True)

    yield

    try:
        await scheduler.stop()
    except Exception:
        logger.warning("스케줄러 정지 실패", exc_info=True)
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


from fastapi.encoders import jsonable_encoder


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic 검증 오류를 공통 에러 형식으로 변환한다."""
    return _error_response(
        400, "INVALID_REQUEST", "요청 본문 검증에 실패했습니다.", details={"errors": jsonable_encoder(exc.errors())}
    )


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

    url = body.base_url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _error_response(422, "INVALID_URL", "base_url은 http:// 또는 https:// 로 시작해야 합니다.")
    parts = url.split("://", 1)[1].split("/", 1)[0]
    if not parts:
        return _error_response(422, "INVALID_URL", "base_url에 올바른 호스트명이 포함되어야 합니다.")

    school = storage.create_school(
        School(
            name=body.name,
            base_url=body.base_url,
            crawl_schedule=body.crawl_schedule,
        )
    )
    # 원자적 크롤링 시작 상태 전이
    updated = storage.try_start_crawl(school.school_id) or storage.get_school(school.school_id)

    # 스케줄러에 학교 주기 등록 (앱 재시작 전에도 자동 재크롤 대상이 되도록)
    try:
        from app.scheduler import get_scheduler

        get_scheduler().register_school(
            school_id=school.school_id,
            crawl_schedule=school.crawl_schedule,
            last_run_at=datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("스케줄러 등록 실패: school_id=%s", school.school_id)

    # 백그라운드에서 크롤링 시작
    background_tasks.add_task(_run_crawl, school.school_id, school.base_url, "initial")

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
    """등록된 학교를 이름으로 검색한다 (N+1 쿼리 방지 단일 조인 조회)."""
    storage = _get_storage()
    schools_with_counts = storage.list_schools_with_entity_counts(query=query)
    items = [
        SchoolListItem(
            school_id=s.school_id,
            name=s.name,
            status=s.status,
            entity_count=count,
            updated_at=s.updated_at,
        )
        for s, count in schools_with_counts
    ]
    return SchoolListResponse(schools=items)


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
        crawl_quality=_crawl_quality(storage, school_id),
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

    # 아직 준비되지 않은 학교에 질의 시. 다만 색인이 끝난 첨부가 하나라도 있으면
    # 크롤링 결과 없이도 문서 RAG 로 답할 수 있으므로 질의를 열어준다.
    if school.status not in ("ready", "partial_failed") and storage.count_ready_attachments(school_id) == 0:
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
        entity_ids=result.entity_ids,
        source_type=result.source_type,
    )


# ── 첨부 문서 (사용자 업로드) ──────────────────────────────────────────

# 업로드 파일 1건 상한. 수강편람 PDF(수백 페이지)도 통상 이 안에 들어간다.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _attachment_item(attachment: Attachment) -> AttachmentItem:
    return AttachmentItem(
        attachment_id=attachment.attachment_id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        byte_size=attachment.byte_size,
        page_count=attachment.page_count,
        chunk_count=attachment.chunk_count,
        status=attachment.status,
        error_code=attachment.error_code,
        uploaded_at=attachment.uploaded_at,
    )


def _run_attachment_ingest(attachment: Attachment, data: bytes) -> None:
    """백그라운드에서 첨부 한 건을 파싱·청킹·임베딩해 색인한다.

    실패해도 첨부 상태(``failed``)와 ``error_code`` 는 인제스터가 남기므로, 여기서는
    로그만 남기고 다른 첨부 처리를 막지 않는다.
    """
    try:
        from app.attachment_ingest import AttachmentIngestor

        ingestor = AttachmentIngestor(storage=_get_storage(), embedder=_get_embedder())
        result = ingestor.ingest(attachment, data)
        logger.info(
            "첨부 색인 완료: school_id=%d, file=%s, units=%d, chunks=%d",
            attachment.school_id, result.filename, result.unit_count, result.chunk_count,
        )
    except Exception:
        logger.exception(
            "첨부 색인 실패: school_id=%d, attachment_id=%s, file=%s",
            attachment.school_id, attachment.attachment_id, attachment.filename,
        )


@app.post("/schools/{school_id}/attachments", status_code=202, response_model=AttachmentUploadResponse)
async def upload_attachments(
    school_id: int,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="PDF·HWP 등 첨부 문서"),
):
    """학교에 첨부 문서(수강편람 PDF·HWP 등)를 올리고 색인을 비동기로 시작한다.

    학교 등록 직후에도, 이미 등록된 학교에도 같은 경로로 올린다. 검증에 걸린 파일은
    ``rejected`` 로 돌려주고 나머지는 계속 처리한다 — 한 파일 때문에 전체가 실패하지
    않게 한다.
    """
    from hashlib import sha256

    from app.attachment_ingest import is_supported, supported_extensions

    storage = _get_storage()
    if storage.get_school(school_id) is None:
        return _school_not_found(school_id)

    accepted: list[AttachmentItem] = []
    rejected: list[RejectedAttachment] = []
    for upload in files:
        filename = (upload.filename or "").strip()
        if not filename:
            rejected.append(
                RejectedAttachment(filename="(이름 없음)", code="INVALID_FILENAME", message="파일 이름이 없습니다.")
            )
            continue
        if not is_supported(filename):
            rejected.append(
                RejectedAttachment(
                    filename=filename,
                    code="UNSUPPORTED_FILE_TYPE",
                    message=f"지원하지 않는 파일 형식입니다. 지원: {', '.join(supported_extensions())}",
                )
            )
            continue

        data = await upload.read()
        if not data:
            rejected.append(
                RejectedAttachment(filename=filename, code="EMPTY_FILE", message="빈 파일입니다.")
            )
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            rejected.append(
                RejectedAttachment(
                    filename=filename,
                    code="FILE_TOO_LARGE",
                    message=f"파일이 너무 큽니다 (최대 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB).",
                )
            )
            continue

        attachment = storage.create_attachment(
            school_id=school_id,
            filename=filename,
            content_type=upload.content_type,
            byte_size=len(data),
            file_hash=sha256(data).hexdigest(),
        )
        background_tasks.add_task(_run_attachment_ingest, attachment, data)
        accepted.append(_attachment_item(attachment))

    if not accepted and rejected:
        return _error_response(
            415,
            "NO_SUPPORTED_ATTACHMENT",
            "처리할 수 있는 첨부가 없습니다.",
            details={"rejected": [item.model_dump() for item in rejected]},
        )

    return AttachmentUploadResponse(accepted=accepted, rejected=rejected)


@app.get("/schools/{school_id}/attachments", response_model=AttachmentListResponse)
def list_attachments(school_id: int):
    """학교에 올린 첨부 문서 목록과 색인 상태를 반환한다."""
    storage = _get_storage()
    if storage.get_school(school_id) is None:
        return _school_not_found(school_id)

    return AttachmentListResponse(
        attachments=[_attachment_item(item) for item in storage.list_attachments(school_id)]
    )


@app.delete("/schools/{school_id}/attachments/{attachment_id}", status_code=204)
def delete_attachment(school_id: int, attachment_id: int):
    """첨부와 그 청크를 함께 지운다. 지운 뒤에는 문서 RAG 검색 대상에서 빠진다."""
    storage = _get_storage()
    if storage.get_school(school_id) is None:
        return _school_not_found(school_id)

    if not storage.delete_attachment(school_id, attachment_id):
        return _error_response(404, "ATTACHMENT_NOT_FOUND", "해당 첨부를 찾을 수 없습니다.")

    return Response(status_code=204)  # 204 는 본문을 실으면 안 된다


@app.post("/schools/{school_id}/recrawl", status_code=202, response_model=RecrawlResponse)
def recrawl_school(school_id: int, background_tasks: BackgroundTasks):
    """해당 학교의 크롤링을 수동으로 다시 실행한다 (원자적 상태 변경으로 중복 방지)."""
    storage = _get_storage()

    # 원자적 한 줄 UPDATE로 상태 변경 시도
    updated = storage.try_start_crawl(school_id)
    if updated is None:
        # 학교 존재 여부 검사
        if storage.get_school(school_id) is None:
            return _school_not_found(school_id)
        return _error_response(409, "CRAWL_IN_PROGRESS", "이미 크롤링이 진행 중입니다.")

    background_tasks.add_task(_run_crawl, school_id, updated.base_url, "recrawl")

    try:
        from app.scheduler import calculate_next_run, get_scheduler

        job = get_scheduler().get_job(school_id)
        if job is not None:
            now = datetime.now(timezone.utc)
            job.last_run_at = now
            job.last_status = "triggered"
            job.run_count += 1
            if job.crawl_schedule:
                job.next_run_at = calculate_next_run(job.crawl_schedule, base_time=now)
    except Exception:
        logger.exception("스케줄러 수동 재크롤 시각 갱신 실패: school_id=%d", school_id)

    return RecrawlResponse(
        school_id=school_id,
        status="crawling",
        message="재크롤링이 시작되었습니다.",
    )


@app.get("/schools/{school_id}/status", response_model=StatusResponse)
def get_school_status(school_id: int):
    """현재 크롤링·인덱싱 진행 상태를 반환한다 (실시간 추적 메타데이터 + 프론트/백엔드 통합 명세)."""
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    stats = storage.get_school_stats(school_id)
    status = school.status

    # 실시간 진행도 추적 정보가 있으면 우선 반영
    live_progress = _PROGRESS_MAP.get(school_id)
    if live_progress and status in ("crawling", "indexing"):
        progress_val = live_progress.get("progress", 0.5)
        stage_val = live_progress.get("stage", status)
        msg = live_progress.get("message", "처리 중입니다.")
        detail_obj = StatusProgressDetail(
            pages=live_progress.get("pages", 0),
            chunks=live_progress.get("chunks", 0),
            entities=live_progress.get("entities", 0),
            edges=live_progress.get("edges", 0),
        )
    else:
        # DB 기반 기본 진행률 산출 및 프론트 stage 변환 ('ready' -> 'done')
        if status == "ready":
            progress_val = 1.0
            stage_val = "done"
            msg = "인덱싱이 완료되어 질의 가능한 상태입니다."
        elif status == "partial_failed":
            progress_val = 1.0
            stage_val = "partial_failed"
            msg = "일부 처리 실패가 있었으나 질의 가능한 상태입니다."
        elif status == "crawling":
            progress_val = 0.3
            stage_val = "crawling"
            msg = "웹 페이지 수집 중입니다."
        elif status == "indexing":
            progress_val = 0.7
            stage_val = "indexing"
            msg = "지식그래프 인덱싱 중입니다."
        elif status == "failed":
            progress_val = 0.0
            stage_val = "failed"
            msg = "크롤링 또는 인덱싱 처리에 실패했습니다."
        else:
            progress_val = 0.0
            stage_val = "idle"
            msg = "대기 중입니다."

        detail_obj = StatusProgressDetail(
            pages=stats.get("document_count", 0),
            chunks=stats.get("document_count", 0),
            entities=stats.get("entity_count", 0),
            edges=0,
        )

    return StatusResponse(
        school_id=school_id,
        status=status,
        stage=stage_val,
        progress=progress_val,
        detail=detail_obj,
        message=msg,
        started_at=school.crawl_started_at or school.updated_at,
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
