"""파이프라인 계약 스키마(DTO): 모듈 경계를 넘는 요청·응답 형태.

각 DTO의 단일 기준(source of truth)은 해당 시스템 문서다:
  - CrawlRequest / CrawledPage / CrawlFailure       → docs/01_SYSTEM/03_crawler.md §3
  - ExtractedChunk / ExtractionFailure              → docs/01_SYSTEM/04_extractor.md §4
  - BuildResult                                     → docs/01_SYSTEM/05_graph-builder.md §5

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
