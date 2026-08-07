"""Extractor 산출물을 정규화해 벡터·지식그래프로 저장하는 Graph Builder.

HTML 구조나 학교별 파서는 다루지 않는다. ``ExtractedChunk`` 공통 계약을 받아
임베딩, Storage upsert, 이름 기반 관계 해소만 담당한다.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from app.llm import Embedder
from app.schemas import BuildResult, ExtractedChunk


LOGGER = logging.getLogger(__name__)
_NON_KEY_CHARACTERS = re.compile(r"[\W_]+", re.UNICODE)


class GraphStorage(Protocol):
    """Graph Builder가 사용하는 Storage 공개 메서드의 최소 계약."""

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
    ) -> int: ...

    def upsert_entity(
        self,
        school_id: int,
        type: str,
        name: str,
        norm_key: str,
        attributes: dict[str, Any],
        source_doc_ids: Sequence[int],
    ) -> int: ...

    def upsert_edge(
        self,
        school_id: int,
        source_entity_id: int,
        target_entity_id: int,
        relation: str,
        source_doc_ids: Sequence[int],
    ) -> int: ...


def normalize_entity_key(entity_type: str, name: str) -> str:
    """학교 내부 엔티티 병합에 쓰는 안정적인 ``type:name`` 키를 만든다.

    타입까지 포함해야 같은 표기라도 서로 다른 엔티티 종류가 병합되지 않는다.
    동의어 사전·의미 유사도 병합은 MVP 범위 밖이다.
    """

    def compact(value: str) -> str:
        normalised = unicodedata.normalize("NFKC", value).casefold()
        return _NON_KEY_CHARACTERS.sub("", normalised)

    return f"{compact(entity_type)}:{compact(name)}"


class GraphBuilder:
    """청크 하나를 문서·엔티티·엣지로 저장한다.

    변경 감지는 Crawler가 원문 페이지 단위로 담당한다. 긴 문서의 모든 청크가 같은
    content_hash를 공유할 수 있으므로 Builder는 전달받은 청크를 해시만으로 건너뛰지
    않고 Storage의 청크 단위 upsert를 호출한다.
    """

    def __init__(self, storage: GraphStorage, embedder: Embedder) -> None:
        self.storage = storage
        self.embedder = embedder

    def build(self, school_id: int, extracted_chunk: ExtractedChunk) -> BuildResult:
        """정규화 → 임베딩 → 문서·엔티티·엣지를 순서대로 저장한다."""
        if school_id != extracted_chunk.school_id:
            return self._failed(
                extracted_chunk,
                "SCHOOL_ID_MISMATCH",
                retryable=False,
                warnings=["build school_id와 ExtractedChunk.school_id가 다릅니다."],
            )

        try:
            embedding = self.embedder.embed(extracted_chunk.content)
        except Exception as error:
            return self._failed(
                extracted_chunk,
                "EMBEDDING_FAILED",
                retryable=True,
                warnings=[self._error_warning("embedding", error)],
            )

        try:
            doc_id = self.storage.upsert_document(
                school_id,
                extracted_chunk.source_url,
                extracted_chunk.title,
                extracted_chunk.content,
                extracted_chunk.chunk_index,
                extracted_chunk.content_hash,
                embedding,
                crawled_at=extracted_chunk.crawled_at,
            )
        except Exception as error:
            return self._failed(
                extracted_chunk,
                "DOCUMENT_STORE_FAILED",
                retryable=True,
                warnings=[self._error_warning("document", error)],
            )

        warnings: list[str] = []
        is_partial = extracted_chunk.extraction_status == "partial"
        if is_partial:
            warnings.append("입력 ExtractedChunk가 partial 상태입니다.")

        entity_ids: list[int] = []
        name_to_ids: dict[str, set[int]] = {}
        for entity in extracted_chunk.entities:
            norm_key = normalize_entity_key(entity.type, entity.name)
            if not entity.type.strip() or not entity.name.strip():
                is_partial = True
                warnings.append("빈 타입 또는 이름 엔티티를 건너뛰었습니다.")
                continue
            try:
                entity_id = self.storage.upsert_entity(
                    school_id,
                    entity.type,
                    entity.name,
                    norm_key,
                    entity.attributes,
                    [doc_id],
                )
            except Exception as error:
                is_partial = True
                warnings.append(self._error_warning(f"entity:{entity.name}", error))
                continue
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
            name_to_ids.setdefault(entity.name, set()).add(entity_id)

        edge_ids: list[int] = []
        for relation in extracted_chunk.relations:
            source_id = self._resolve_name(relation.source, name_to_ids, warnings)
            target_id = self._resolve_name(relation.target, name_to_ids, warnings)
            if source_id is None or target_id is None:
                is_partial = True
                continue
            try:
                edge_id = self.storage.upsert_edge(
                    school_id,
                    source_id,
                    target_id,
                    relation.relation,
                    [doc_id],
                )
            except Exception as error:
                is_partial = True
                warnings.append(self._error_warning(
                    f"edge:{relation.source}-{relation.relation}->{relation.target}", error
                ))
                continue
            if edge_id not in edge_ids:
                edge_ids.append(edge_id)

        return BuildResult(
            school_id=school_id,
            source_url=extracted_chunk.source_url,
            content_hash=extracted_chunk.content_hash,
            doc_id=doc_id,
            entity_ids=entity_ids,
            edge_ids=edge_ids,
            status="partial" if is_partial else "complete",
            warnings=warnings,
        )

    @staticmethod
    def _resolve_name(name: str, name_to_ids: dict[str, set[int]], warnings: list[str]) -> int | None:
        candidates = name_to_ids.get(name, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        if not candidates:
            warnings.append(f"관계 끝 엔티티를 찾지 못해 건너뛰었습니다: {name}")
        else:
            warnings.append(f"동일 이름 엔티티가 여러 타입이라 관계를 건너뛰었습니다: {name}")
        return None

    @staticmethod
    def _error_warning(stage: str, error: Exception) -> str:
        detail = str(error).replace("\n", " ").strip()
        return f"{stage} 처리 실패: {type(error).__name__}{f' ({detail})' if detail else ''}"

    @staticmethod
    def _failed(
        chunk: ExtractedChunk,
        error_code: str,
        *,
        retryable: bool,
        warnings: list[str],
    ) -> BuildResult:
        LOGGER.warning("graph build failed for %s: %s", chunk.source_url, error_code)
        return BuildResult(
            school_id=chunk.school_id,
            source_url=chunk.source_url,
            content_hash=chunk.content_hash,
            status="failed",
            error_code=error_code,
            retryable=retryable,
            warnings=warnings,
        )
