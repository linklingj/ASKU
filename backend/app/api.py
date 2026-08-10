"""백엔드 API — 프론트엔드와 내부 시스템을 잇는 유일한 진입점(FastAPI).

내부 기능(크롤러·Graph RAG·저장소)을 REST 엔드포인트로 묶어 위임한다.
비즈니스 로직은 직접 구현하지 않는다.

설계 문서: docs/01_SYSTEM/01_backend-api.md
이슈: https://github.com/linklingj/ASKU/issues/15
"""

from __future__ import annotations

import logging
import os
import base64
import binascii
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.models import Attachment, School
from app.schemas import (
    AttachmentItem,
    AttachmentListResponse,
    AttachmentUploadResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    CrawlQuality,
    CrawlQualityBoard,
    CrawlQualityFinding,
    CrawlRequest,
    CrawlScope,
    EntityDetailResponse,
    EntityNeighbor,
    ErrorDetail,
    ErrorResponse,
    ForceCompleteResponse,
    GraphEdge,
    GraphNode,
    GraphResponse,
    QueryRequest,
    QueryResponse,
    RecrawlResponse,
    RejectedAttachment,
    ResetStatusResponse,
    RetrieveResponse,
    SchoolCreateRequest,
    SchoolDetailResponse,
    SchoolDetailStats,
    SchoolListItem,
    SchoolListResponse,
    SchoolResponse,
    SchoolUpdateRequest,
    StatusProgressDetail,
    StatusResponse,
)

logger = logging.getLogger(__name__)

_ADMIN_TOKEN_TTL_SECONDS = 60 * 60


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _admin_secret() -> tuple[str, str] | None:
    password = os.getenv("ADMIN_PASSWORD")
    token_secret = os.getenv("ADMIN_TOKEN_SECRET")
    return (password, token_secret) if password and token_secret else None


def _issue_admin_token(secret: str, *, now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_ADMIN_TOKEN_TTL_SECONDS)
    payload = _b64url(f"admin:{int(expires_at.timestamp())}".encode("utf-8"))
    signature = _b64url(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}", expires_at


