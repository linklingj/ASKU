"""Postgres/pgvector 기반 Storage 공개 인터페이스.

다른 시스템은 이 모듈의 ``Storage`` 메서드만 사용한다. SQLAlchemy 엔진과
SQL 세부 사항은 이 모듈 밖으로 노출하지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BIGINT,
    INTEGER,
    TIMESTAMP,
    TEXT,
    Column,
    ForeignKey,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    and_,
    create_engine,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.engine import Engine, RowMapping

from app.models import (
    EMBEDDING_DIM,
    SOURCE_TYPE_WEB,
    Attachment,
    Document,
    Edge,
    Entity,
    School,
)


metadata = MetaData()

schools = Table(
    "schools",
    metadata,
    Column("school_id", BIGINT, primary_key=True),
    Column("name", TEXT, nullable=False),
    Column("base_url", TEXT, nullable=False),
    Column("crawl_schedule", TEXT),
    Column("status", TEXT, nullable=False, server_default=text("'idle'")),
    Column("crawl_started_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
)

attachments = Table(
    "attachments",
    metadata,
    Column("attachment_id", BIGINT, primary_key=True),
    Column("school_id", BIGINT, ForeignKey("schools.school_id"), nullable=False),
    Column("filename", TEXT, nullable=False),
    Column("content_type", TEXT),
    Column("byte_size", BIGINT, nullable=False),
    Column("file_hash", TEXT, nullable=False),
    Column("page_count", INTEGER, nullable=False, server_default=text("0")),
    Column("chunk_count", INTEGER, nullable=False, server_default=text("0")),
    Column("status", TEXT, nullable=False, server_default=text("'pending'")),
    Column("error_code", TEXT),
    Column("uploaded_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    # 같은 학교에 같은 파일을 다시 올리면 새 행을 만들지 않고 기존 첨부를 다시 색인한다.
    UniqueConstraint("school_id", "file_hash", name="uq_attachments_school_file_hash"),
)
Index("ix_attachments_school_id", attachments.c.school_id)

documents = Table(
    "documents",
    metadata,
    Column("doc_id", BIGINT, primary_key=True),
    Column("school_id", BIGINT, ForeignKey("schools.school_id"), nullable=False),
    Column("source_url", TEXT, nullable=False),
    Column("title", TEXT),
    Column("content", TEXT, nullable=False),
    Column("chunk_index", INTEGER, nullable=False, server_default=text("0")),
    Column("content_hash", TEXT, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIM)),
    Column("crawled_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("miss_count", INTEGER, nullable=False, server_default=text("0")),
    Column("expired_at", TIMESTAMP(timezone=True)),
    Column("source_type", TEXT, nullable=False, server_default=text(f"'{SOURCE_TYPE_WEB}'")),
    Column("page", INTEGER),  # 첨부 문서의 페이지·구역 번호. 'web' 청크는 NULL
    Column("attachment_id", BIGINT, ForeignKey("attachments.attachment_id")),
    UniqueConstraint(
        "school_id",
        "source_url",
        "content_hash",
        "chunk_index",
        name="uq_documents_school_url_hash_chunk",
    ),
)
Index(
    "ix_documents_embedding_hnsw",
    documents.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
Index("ix_documents_school_id", documents.c.school_id)
# 검색 풀 분리(그래프 RAG='web', 문서 RAG='attachment')가 인덱스를 타게 한다.
Index("ix_documents_school_source_type", documents.c.school_id, documents.c.source_type)
Index("ix_documents_attachment_id", documents.c.attachment_id)

entities = Table(
    "entities",
    metadata,
    Column("entity_id", BIGINT, primary_key=True),
    Column("school_id", BIGINT, ForeignKey("schools.school_id"), nullable=False),
    Column("type", TEXT, nullable=False),
    Column("name", TEXT, nullable=False),
    Column("norm_key", TEXT, nullable=False),
    Column("attributes", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("source_doc_ids", ARRAY(BIGINT), nullable=False),
    UniqueConstraint("school_id", "norm_key", name="uq_entities_school_norm_key"),
)

edges = Table(
    "edges",
    metadata,
    Column("edge_id", BIGINT, primary_key=True),
    Column("school_id", BIGINT, ForeignKey("schools.school_id"), nullable=False),
    Column("source_entity_id", BIGINT, ForeignKey("entities.entity_id"), nullable=False),
    Column("target_entity_id", BIGINT, ForeignKey("entities.entity_id"), nullable=False),
    Column("relation", TEXT, nullable=False),
    Column("source_doc_ids", ARRAY(BIGINT), nullable=False),
    # MVP 결정: 같은 학교에서 같은 양 끝과 관계는 하나의 엣지로 병합한다.
    UniqueConstraint(
        "school_id",
        "source_entity_id",
        "target_entity_id",
        "relation",
        name="uq_edges_school_source_target_relation",
    ),
)
Index("ix_edges_school_source", edges.c.school_id, edges.c.source_entity_id)


@dataclass(frozen=True)
class Neighbor:
    """1-hop 조회 결과. 엣지와 양 끝 엔티티를 함께 반환한다."""

    source: Entity
    edge: Edge
    target: Entity


def _merge_ids(existing: Sequence[int], incoming: Sequence[int]) -> list[int]:
    """근거 문서 ID를 중복 없이 오름차순으로 병합한다."""

    return sorted(set(existing).union(incoming))


def _validate_embedding(embedding: Sequence[float] | None) -> None:
    if embedding is not None and len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"embedding 차원은 {EMBEDDING_DIM} 이어야 함")


def _assert_entities_belong_to_school(
    school_id: int, entity_ids: Sequence[int], found_entity_ids: Sequence[int]
) -> None:
    """엣지 양 끝이 같은 학교 소속이라는 불변식을 강제한다."""

    expected = set(entity_ids)
    if expected != set(found_entity_ids):
        raise ValueError(f"school_id={school_id}에 속하지 않는 엔티티를 엣지로 연결할 수 없습니다")


def _document(row: RowMapping) -> Document:
    return Document(
        doc_id=row["doc_id"],
        school_id=row["school_id"],
        source_url=row["source_url"],
        title=row["title"],
        content=row["content"],
        chunk_index=row["chunk_index"],
        content_hash=row["content_hash"],
        embedding=list(row["embedding"]) if row["embedding"] is not None else None,
        crawled_at=row["crawled_at"],
        miss_count=int(row["miss_count"]) if row.get("miss_count") is not None else 0,
        expired_at=row.get("expired_at"),
        source_type=row.get("source_type") or SOURCE_TYPE_WEB,
        page=row.get("page"),
        attachment_id=row.get("attachment_id"),
    )


def _attachment(row: RowMapping) -> Attachment:
    return Attachment(
        attachment_id=row["attachment_id"],
        school_id=row["school_id"],
        filename=row["filename"],
        content_type=row["content_type"],
        byte_size=int(row["byte_size"]),
        file_hash=row["file_hash"],
        page_count=int(row["page_count"]),
        chunk_count=int(row["chunk_count"]),
        status=row["status"],
        error_code=row["error_code"],
        uploaded_at=row["uploaded_at"],
    )


def _entity(row: RowMapping) -> Entity:
    return Entity(
        entity_id=row["entity_id"],
        school_id=row["school_id"],
        type=row["type"],
        name=row["name"],
        norm_key=row["norm_key"],
        attributes=row["attributes"] or {},
        source_doc_ids=row["source_doc_ids"],
    )


def _edge(row: RowMapping) -> Edge:
    return Edge(
        edge_id=row["edge_id"],
        school_id=row["school_id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relation=row["relation"],
        source_doc_ids=row["source_doc_ids"],
    )


def _school(row: RowMapping) -> School:
    return School(
        school_id=row["school_id"],
        name=row["name"],
        base_url=row["base_url"],
        crawl_schedule=row["crawl_schedule"],
        status=row["status"],
        crawl_started_at=row.get("crawl_started_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Storage:
    """학교 격리가 강제된 동기식 Postgres 저장소."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self._engine: Engine = create_engine(database_url, echo=echo)

    @classmethod
    def from_env(cls) -> "Storage":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL 환경변수가 필요합니다")
        return cls(database_url)

    def close(self) -> None:
        self._engine.dispose()

    def create_schema(self) -> None:
        """pgvector 확장과 Storage 테이블·인덱스를 생성하고 기존 DB 호환성 컬럼을 보완한다."""

        with self._engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            metadata.create_all(connection)
            # 기존 테이블 호환성을 위한 경량 멱등 마이그레이션
            connection.execute(text("ALTER TABLE schools ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'idle'"))
            connection.execute(text("ALTER TABLE schools ADD COLUMN IF NOT EXISTS crawl_started_at TIMESTAMPTZ"))
            connection.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS miss_count INT NOT NULL DEFAULT 0"))
            connection.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ"))
            connection.execute(
                text(
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL "
                    f"DEFAULT '{SOURCE_TYPE_WEB}'"
                )
            )
            connection.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS page INT"))
            connection.execute(
                text(
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS attachment_id BIGINT "
                    "REFERENCES attachments(attachment_id)"
                )
            )

    def create_school(self, school: School) -> School:
        values = {
            "name": school.name,
            "base_url": school.base_url,
            "crawl_schedule": school.crawl_schedule,
        }
        with self._engine.begin() as connection:
            row = connection.execute(
                insert(schools).values(**values).returning(*schools.c)
            ).mappings().one()
        return School(**row)

    def get_school(self, school_id: int) -> School | None:
        """학교 ID로 단일 학교를 조회한다."""

        statement = select(schools).where(schools.c.school_id == school_id)
        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            return _school(row) if row is not None else None

    def list_schools(self, query: str | None = None) -> list[School]:
        """학교 목록을 조회한다. 이름으로 필터링할 수 있다."""

        statement = select(schools)
        if query is not None:
            statement = statement.where(schools.c.name.ilike(f"%{query}%"))
        statement = statement.order_by(schools.c.name)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [_school(row) for row in rows]

    def list_schools_with_entity_counts(self, query: str | None = None) -> list[tuple[School, int]]:
        """학교 목록과 소속 엔티티 수를 단일 JOIN/GROUP BY 쿼리로 조회한다 (N+1 방지)."""

        statement = (
            select(*schools.c, func.count(entities.c.entity_id).label("entity_count"))
            .outerjoin(entities, schools.c.school_id == entities.c.school_id)
        )
        if query is not None:
            statement = statement.where(schools.c.name.ilike(f"%{query}%"))
        statement = statement.group_by(*schools.c).order_by(schools.c.name)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [(_school(row), int(row["entity_count"])) for row in rows]

    def try_start_crawl(self, school_id: int) -> School | None:
        """원자적으로 크롤링 시작 상태로 변경한다. 이미 crawling/indexing 중이면 None을 반환한다."""

        statement = (
            schools.update()
            .where(
                and_(
                    schools.c.school_id == school_id,
                    schools.c.status.notin_(["crawling", "indexing"]),
                )
            )
            .values(status="crawling", crawl_started_at=func.now())
            .returning(*schools.c)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            return _school(row) if row is not None else None

    def update_school_status(self, school_id: int, status: str) -> School | None:
        """학교의 상태를 업데이트한다."""

        values: dict[str, Any] = {"status": status}
        if status in ("crawling",):
            values["crawl_started_at"] = func.now()

        statement = (
            schools.update()
            .where(schools.c.school_id == school_id)
            .values(**values)
            .returning(*schools.c)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            return _school(row) if row is not None else None

    def get_school_stats(self, school_id: int) -> dict:
        """학교의 문서 수, 엔티티 수, 마지막 크롤링 시각을 반환한다."""

        with self._engine.connect() as connection:
            document_count = connection.execute(
                select(func.count()).where(documents.c.school_id == school_id)
            ).scalar_one()

            entity_count = connection.execute(
                select(func.count()).where(entities.c.school_id == school_id)
            ).scalar_one()

            attachment_count = connection.execute(
                select(func.count()).where(attachments.c.school_id == school_id)
            ).scalar_one()

            last_crawled_at = connection.execute(
                select(func.max(documents.c.crawled_at)).where(documents.c.school_id == school_id)
            ).scalar_one_or_none()

            return {
                "document_count": document_count,
                "entity_count": entity_count,
                "attachment_count": attachment_count,
                "last_crawled_at": last_crawled_at,
            }

    # ── 첨부 문서(사용자 업로드) ──────────────────────────────────────

    def create_attachment(
        self,
        school_id: int,
        filename: str,
        content_type: str | None,
        byte_size: int,
        file_hash: str,
    ) -> Attachment:
        """업로드 첨부를 ``pending`` 상태로 등록하고 첨부 행을 반환한다.

        같은 학교에 같은 바이트의 파일을 다시 올리면 새 행을 만들지 않고 기존 행을
        다시 ``pending`` 으로 되돌린다. 청크는 ``source_url`` 이 같아 멱등 업서트되므로
        재색인해도 중복이 쌓이지 않는다.
        """

        statement = insert(attachments).values(
            school_id=school_id,
            filename=filename,
            content_type=content_type,
            byte_size=byte_size,
            file_hash=file_hash,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_attachments_school_file_hash",
            set_={
                "filename": statement.excluded.filename,
                "content_type": statement.excluded.content_type,
                "byte_size": statement.excluded.byte_size,
                "status": "pending",
                "error_code": None,
                "uploaded_at": func.now(),
            },
        ).returning(*attachments.c)
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one()
        return _attachment(row)

    def get_attachment(self, school_id: int, attachment_id: int) -> Attachment | None:
        """학교 범위 안의 첨부 한 건을 조회한다."""

        statement = select(attachments).where(
            and_(
                attachments.c.school_id == school_id,
                attachments.c.attachment_id == attachment_id,
            )
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return _attachment(row) if row is not None else None

    def list_attachments(self, school_id: int) -> list[Attachment]:
        """학교의 첨부 목록을 최신 업로드 순으로 반환한다."""

        statement = (
            select(attachments)
            .where(attachments.c.school_id == school_id)
            .order_by(attachments.c.uploaded_at.desc(), attachments.c.attachment_id.desc())
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_attachment(row) for row in rows]

    def update_attachment_status(
        self,
        school_id: int,
        attachment_id: int,
        status: str,
        *,
        page_count: int | None = None,
        chunk_count: int | None = None,
        error_code: str | None = None,
    ) -> Attachment | None:
        """첨부의 색인 상태·집계를 갱신한다. 주지 않은 집계 값은 그대로 둔다."""

        values: dict[str, Any] = {"status": status, "error_code": error_code}
        if page_count is not None:
            values["page_count"] = page_count
        if chunk_count is not None:
            values["chunk_count"] = chunk_count

        statement = (
            attachments.update()
            .where(
                and_(
                    attachments.c.school_id == school_id,
                    attachments.c.attachment_id == attachment_id,
                )
            )
            .values(**values)
            .returning(*attachments.c)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return _attachment(row) if row is not None else None

    def delete_attachment(self, school_id: int, attachment_id: int) -> bool:
        """첨부와 그 청크를 함께 지운다. 지울 첨부가 없으면 False."""

        with self._engine.begin() as connection:
            connection.execute(
                documents.delete().where(
                    and_(
                        documents.c.school_id == school_id,
                        documents.c.attachment_id == attachment_id,
                    )
                )
            )
            deleted = connection.execute(
                attachments.delete()
                .where(
                    and_(
                        attachments.c.school_id == school_id,
                        attachments.c.attachment_id == attachment_id,
                    )
                )
                .returning(attachments.c.attachment_id)
            ).scalar_one_or_none()
        return deleted is not None

    def count_ready_attachments(self, school_id: int) -> int:
        """색인이 끝난 첨부 수. 크롤링이 실패해도 질의를 열어줄지 판단하는 데 쓴다."""

        statement = select(func.count()).where(
            and_(attachments.c.school_id == school_id, attachments.c.status == "ready")
        )
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def get_entities_for_graph(self, school_id: int, *, limit: int = 100) -> list[Entity]:
        """그래프 코어 표시를 위해 차수가 높은 엔티티를 반환한다."""

        statement = (
            select(entities)
            .join(
                edges,
                and_(
                    edges.c.school_id == school_id,
                    or_(
                        edges.c.source_entity_id == entities.c.entity_id,
                        edges.c.target_entity_id == entities.c.entity_id
                    )
                )
            )
            .where(entities.c.school_id == school_id)
            .group_by(entities.c.entity_id)
            .order_by(func.count(edges.c.edge_id).desc())
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [_entity(row) for row in rows]

    def get_edges_for_graph(self, school_id: int, entity_ids: Sequence[int]) -> list[Edge]:
        """주어진 엔티티들 사이의 엣지를 모두 반환한다."""

        if not entity_ids:
            return []

        statement = select(edges).where(
            and_(
                edges.c.school_id == school_id,
                edges.c.source_entity_id.in_(list(entity_ids)),
                edges.c.target_entity_id.in_(list(entity_ids)),
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            return [_edge(row) for row in rows]

    def get_entity(self, school_id: int, entity_id: int) -> Entity | None:
        """단일 엔티티를 조회한다."""

        statement = select(entities).where(
            and_(entities.c.school_id == school_id, entities.c.entity_id == entity_id)
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            return _entity(row) if row is not None else None

    def get_entity_neighbors(self, school_id: int, entity_id: int) -> list[Neighbor]:
        """단일 엔티티의 이웃을 조회한다."""

        return self.neighbors(school_id, [entity_id])

    def get_entity_sources(self, school_id: int, entity_id: int) -> list[Document]:
        """엔티티의 근거 문서를 조회한다."""

        entity = self.get_entity(school_id, entity_id)
        if entity is None or not entity.source_doc_ids:
            return []

        return self.get_documents(school_id, entity.source_doc_ids)

    def upsert_document(
        self,
        school_id: int,
        source_url: str,
        title: str | None,
        content: str,
        chunk_index: int,
        content_hash: str,
        embedding: Sequence[float] | None,
        *,
        crawled_at: datetime | None = None,
        source_type: str = SOURCE_TYPE_WEB,
        page: int | None = None,
        attachment_id: int | None = None,
    ) -> int:
        """청크를 해시 기준으로 멱등 저장하고 ``doc_id``를 반환한다.

        ``source_type`` 은 검색 풀을 가른다('web' 크롤링 / 'attachment' 업로드).
        첨부 청크는 ``page``(페이지 번호)와 ``attachment_id``(소속 첨부)를 함께 남겨
        인용 시 페이지를 표기하고 첨부 삭제 시 청크를 함께 지울 수 있게 한다.
        """

        _validate_embedding(embedding)
        values: dict[str, Any] = {
            "school_id": school_id,
            "source_url": source_url,
            "title": title,
            "content": content,
            "chunk_index": chunk_index,
            "content_hash": content_hash,
            "embedding": list(embedding) if embedding is not None else None,
            "source_type": source_type,
            "page": page,
            "attachment_id": attachment_id,
        }
        if crawled_at is not None:
            values["crawled_at"] = crawled_at

        statement = insert(documents).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_documents_school_url_hash_chunk",
            set_={
                "title": statement.excluded.title,
                "content": statement.excluded.content,
                "embedding": statement.excluded.embedding,
                "crawled_at": statement.excluded.crawled_at,
                "source_type": statement.excluded.source_type,
                "page": statement.excluded.page,
                "attachment_id": statement.excluded.attachment_id,
                "miss_count": 0,
                "expired_at": None,
            },
        ).returning(documents.c.doc_id)
        with self._engine.begin() as connection:
            return connection.execute(statement).scalar_one()

    def upsert_entity(
        self,
        school_id: int,
        type: str,
        name: str,
        norm_key: str,
        attributes: dict[str, Any],
        source_doc_ids: Sequence[int],
    ) -> int:
        """엔티티를 병합한다. 새 속성 값이 기존 같은 키를 덮어쓴다."""

        if not source_doc_ids:
            raise ValueError("Entity에는 최소 하나의 source_doc_id가 필요합니다")
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(entities).where(
                    and_(entities.c.school_id == school_id, entities.c.norm_key == norm_key)
                )
            ).mappings().one_or_none()
            if existing is None:
                return connection.execute(
                    insert(entities)
                    .values(
                        school_id=school_id,
                        type=type,
                        name=name,
                        norm_key=norm_key,
                        attributes=attributes,
                        source_doc_ids=_merge_ids([], source_doc_ids),
                    )
                    .returning(entities.c.entity_id)
                ).scalar_one()

            merged_attributes = {**(existing["attributes"] or {}), **attributes}
            merged_doc_ids = _merge_ids(existing["source_doc_ids"], source_doc_ids)
            return connection.execute(
                entities.update()
                .where(entities.c.entity_id == existing["entity_id"])
                .values(attributes=merged_attributes, source_doc_ids=merged_doc_ids)
                .returning(entities.c.entity_id)
            ).scalar_one()

    def upsert_edge(
        self,
        school_id: int,
        source_entity_id: int,
        target_entity_id: int,
        relation: str,
        source_doc_ids: Sequence[int],
    ) -> int:
        """같은 양 끝·관계의 엣지를 병합하고 근거 문서를 누적한다."""

        if not source_doc_ids:
            raise ValueError("Edge에는 최소 하나의 source_doc_id가 필요합니다")
        identity = and_(
            edges.c.school_id == school_id,
            edges.c.source_entity_id == source_entity_id,
            edges.c.target_entity_id == target_entity_id,
            edges.c.relation == relation,
        )
        with self._engine.begin() as connection:
            found_entity_ids = connection.execute(
                select(entities.c.entity_id).where(
                    and_(
                        entities.c.school_id == school_id,
                        entities.c.entity_id.in_([source_entity_id, target_entity_id]),
                    )
                )
            ).scalars().all()
            _assert_entities_belong_to_school(
                school_id, [source_entity_id, target_entity_id], found_entity_ids
            )
            existing = connection.execute(select(edges).where(identity)).mappings().one_or_none()
            if existing is None:
                return connection.execute(
                    insert(edges)
                    .values(
                        school_id=school_id,
                        source_entity_id=source_entity_id,
                        target_entity_id=target_entity_id,
                        relation=relation,
                        source_doc_ids=_merge_ids([], source_doc_ids),
                    )
                    .returning(edges.c.edge_id)
                ).scalar_one()

            merged_doc_ids = _merge_ids(existing["source_doc_ids"], source_doc_ids)
            return connection.execute(
                edges.update()
                .where(edges.c.edge_id == existing["edge_id"])
                .values(source_doc_ids=merged_doc_ids)
                .returning(edges.c.edge_id)
            ).scalar_one()

    def doc_hash_exists(self, school_id: int, source_url: str, content_hash: str) -> bool:
        """같은 학교·원문 URL·본문 해시의 저장 청크가 하나라도 있는지 확인한다."""

        statement = select(documents.c.doc_id).where(
            and_(
                documents.c.school_id == school_id,
                documents.c.source_url == source_url,
                documents.c.content_hash == content_hash,
            )
        ).limit(1)
        with self._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def doc_url_exists(self, school_id: int, source_url: str) -> bool:
        """같은 학교에서 원문 URL이 이미 처리된 적 있는지 확인한다."""

        statement = select(documents.c.doc_id).where(
            and_(
                documents.c.school_id == school_id,
                documents.c.source_url == source_url,
            )
        ).limit(1)
        with self._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def vector_search(
        self,
        school_id: int,
        query_embedding: Sequence[float],
        k: int,
        *,
        source_type: str | None = None,
    ) -> list[tuple[Document, float]]:
        """같은 학교 안에서 코사인 유사도 기준 상위 ``k`` 청크를 찾는다.

        ``source_type`` 을 주면 그 풀만 검색한다 — 그래프 RAG 는 ``'web'``,
        문서 RAG 는 ``'attachment'`` 로 단계를 분리한다(07_graph-rag-engine.md).
        생략하면 구분 없이 전체를 검색한다.
        """

        _validate_embedding(query_embedding)
        if k <= 0:
            return []
        distance = documents.c.embedding.cosine_distance(list(query_embedding))
        conditions = [
            documents.c.school_id == school_id,
            documents.c.embedding.is_not(None),
            documents.c.expired_at.is_(None),
        ]
        if source_type is not None:
            conditions.append(documents.c.source_type == source_type)
        statement = (
            select(*documents.c, (1 - distance).label("score"))
            .where(and_(*conditions))
            .order_by(distance)
            .limit(k)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [(_document(row), float(row["score"])) for row in rows]

    def entities_by_norm_keys(self, school_id: int, norm_keys: Sequence[str]) -> list[Entity]:
        """정규화 키로 학교 범위 안의 엔티티를 찾는다. RAG 질의 엔티티 매핑용."""

        if not norm_keys:
            return []
        statement = select(entities).where(
            and_(entities.c.school_id == school_id, entities.c.norm_key.in_(list(norm_keys)))
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_entity(row) for row in rows]

    def neighbors(self, school_id: int, entity_ids: Sequence[int], *, hops: int = 1) -> list[Neighbor]:
        """지정 엔티티의 1-hop 이웃만 학교 범위 안에서 반환한다."""

        if hops != 1:
            raise ValueError("MVP Storage는 1-hop 조회만 지원합니다")
        if not entity_ids:
            return []

        source = entities.alias("source")
        target = entities.alias("target")
        statement = (
            select(
                *[column.label(f"source_{column.name}") for column in source.c],
                *[column.label(f"edge_{column.name}") for column in edges.c],
                *[column.label(f"target_{column.name}") for column in target.c],
            )
            .select_from(
                edges.join(source, edges.c.source_entity_id == source.c.entity_id).join(
                    target, edges.c.target_entity_id == target.c.entity_id
                )
            )
            .where(
                and_(
                    edges.c.school_id == school_id,
                    source.c.school_id == school_id,
                    target.c.school_id == school_id,
                    or_(edges.c.source_entity_id.in_(entity_ids), edges.c.target_entity_id.in_(entity_ids)),
                )
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            Neighbor(
                source=_entity({key.removeprefix("source_"): value for key, value in row.items() if key.startswith("source_")}),
                edge=_edge({key.removeprefix("edge_"): value for key, value in row.items() if key.startswith("edge_")}),
                target=_entity({key.removeprefix("target_"): value for key, value in row.items() if key.startswith("target_")}),
            )
            for row in rows
        ]

    def get_documents(self, school_id: int, doc_ids: Sequence[int]) -> list[Document]:
        """학교 범위 안의 문서만 반환한다."""

        if not doc_ids:
            return []
        statement = select(documents).where(
            and_(documents.c.school_id == school_id, documents.c.doc_id.in_(list(doc_ids)))
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_document(row) for row in rows]

    def record_url_observations(self, school_id: int, observed_urls: Sequence[str]) -> None:
        """재크롤에서 관측된 URL은 miss_count를 0으로, 미관측 URL은 1 증가시킨다.

        이미 만료된 문서(expired_at IS NOT NULL)는 건드리지 않는다.
        observed_urls가 비어 있으면 no-op (실패·빈 수집으로 전량 미관측 처리 방지).

        대상은 크롤링 청크('web')뿐이다. 업로드 첨부는 크롤러가 관측할 수 없어
        재크롤마다 미관측으로 집계되고, 그대로 두면 멀쩡한 첨부가 만료된다.
        """

        urls = [u for u in dict.fromkeys(observed_urls) if u]
        if not urls:
            return

        active = and_(
            documents.c.school_id == school_id,
            documents.c.expired_at.is_(None),
            documents.c.source_type == SOURCE_TYPE_WEB,
        )
        with self._engine.begin() as connection:
            connection.execute(
                documents.update()
                .where(and_(active, documents.c.source_url.in_(urls)))
                .values(miss_count=0)
            )
            connection.execute(
                documents.update()
                .where(and_(active, documents.c.source_url.notin_(urls)))
                .values(miss_count=documents.c.miss_count + 1)
            )

    def expire_documents_by_miss_count(
        self,
        school_id: int | None = None,
        threshold: int = 3,
    ) -> list[int]:
        """연속 미관측 횟수가 threshold 이상인 문서를 만료(expired_at 기록)하고 doc_id 목록을 반환한다."""

        if threshold < 1:
            raise ValueError("threshold는 1 이상이어야 합니다")

        conditions = [
            documents.c.miss_count >= threshold,
            documents.c.expired_at.is_(None),
        ]
        if school_id is not None:
            conditions.append(documents.c.school_id == school_id)

        statement = (
            documents.update()
            .where(and_(*conditions))
            .values(expired_at=func.now())
            .returning(documents.c.doc_id)
        )
        with self._engine.begin() as connection:
            return list(connection.execute(statement).scalars().all())
