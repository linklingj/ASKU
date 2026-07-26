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

from app.models import EMBEDDING_DIM, Document, Edge, Entity, School


metadata = MetaData()

schools = Table(
    "schools",
    metadata,
    Column("school_id", BIGINT, primary_key=True),
    Column("name", TEXT, nullable=False),
    Column("base_url", TEXT, nullable=False),
    Column("crawl_schedule", TEXT),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
)

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
        """pgvector 확장과 Storage 테이블·인덱스를 생성한다."""

        with self._engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            metadata.create_all(connection)

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
    ) -> int:
        """청크를 해시 기준으로 멱등 저장하고 ``doc_id``를 반환한다."""

        _validate_embedding(embedding)
        values: dict[str, Any] = {
            "school_id": school_id,
            "source_url": source_url,
            "title": title,
            "content": content,
            "chunk_index": chunk_index,
            "content_hash": content_hash,
            "embedding": list(embedding) if embedding is not None else None,
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

    def vector_search(
        self, school_id: int, query_embedding: Sequence[float], k: int
    ) -> list[tuple[Document, float]]:
        """같은 학교 안에서 코사인 유사도 기준 상위 ``k`` 청크를 찾는다."""

        _validate_embedding(query_embedding)
        if k <= 0:
            return []
        distance = documents.c.embedding.cosine_distance(list(query_embedding))
        statement = (
            select(*documents.c, (1 - distance).label("score"))
            .where(and_(documents.c.school_id == school_id, documents.c.embedding.is_not(None)))
            .order_by(distance)
            .limit(k)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [(_document(row), float(row["score"])) for row in rows]

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
            and_(documents.c.school_id == school_id, documents.c.doc_id.in_(doc_ids))
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_document(row) for row in rows]