def _require_admin(authorization: str | None = Header(default=None)) -> None:
    config = _admin_secret()
    if config is None:
        raise HTTPException(status_code=503, detail="관리자 인증이 구성되지 않았습니다.")
    _, secret = config
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    try:
        payload, supplied_signature = authorization[7:].split(".", 1)
        expected_signature = _b64url(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
        decoded = _b64url_decode(payload).decode("utf-8")
        role, expiry = decoded.split(":", 1)
        if role != "admin" or int(expiry) <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("expired or invalid role")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(status_code=401, detail="유효하지 않거나 만료된 관리자 토큰입니다.") from None

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
        from app.llm import make_llm_provider
        from app.rag import DocumentRAG, GraphRAG, HybridRAG

        storage = _get_storage()
        embedder = _get_embedder()
        provider = make_llm_provider()
        # 두 단계의 임계값은 비대칭이다(07 §3). 그래프 단계가 느슨하면 주제만 겹치는
        # 공지가 통과해 단계가 "성공"해 버리고, 정답이 든 첨부까지 내려가지 않는다.
        # 반대로 문서 단계까지 같이 조이면 fallback 자체가 막히므로 0.3 을 유지한다.
        graph_rag = GraphRAG(
            storage=storage,
            embedder=embedder,
            extractor=provider,
            generator=provider,
            min_similarity=0.6,
        )
        document_rag = DocumentRAG(
            storage=storage, embedder=embedder, generator=provider, min_similarity=0.3
        )
        _rag_engine = HybridRAG(graph_rag=graph_rag, document_rag=document_rag)
    return _rag_engine


def _get_embedder():
    """프로세스 전역 Embedder 싱글턴 — 크롤 인덱싱·첨부 색인·질의가 함께 쓴다.

    bge-m3 는 인스턴스마다 모델을 통째로 메모리에 올린다. 예전처럼 각자 새로 만들면
    학교 등록 직후 첨부를 올리는 흐름(크롤 + 첨부 색인 동시 진행)에서 모델이 겹쳐 떠
    컨테이너가 죽는다(크롤 직렬화와 같은 이유 — `_CRAWL_LOCK`). `embed()` 는 호출 간
    상태를 남기지 않아 스레드끼리 나눠 써도 된다.
    """
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


def _ensure_adapter_spec(storage, crawler, request, base_url: str):
    """규격이 없으면 확보한다. 알려진 게시판 제품이면 LLM 없이 끝난다.

    LLM 생성은 기본으로 켜지 않는다(`SPEC_AUTOGEN`). 잘못된 선택자가 곧바로 크롤에
    쓰이면 남의 서버를 잘못 긁고, 호출당 수만 토큰이 든다. 템플릿 대조는 공짜라
    항상 시도한다.
    """

    try:
        return _provision_adapter_spec(storage, crawler, request, base_url)
    except Exception:
        # 규격 확보는 보조 작업이다. 여기서 터지면 수집 자체가 중단돼, 공용 파서로도
        # 모을 수 있었을 공지를 통째로 잃는다.
        logger.exception("규격 확보 실패: %s", base_url)
        return None


def _provision_adapter_spec(storage, crawler, request, base_url: str):
    from app.crawler import ADAPTER_REGISTRY
    from app.spec_generator import collect_samples, discover_boards, generate_spec, match_template
    from app.spec_templates import host_spec

    host = (urlsplit(base_url).hostname or "").lower()
    if host in ADAPTER_REGISTRY:
        return None  # 검증된 전용 어댑터가 있으면 규격이 필요 없다

    existing = _adapter_spec(storage, base_url)
    if existing is not None:
        return existing

    seeded = host_spec(host)
    samples = collect_samples(crawler, request, base_url)
    if samples is None:
        logger.warning("규격 판정용 표본을 얻지 못했습니다: %s", base_url)
        # 표본이 없어도 손으로 쓴 규격은 살린다. 하위 게시판만 못 찾을 뿐,
        # 공용 폴백으로 떨어지는 것보다 낫다.
        if seeded is not None:
            _save_spec(storage, host, seeded, origin="host")
        return seeded
    listing, details = samples

    if seeded is not None:
        # 손으로 쓴 규격이 템플릿 대조보다 먼저다. 이 학교를 보고 쓴 것이라
        # 느슨한 제품 템플릿이 우연히 통과하는 것보다 정확하다.
        seeded = discover_boards(seeded, listing, crawler, request)
        _save_spec(storage, host, seeded, origin="host")
        logger.info("호스트 규격 적용: host=%s 게시판=%d", host, len(seeded.boards) or 1)
        return seeded

    matched = match_template(host, listing, details)
    if matched is not None:
        name, spec, report = matched
        # 템플릿 경로는 LLM 을 부르지 않으므로, 여기서 찾지 않으면 하위 게시판을
        # 발견할 기회가 아예 없다.
        spec = discover_boards(spec, listing, crawler, request)
        _save_spec(storage, host, spec, origin=f"template:{name}")
        logger.info("규격 템플릿 적용: host=%s template=%s %s", host, name, report.summary())
        return spec

    if os.getenv("SPEC_AUTOGEN", "").lower() not in ("1", "true", "yes"):
        logger.info("알려진 템플릿과 맞지 않습니다. 자동 생성은 꺼져 있습니다: host=%s", host)
        return None

    try:
        from app.llm import make_llm_provider

        result = generate_spec(make_llm_provider(), host, listing, details)
    except Exception:
        logger.exception("규격 자동 생성 실패: host=%s", host)
        return None

    if not result.accepted:
        logger.warning("규격 자동 생성이 검증을 통과하지 못했습니다: host=%s %s", host, result.summary())
        return None
    result.spec = discover_boards(result.spec, listing, crawler, request)
    _save_spec(storage, host, result.spec, origin="generated")
    logger.info("규격 자동 생성: host=%s %s", host, result.summary())
    return result.spec


def _save_spec(storage, host: str, spec, *, origin: str) -> None:
    """확보한 규격을 저장한다. 저장 실패가 크롤을 막지는 않는다."""

    try:
        storage.upsert_adapter_spec(host, spec.model_dump(mode="json"), source=spec.source)
    except Exception:
        logger.exception("규격 저장 실패: host=%s origin=%s", host, origin)


def _adapter_spec(storage, base_url: str):
    """호스트에 등록된 수집 규격을 읽는다. 없거나 형식이 어긋나면 None.

    규격은 부가 정보다. 읽지 못해도 전용 어댑터·공용 파서로 수집은 계속된다.
    """

    from app.adapter_spec import AdapterSpec

    host = (urlsplit(base_url).hostname or "").lower()
    try:
        raw = storage.get_adapter_spec(host)
        return AdapterSpec.model_validate(raw) if raw else None
    except Exception:
        logger.exception("수집 규격을 읽지 못했습니다: host=%s", host)
        return None


def _record_crawl_quality(storage, school_id: int, crawl_id: str, run, adapter, boards, spec=None) -> None:
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
            # 운영과 같은 본문 파서로 검사해야 한다. 규격을 빼면 규격 기반 학교에서
            # 공용 파서로 검사하게 되어 본문이 비어도 통과로 판정한다.
            spec=spec,
        )
        storage.record_crawl_quality(school_id, crawl_id, reports)
        for report in reports:
            if not report.passed:
                logger.warning("수집 품질 경고: school_id=%d %s", school_id, report.summary())
        return reports
    except Exception:
        logger.exception("수집 품질 기록 실패: school_id=%d", school_id)
        return []


