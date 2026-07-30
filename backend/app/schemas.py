"""파이프라인 계약 스키마(DTO): 모듈 경계를 넘는 요청·응답 형태.

각 DTO의 단일 기준(source of truth)은 해당 시스템 문서다:
  - CrawlRequest / CrawledPage / CrawlFailure       → docs/01_SYSTEM/03_crawler.md §3
  - ExtractedChunk / ExtractionFailure              → docs/01_SYSTEM/04_extractor.md §4
  - BuildResult                                     → docs/01_SYSTEM/05_graph-builder.md §5
  - RagAnswer / Source                              → docs/01_SYSTEM/07_graph-rag-engine.md §4

여기 DTO는 영속 엔터티(models.py)와 다르다: DB PK(doc_id·entity_id…)가 없고,
Extractor 산출 엔티티/관계는 아직 ID 없는 '이름 기반'이다(빌더가 ID 부여).

공통 필드 강제:
  - school_id: 모든 경계 넘는 DTO 에 필수.
  - source_url / content_hash: 근거 추적·증분 갱신 키.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# ── 공용 하위 스키마 ────────────────────────────────────────────────


class CrawlScope(BaseModel):
    """크롤 범위. 목록 페이지 수와 총 공지 수를 별도로 제한한다."""

    allowed_hosts: list[str] = Field(default_factory=list)
    path_prefixes: list[str] = Field(default_factory=list)
    max_listing_pages: int = Field(default=10, ge=1)
    max_items: int = Field(default=300, ge=1)


class Attachment(BaseModel):
    """첨부파일 링크 힌트. 본문 파싱은 하지 않는다."""

    url: str
    name_hint: str | None = None
    content_type: str | None = None


class ExtractedEntity(BaseModel):
    """추출된 이름 기반 엔티티(ID·school_id 없음, 빌더가 부여)"""

    type: str
    name: str
    attributes: dict = Field(default_factory=dict)


class ExtractedRelation(BaseModel):
    """추출된 이름 기반 관계. source/target 은 엔티티 이름 문자열"""

    source: str
    relation: str
    target: str


# ── Crawler DTO ─────────────────────────────────


class CrawlRequest(BaseModel):
    """Crawler 입력. crawl_id·school_id·base_url·mode 필수."""

    crawl_id: UUID
    school_id: int
    base_url: str
    mode: Literal["initial", "recrawl"]
    scope: CrawlScope | None = None


class CrawledPage(BaseModel):
    """Crawler 출력. new·changed 만 Extractor 로 넘어간다."""

    crawl_id: UUID
    school_id: int
    source_url: str
    canonical_url: str
    title_hint: str | None = None
    category_hint: str | None = None
    author_hint: str | None = None
    published_at_hint: datetime | None = None
    raw_html: str  # Extractor 로 넘길 일시 입력(영구 저장 미정)
    attachments: list[Attachment] = Field(default_factory=list)
    content_hash: str
    fetched_at: datetime
    crawl_status: Literal["new", "changed", "unchanged"]


class CrawlFailure(BaseModel):
    """Crawler 실패 이력."""

    crawl_id: UUID
    school_id: int
    source_url: str
    stage: Literal["policy", "fetch", "render"]
    error_code: str
    retryable: bool
    occurred_at: datetime


# ── Extractor DTO (04_extractor.md §4) ─────────────────────────────


class ExtractedChunk(BaseModel):
    """Extractor 출력 = Graph Builder 입력. 아직 영속 ID 없음."""

    school_id: int
    source_url: str
    title: str | None = None
    content: str
    chunk_index: int = 0
    content_hash: str
    crawled_at: datetime | None = None
    entities: list[ExtractedEntity]  # 빈 리스트 허용(추출 0건)
    relations: list[ExtractedRelation]
    extraction_status: Literal["complete", "partial"]


class ExtractionFailure(BaseModel):
    """본문을 만들 수 없을 때 반환."""

    school_id: int
    source_url: str
    content_hash: str
    error_code: str
    retryable: bool
    warnings: list[str] = Field(default_factory=list)


# ── Graph Builder DTO (05_graph-builder.md §5) ─────────────────────


class BuildResult(BaseModel):
    """Graph Builder 결과. 성공 시 doc_id 필수, 실패 시 error_code 필수."""

    school_id: int
    source_url: str
    content_hash: str
    doc_id: int | None = None
    entity_ids: list[int] = Field(default_factory=list)
    edge_ids: list[int] = Field(default_factory=list)
    status: Literal["complete", "partial", "failed"]
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None  # 실패 시에만
    retryable: bool | None = None  # 실패 시에만

    @model_validator(mode="after")
    def _check_status_contract(self) -> "BuildResult":
        if self.status == "failed":
            if self.error_code is None:
                raise ValueError("status=failed 이면 error_code 가 있어야 함")
        elif self.doc_id is None:
            raise ValueError(f"status={self.status} 이면 doc_id 가 있어야 함")
        return self


# ── Graph RAG DTO (07_graph-rag-engine.md §4) ──────────────────────


class Source(BaseModel):
    """답변 근거 출처. title 은 원문 제목(없을 수 있음), url 은 원문 링크."""

    title: str | None = None
    url: str


class RagAnswer(BaseModel):
    """Graph RAG 엔진 출력 = API POST /schools/{id}/query 응답 본문.

    근거가 없거나 유사도 임계 미만이면 answer 는 보류 문구, sources 는 빈 목록이다.
    """

    answer: str
    sources: list[Source] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)


# ── Backend API 요청·응답 스키마 (01_backend-api.md) ─────────────────


class SchoolCreateRequest(BaseModel):
    """POST /schools 요청 본문."""

    name: str = Field(..., min_length=1, description="학교명")
    base_url: str = Field(..., description="공지·학사 기준 URL")
    crawl_schedule: str | None = Field(
        default=None,
        description="재크롤링 주기 (daily/weekly/monthly/hourly, 30m·1h, 또는 5필드 cron)",
    )


class SchoolResponse(BaseModel):
    """학교 응답 (등록·상세 공통)."""

    school_id: int
    name: str
    base_url: str
    crawl_schedule: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class SchoolListItem(BaseModel):
    """GET /schools 목록 항목."""

    school_id: int
    name: str
    status: str
    entity_count: int = 0
    updated_at: datetime


class SchoolListResponse(BaseModel):
    """GET /schools 응답."""

    schools: list[SchoolListItem]


class SchoolDetailStats(BaseModel):
    """학교 상세의 통계 섹션."""

    document_count: int = 0
    entity_count: int = 0
    last_crawled_at: datetime | None = None


class SchoolDetailResponse(BaseModel):
    """GET /schools/{id} 응답."""

    school_id: int
    name: str
    base_url: str
    crawl_schedule: str | None = None
    status: str
    stats: SchoolDetailStats
    created_at: datetime
    updated_at: datetime


class QueryRequest(BaseModel):
    """POST /schools/{id}/query 요청 본문."""

    question: str = Field(..., min_length=1)


class QueryResponse(BaseModel):
    """POST /schools/{id}/query 응답."""

    answer: str
    sources: list[Source] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)


class RecrawlResponse(BaseModel):
    """POST /schools/{id}/recrawl 응답."""

    school_id: int
    status: str
    message: str


class StatusProgressDetail(BaseModel):
    """크롤링·인덱싱 단계별 상세 카운트."""

    pages: int = 0
    chunks: int = 0
    entities: int = 0
    edges: int = 0


class StatusResponse(BaseModel):
    """GET /schools/{id}/status 응답 (프론트·백엔드 문서 계약 통합)."""

    school_id: int
    status: str  # 백엔드 하위호환 status
    stage: str   # 프론트엔드 소비용 stage (idle|crawling|extracting|building|indexing|ready|partial_failed|failed)
    progress: float = 0.0  # 0.0 ~ 1.0 진행률
    detail: StatusProgressDetail = Field(default_factory=StatusProgressDetail)
    message: str | None = None
    started_at: datetime | None = None


class GraphNode(BaseModel):
    """그래프 노드 (프론트엔드 소비용)."""

    id: str
    type: str
    name: str
    degree: int = 0
    doc_count: int = 0


class GraphEdge(BaseModel):
    """그래프 엣지 (프론트엔드 소비용)."""

    source: str
    target: str
    relation: str


class GraphResponse(BaseModel):
    """GET /schools/{id}/graph 응답."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class EntityNeighbor(BaseModel):
    """엔티티 이웃 정보."""

    id: str
    name: str
    relation: str


class EntityDetailResponse(BaseModel):
    """GET /schools/{id}/entities/{eid} 응답."""

    id: str
    type: str
    name: str
    attributes: dict = Field(default_factory=dict)
    sources: list[Source] = Field(default_factory=list)
    neighbors: list[EntityNeighbor] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """공통 에러 본문."""

    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    """공통 에러 응답 형식. {error: {code, message, details}}"""

    error: ErrorDetail
