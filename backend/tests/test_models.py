"""핵심 엔터티 불변식 자체 점검. `python backend/tests/test_models.py` 또는 pytest."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic import ValidationError

from app.models import EMBEDDING_DIM, Document, Edge, Entity, School


def test_valid_construction():
    School(name="연세대", base_url="https://yonsei.ac.kr")
    Document(school_id=1, source_url="https://x", content="본문", content_hash="h")
    Entity(school_id=1, type="장학금", name="성적우수", norm_key="성적우수", source_doc_ids=[1])
    Edge(school_id=1, source_entity_id=1, target_entity_id=2, relation="담당", source_doc_ids=[1])


def test_embedding_dim_enforced():
    Document(school_id=1, source_url="u", content="c", content_hash="h",
             embedding=[0.0] * EMBEDDING_DIM)  # ok
    with pytest.raises(ValidationError):
        Document(school_id=1, source_url="u", content="c", content_hash="h",
                 embedding=[0.0] * (EMBEDDING_DIM - 1))


def test_evidence_required():  # 불변식 ①: 근거 없는 노드/엣지 금지
    with pytest.raises(ValidationError):
        Entity(school_id=1, type="공지", name="n", norm_key="n", source_doc_ids=[])
    with pytest.raises(ValidationError):
        Edge(school_id=1, source_entity_id=1, target_entity_id=2, relation="안내", source_doc_ids=[])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