def _refresh_broken_spec(storage, crawler, request, base_url: str, spec, reports) -> None:
    """규격이 깨진 것으로 보이면 다시 만든다.

    학교가 홈페이지를 개편하면 선택자가 어긋나 수집이 0건이 된다. 크롤은 정상
    종료하므로 지표를 보지 않으면 아무도 알아채지 못하고, 학교가 늘수록 사람이
    매번 손보기 어렵다.

    사람이 쓴 규격(`source="human"`)은 덮어쓰지 않는다. 손으로 고친 결정을 자동
    생성이 밀어내면 왜 바뀌었는지 알 수 없게 된다. 경고만 남기고 사람이 판단한다.
    """

    from app.spec_generator import BLOCKING_CODES, collect_samples, generate_spec, match_template

    broken = [r for r in reports if any(f.code in BLOCKING_CODES for f in r.findings)]
    if not broken or spec is None:
        return
    host = (urlsplit(base_url).hostname or "").lower()
    if spec.source == "human":
        logger.warning("사람이 쓴 규격이 어긋났습니다. 자동 교체하지 않습니다: host=%s", host)
        return

    samples = collect_samples(crawler, request, base_url)
    if samples is None:
        logger.warning("규격 재생성용 표본을 얻지 못했습니다: host=%s", host)
        return
    listing, details = samples

    matched = match_template(host, listing, details)
    if matched is not None:
        name, fresh, report = matched
        _save_spec(storage, host, fresh, origin=f"template:{name}")
        logger.info("규격 재생성(템플릿 %s): host=%s %s", name, host, report.summary())
        return

    if os.getenv("SPEC_AUTOGEN", "").lower() not in ("1", "true", "yes"):
        logger.warning("규격이 어긋났으나 자동 생성이 꺼져 있습니다: host=%s", host)
        return
    try:
        from app.llm import make_llm_provider

        result = generate_spec(make_llm_provider(), host, listing, details)
    except Exception:
        logger.exception("규격 재생성 실패: host=%s", host)
        return
    if not result.accepted:
        logger.warning("규격 재생성이 검증을 통과하지 못했습니다: host=%s %s", host, result.summary())
        return
    _save_spec(storage, host, result.spec, origin="regenerated")
    logger.info("규격 재생성: host=%s %s", host, result.summary())


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

