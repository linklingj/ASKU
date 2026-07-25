"""핵심 엔터티: School · Document(청크) · Entity(노드) · Edge(관계).

개념 모델과 불변식의 단일 기준은 docs/01_SYSTEM/10_data-model.md,
물리 스키마(DDL)는 docs/01_SYSTEM/06_storage.md 이다. 필드는 그 DDL 컬럼과 1:1로 맞춘다.

여기서 강제하는 불변식(코드로 검증 가능한 것):
  1. 근거 추적성 — Entity·Edge 는 근거 문서 없이 만들지 않는다(source_doc_ids 비어있으면 안 됨).
  4. 임베딩 차원 = EMBEDDING_DIM.

코드로 강제할 수 없어 DB/조회 계층이 담당하는 불변식:
  2. 학교 격리 — 모든 조회는 school_id 로 필터(엔진/스토리지 책임).
  3. 엔티티 유일성 — (school_id, norm_key) UNIQUE(DB 인덱스, upsert 병합).

type/relation 은 화이트리스트(04_extractor.md §2.2/§2.3)를 따르지만, 화이트리스트 검증은
추출기(extractor)가 source of truth 로서 수행한다. 여기서는 저장 형태 그대로 str 로 둔다.
type/relation 은 str. 화이트리스트를 Enum 으로 승격하는 건 extractor 작업 몫.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# 임베딩 모델(bge-m3) 출력 차원. 모델 교체 시 함께 바뀌고 재임베딩이 필요하다
EMBEDDING_DIM = 1024


class School(BaseModel):
    """학교 — 격리 단위이자 재크롤링 주기 기준."""

    school_id: int | None = None  # PK(BIGSERIAL): insert 전에는 None
    name: str
    base_url: str
    crawl_schedule: str | None = None  # 예: 'daily', 'weekly'
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Document(BaseModel):
    """검색·인용의 최소 단위(청크). 공지 1건 = 기본 1행, 긴 문서만 여러 행으로 분할."""

    doc_id: int | None = None  # PK(BIGSERIAL)
    school_id: int
    source_url: str  # 원문 링크(근거 제공용)
    title: str | None = None
    content: str
    chunk_index: int = 0  # 같은 원문 내 순번
    content_hash: str  # 변경 감지용 본문 해시
    embedding: list[float] | None = None  # vector(EMBEDDING_DIM)
    crawled_at: datetime | None = None

    @field_validator("embedding")
    @classmethod
    def _check_dim(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != EMBEDDING_DIM:
            raise ValueError(f"embedding 차원은 {EMBEDDING_DIM} 이어야 함 (got {len(v)})")
        return v


class Entity(BaseModel):
    """그래프 노드. 학교 내에서 norm_key 가 같으면 같은 엔티티(빌더가 병합)."""

    entity_id: int | None = None  # PK(BIGSERIAL)
    school_id: int
    type: str  # 화이트리스트 → 04_extractor.md §2.2
    name: str
    norm_key: str  # 정규화 키. (school_id, norm_key) UNIQUE
    attributes: dict = Field(default_factory=dict)
    source_doc_ids: list[int] = Field(min_length=1)  # 근거 문서(불변식 1: 비어있을 수 없음)


class Edge(BaseModel):
    """그래프 관계. 학교 간 참조는 없다."""

    edge_id: int | None = None  # PK(BIGSERIAL)
    school_id: int
    source_entity_id: int
    target_entity_id: int
    relation: str  # 화이트리스트 → 04_extractor.md §2.3
    source_doc_ids: list[int] = Field(min_length=1)  # 불변식 1