# 학교 하나가 가질 수 있는 그래프 노드(엔티티) 상한. 노드가 늘수록 그래프 조회·검색이
# 잡아먹는 메모리가 커져 실제로 컨테이너가 죽었다. 운영 중 조정하는 값이라 환경변수로
# 열어 둔다(재배포 없이 바꾸기 위함). 호출자가 넘기면 그 값이 우선한다.
DEFAULT_MAX_NODES = int(os.getenv("MAX_GRAPH_NODES", "1200"))

# 크롤은 프로세스 전체에서 한 번에 하나만 돈다. `try_start_crawl` 은 같은 학교의 중복만
# 막아 다른 학교끼리는 동시에 돌 수 있었고, 크롤마다 bge-m3 임베더를 새로 만들기 때문에
# 동시 실행 수만큼 모델이 메모리에 겹쳐 떠 24GB 를 넘겼다. 크롤 대상 서버 부하와 LLM
# 레이트리밋에도 직렬 실행이 낫다.
# ponytail: 프로세스 안에서만 유효한 락. 워커를 늘리면 DB 기반 잠금이 필요하다
#           (배포는 워커 1개 고정 — deployment.md).
_CRAWL_LOCK = threading.Lock()

# 차례를 기다리는 크롤이 FastAPI 스레드풀을 붙잡지 않도록 전용 워커에서 돌린다.
# 엔드포인트가 동기 함수라 스레드풀이 마르면 API 전체가 응답하지 못한다.
_CRAWL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asku-crawl")


def _run_crawl(school_id: int, base_url: str, mode: str, max_nodes: int = 0) -> None:
    """크롤 파이프라인을 한 번에 하나만 실행한다(직렬화 래퍼).

    스케줄러·API 등 모든 호출부가 이 함수를 지나므로 여기 한 곳에서만 막는다.
    """
    # 대기 중임을 먼저 알린다. 이 표시가 없으면 차례를 기다리는 학교가 끼어버린
    # 크롤과 구분되지 않는다.
    _PROGRESS_MAP[school_id] = {
        "pages": 0, "chunks": 0, "entities": 0, "edges": 0,
        "progress": 0.05, "stage": "crawling",
        "message": "다른 학교 크롤이 끝나기를 기다리는 중입니다.",
    }
    with _CRAWL_LOCK:
        _run_crawl_inner(school_id, base_url, mode, max_nodes)


def _run_crawl_inner(school_id: int, base_url: str, mode: str, max_nodes: int = 0) -> None:
    """백그라운드에서 크롤링 파이프라인을 실행한다.

    crawling → indexing → ready 상태 전이를 추적한다.
    실패 시 failed 또는 partial_failed 로 전이한다.

    ``max_nodes`` 는 이 학교의 그래프 노드 상한이다. 0 이하면 ``DEFAULT_MAX_NODES``
    를 쓴다(스케줄러처럼 3인자로 부르는 기존 호출부를 그대로 두기 위한 기본값).
    상한에 닿으면 남은 페이지를 더 추출하지 않고 멈춘다 — LLM 호출도 같이 아낀다.
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
        from app.crawler import USER_AGENT, Crawler, adapter_for, boards_for
        from app.extractor import DocumentExtractor
        from app.rendering import PlaywrightRenderer
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
        # 브라우저 수집기는 항상 넘기되 실제로 뜨지는 않는다. 규격이 `render` 를
        # 요구하는 학교(중앙대)에서 처음 쓸 때 띄우고, 정적 학교만 도는 크롤은
        # Playwright 를 import 조차 하지 않는다.
        renderer = PlaywrightRenderer(user_agent=USER_AGENT)
        crawler = Crawler.from_storage(storage, renderer=renderer)
        # 규격은 크롤 시작 때 한 번만 확보해 파이프라인 전체에 넘긴다. Crawler·Extractor 가
        # Storage 를 직접 알지 않게 하고, 페이지마다 조회하는 일도 없게 한다.
        # 처음 보는 학교면 여기서 규격을 만든다(알려진 게시판 제품이면 LLM 없이).
        _PROGRESS_MAP[school_id]["message"] = "게시판 구조 확인 중..."
        spec = _ensure_adapter_spec(storage, crawler, crawl_request, base_url)
        _PROGRESS_MAP[school_id]["message"] = "공지 페이지 수집 중..."
        adapter = adapter_for(base_url, spec)
        # 세종대처럼 공지가 여러 탭으로 쪼개진 학교는 등록된 게시판을 모두 돈다.
        boards = boards_for(base_url, spec)
        run = crawler.crawl_boards(crawl_request, boards, adapter)
        reports = _record_crawl_quality(storage, school_id, str(crawl_request.crawl_id), run, adapter, boards, spec)
        # 사이트 개편으로 규격이 깨졌으면 다음 크롤을 위해 지금 다시 만든다.
        try:
            _refresh_broken_spec(storage, crawler, crawl_request, base_url, spec, reports)
        except Exception:
            logger.exception("규격 재생성 처리 실패: school_id=%d", school_id)
        pages = crawler.pages_for_extractor(run)
        # 브라우저는 목록을 받는 동안만 필요하다. 추출·그래프 단계까지 띄워 두면
        # 임베딩 모델과 메모리를 다투게 된다.
        renderer.close()

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

        from app.llm import make_llm_provider

        embedder = _get_embedder()
        provider = make_llm_provider()
        extractor = DocumentExtractor(llm_extractor=provider, spec=spec)
        builder = GraphBuilder(storage=storage, embedder=embedder)

        has_failures = len(run.failures) > 0
        total_pages = max(len(pages), 1)
        node_cap = max_nodes if max_nodes > 0 else DEFAULT_MAX_NODES
        node_limit_hit = False

        for i, page in enumerate(pages):
            if node_limit_hit:
                break
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

                    # 노드 수는 DB 로 센다. 청크마다 같은 엔티티가 다시 나오면
                    # upsert 로 합쳐지므로 build 결과를 더하면 실제보다 부풀려진다.
                    if storage.get_school_stats(school_id)["entity_count"] >= node_cap:
                        node_limit_hit = True
                        logger.info(
                            "노드 상한 도달로 인덱싱 중단: school_id=%d, cap=%d", school_id, node_cap
                        )
                        break

                # 진행률 계산 (0.4 ~ 0.9)
                curr_p = 0.4 + (0.5 * (i + 1) / total_pages)
                _PROGRESS_MAP[school_id]["progress"] = round(curr_p, 2)

            except Exception:
                has_failures = True
                logger.exception("파이프라인 실패: school_id=%d, url=%s", school_id, page.source_url)

        # 3. 완료
        # 상한에 걸린 것은 실패가 아니다 — 의도한 중단이라 상태를 낮추지 않는다.
        final_status = "partial_failed" if has_failures else "ready"
        storage.update_school_status(school_id, final_status)

        _PROGRESS_MAP[school_id]["stage"] = "done" if final_status == "ready" else final_status
        _PROGRESS_MAP[school_id]["progress"] = 1.0
        _PROGRESS_MAP[school_id]["message"] = (
            f"노드 상한({node_cap}개)에 도달해 인덱싱을 중단했습니다."
            if node_limit_hit
            else "인덱싱이 완료되었습니다."
        )
        crawl_ok = final_status in ("ready", "partial_failed")

    except Exception:
        logger.exception("크롤링 전체 실패: school_id=%d", school_id)
        # 중간에 터졌으면 브라우저가 떠 있을 수 있다. 그대로 두면 프로세스가
        # 살아 있는 내내 메모리를 잡는다. 두 번 불러도 안전하다.
        try:
            renderer.close()
        except Exception:
            logger.exception("브라우저 정리 실패: school_id=%d", school_id)
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
    # 진행 중인 크롤을 기다리지 않는다. 종료는 대개 재배포라 어차피 컨테이너가 사라지고,
    # 여기서 붙잡으면 배포가 크롤 시간만큼 지연된다.
    _CRAWL_EXECUTOR.shutdown(wait=False)
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


@app.post("/admin/login", response_model=AdminLoginResponse)
def admin_login(body: AdminLoginRequest):
    """환경변수의 관리자 비밀번호를 검증하고 짧은 수명 토큰을 발급한다."""

    config = _admin_secret()
    if config is None:
        return _error_response(503, "ADMIN_NOT_CONFIGURED", "관리자 인증이 구성되지 않았습니다.")
    password, secret = config
    # str 비교는 비ASCII 문자열에서 TypeError 를 내므로, UTF-8 바이트로 비교한다.
    if not hmac.compare_digest(body.password.encode("utf-8"), password.encode("utf-8")):
        return _error_response(401, "INVALID_ADMIN_CREDENTIALS", "관리자 비밀번호가 올바르지 않습니다.")
    token, expires_at = _issue_admin_token(secret)
    return AdminLoginResponse(token=token, expires_at=expires_at)


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
    # 전용 워커에 넘긴다. BackgroundTasks 로 붙이면 FastAPI 스레드풀에서 돌아,
    # 앞선 크롤을 기다리는 동안 동기 엔드포인트용 스레드를 붙잡는다.
    _CRAWL_EXECUTOR.submit(_run_crawl, school.school_id, school.base_url, "initial")

    return SchoolResponse(
        school_id=updated.school_id,
        name=updated.name,
        base_url=updated.base_url,
        image_url=updated.image_url,
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
            base_url=s.base_url,
            image_url=s.image_url,
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
        image_url=school.image_url,
        crawl_schedule=school.crawl_schedule,
        status=school.status,
        stats=SchoolDetailStats(**stats),
        crawl_quality=_crawl_quality(storage, school_id),
        created_at=school.created_at,
        updated_at=school.updated_at,
    )


@app.patch("/schools/{school_id}", response_model=SchoolResponse)
def update_school(school_id: int, body: SchoolUpdateRequest, _: None = Depends(_require_admin)):
    """관리자만 학교명·공지 URL·대표 이미지 URL을 수정한다."""

    school = _get_storage().update_school(school_id, **body.model_dump(exclude_unset=True))
    if school is None:
        return _school_not_found(school_id)
    return SchoolResponse(
        school_id=school.school_id,
        name=school.name,
        base_url=school.base_url,
        image_url=school.image_url,
        crawl_schedule=school.crawl_schedule,
        status=school.status,
        created_at=school.created_at,
        updated_at=school.updated_at,
    )


@app.delete("/schools/{school_id}", status_code=204)
def delete_school(school_id: int, _: None = Depends(_require_admin)):
    """관리자만 학교와 관련 데이터(문서·첨부·그래프·품질 이력)를 삭제한다."""

    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)
    if school.status in ("crawling", "indexing"):
        return _error_response(409, "CRAWL_IN_PROGRESS", "수집 또는 인덱싱 중인 학교는 완료 후 삭제할 수 있습니다.")
    if not storage.delete_school(school_id):
        return _school_not_found(school_id)
    try:
        from app.scheduler import get_scheduler

        get_scheduler().unregister_school(school_id)
    except Exception:
        logger.exception("삭제한 학교의 스케줄 해제 실패: school_id=%s", school_id)
    return Response(status_code=204)


def _query_not_ready(storage, school_id: int):
    """질의를 받을 수 없는 학교면 오류 응답을, 받을 수 있으면 ``None`` 을 반환한다.

    아직 준비되지 않은 학교라도 색인이 끝난 첨부가 하나라도 있으면 크롤링 결과 없이
    문서 RAG 로 답할 수 있으므로 질의를 열어준다. 질의 계열 엔드포인트(``query``·
    ``retrieve``)가 같은 판정을 쓴다.
    """

    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)
    if school.status not in ("ready", "partial_failed") and storage.count_ready_attachments(school_id) == 0:
        return _error_response(
            503,
            "SCHOOL_NOT_READY",
            "학교 데이터가 아직 준비되지 않았습니다. 크롤링·인덱싱이 완료될 때까지 기다려주세요.",
        )
    return None


@app.post("/schools/{school_id}/query", response_model=QueryResponse)
def query_school(school_id: int, body: QueryRequest):
    """선택한 학교의 지식그래프를 기반으로 질문에 답변한다(서버 기본 모델)."""
    storage = _get_storage()
    not_ready = _query_not_ready(storage, school_id)
    if not_ready is not None:
        return not_ready

    rag = _get_rag_engine()
    result = rag.answer(school_id, body.question)

    return QueryResponse(
        answer=result.answer,
        sources=result.sources,
        entity_ids=result.entity_ids,
        source_type=result.source_type,
    )


@app.post("/schools/{school_id}/retrieve", response_model=RetrieveResponse)
def retrieve_school(school_id: int, body: QueryRequest):
    """근거만 검색해 돌려준다 — 답변 생성은 하지 않는다(사용자 모델 경로).

    사용자가 자기 Gemini 키나 자기 PC 의 Ollama 를 고른 경우 브라우저가 이 응답의
    ``instruction`` + ``context`` 로 직접 모델을 부른다. 서버는 **답변 생성을 하지
    않으며** 사용자 API 키를 받지도, 저장하지도 않는다. 검색 단계의 질문 엔티티
    추출은 ``/query`` 와 동일하게 서버 모델이 맡는다 — 그래프 확장 결과가 모델
    선택에 따라 달라지지 않게 하기 위해서다(계획 §1-1).
    """
    storage = _get_storage()
    not_ready = _query_not_ready(storage, school_id)
    if not_ready is not None:
        return not_ready

    from app.rag import NO_EVIDENCE_ANSWER, answer_prompt

    rag = _get_rag_engine()
    result = rag.retrieve(school_id, body.question)

    return RetrieveResponse(
        context=result.context,
        instruction=answer_prompt(body.question),
        sources=result.sources,
        entity_ids=result.entity_ids,
        source_type=result.source_type,
        no_evidence_answer=NO_EVIDENCE_ANSWER,
    )


# ── 첨부 문서 (사용자 업로드) ──────────────────────────────────────────

# 업로드 파일 1건 크기 상한. 수강편람 PDF(수백 페이지)도 통상 이 안에 들어간다.
# `upload.read()` 가 파일을 통째로 메모리에 올린 뒤 검사하므로, 이 값이 곧 한 요청이
# 잡을 수 있는 메모리다(여러 파일이면 그만큼 곱해진다). bge-m3 가 이미 3~4GB 를 물고
# 있어 무작정 키우면 컨테이너가 죽는다(deployment.md).
MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "100"))
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

# 첨부 한 건이 만들 수 있는 청크 수 상한. 진짜 비용은 파일 크기가 아니라 텍스트 양이다
# — 청크마다 임베딩 1회(bge-m3, 배치 없음)와 DB 쓰기 1회가 든다. 크기 상한만으로는
# 텍스트가 빽빽한 파일 하나가 색인을 수십 분 붙잡는 것을 막지 못한다.
# 기본 2000 청크 ≈ 400만 자 ≈ 1300페이지 안팎으로, 수강편람 한 권은 통째로 들어간다.
# `MAX_GRAPH_NODES` 처럼 재배포 없이 조정하려고 환경변수로 열어 둔다.
MAX_ATTACHMENT_CHUNKS = int(os.getenv("MAX_ATTACHMENT_CHUNKS", "2000"))


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
        truncated=attachment.truncated,
        uploaded_at=attachment.uploaded_at,
    )


def _run_attachment_ingest(attachment: Attachment, data: bytes) -> None:
    """백그라운드에서 첨부 한 건을 파싱·청킹·임베딩해 색인한다.

    실패해도 첨부 상태(``failed``)와 ``error_code`` 는 인제스터가 남기므로, 여기서는
    로그만 남기고 다른 첨부 처리를 막지 않는다.
    """
    try:
        from app.attachment_ingest import AttachmentIngestor

        ingestor = AttachmentIngestor(
            storage=_get_storage(), embedder=_get_embedder(), max_chunks=MAX_ATTACHMENT_CHUNKS
        )
        result = ingestor.ingest(attachment, data)
        logger.info(
            "첨부 색인 완료: school_id=%d, file=%s, units=%d, chunks=%d%s",
            attachment.school_id, result.filename, result.unit_count, result.chunk_count,
            f" (청크 상한 {MAX_ATTACHMENT_CHUNKS} 도달로 중단)" if result.truncated else "",
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
def recrawl_school(
    school_id: int,
    background_tasks: BackgroundTasks,
    max_nodes: int = Query(
        DEFAULT_MAX_NODES, ge=1, description="이 학교 그래프의 노드(엔티티) 상한"
    ),
):
    """해당 학교의 크롤링을 수동으로 다시 실행한다 (원자적 상태 변경으로 중복 방지).

    ``max_nodes`` 로 이번 실행의 노드 상한을 조절한다. 생략하면 `DEFAULT_MAX_NODES`.
    """
    storage = _get_storage()

    # 원자적 한 줄 UPDATE로 상태 변경 시도
    updated = storage.try_start_crawl(school_id)
    if updated is None:
        # 학교 존재 여부 검사
        if storage.get_school(school_id) is None:
            return _school_not_found(school_id)
        return _error_response(409, "CRAWL_IN_PROGRESS", "이미 크롤링이 진행 중입니다.")

    _CRAWL_EXECUTOR.submit(_run_crawl, school_id, updated.base_url, "recrawl", max_nodes)

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


@app.post("/schools/{school_id}/reset-status", response_model=ResetStatusResponse)
def reset_school_status(school_id: int):
    """끼어버린 크롤링·인덱싱 상태를 관리용으로 강제 되돌린다.

    컨테이너가 재배포·OOM 등으로 죽으면 `_run_crawl`의 예외 핸들러가 실행되지
    않아 상태가 crawling/indexing 에 영구히 남는다(`try_start_crawl` 이 이후
    모든 recrawl 을 409 로 막는다). 자동 판별 없이, 운영자가 `/status` 로 직접
    확인한 뒤 호출하는 수동 복구 경로다.
    """
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    if school.status not in ("crawling", "indexing"):
        return _error_response(
            409, "NOT_STUCK", f"현재 상태가 '{school.status}' 라 리셋 대상이 아닙니다."
        )

    storage.update_school_status(school_id, "failed")
    _PROGRESS_MAP.pop(school_id, None)

    return ResetStatusResponse(
        school_id=school_id,
        status="failed",
        message="상태를 failed 로 되돌렸습니다. 다시 재크롤링을 시작할 수 있습니다.",
    )


@app.post("/schools/{school_id}/force-complete", response_model=ForceCompleteResponse)
def force_complete_school(school_id: int):
    """indexing/partial_failed 상태를 관리용으로 강제 ready(완료) 처리한다.

    인덱싱이 끼었거나 일부만 실패했지만 이미 쌓인 데이터로 질의해도 충분하다고
    운영자가 판단했을 때 쓴다. 실제로 색인을 다시 돌리지 않는다 — 상태값만
    바꾼다. `crawling`(아직 아무것도 색인 안 됨)·`failed`(색인 결과 없음)·
    `ready`(이미 완료)에서는 데이터 없이 완료로 속일 수 있어 거부한다.
    """
    storage = _get_storage()
    school = storage.get_school(school_id)
    if school is None:
        return _school_not_found(school_id)

    if school.status not in ("indexing", "partial_failed"):
        return _error_response(
            409, "NOT_ELIGIBLE", f"현재 상태가 '{school.status}' 라 강제 완료 대상이 아닙니다."
        )

    storage.update_school_status(school_id, "ready")
    _PROGRESS_MAP.pop(school_id, None)

    return ForceCompleteResponse(
        school_id=school_id,
        status="ready",
        message="상태를 ready 로 강제 완료 처리했습니다. 지금까지 색인된 데이터로 질의할 수 있습니다.",
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
