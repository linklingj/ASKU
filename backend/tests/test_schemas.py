"""파이프라인 DTO 계약 자체 점검. `python backend/tests/test_schemas.py` 또는 pytest."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic import ValidationError

from app.schemas import BuildResult, CrawledPage, CrawlRequest, ExtractedChunk


def test_valid_pipeline_dtos():
    cid = uuid4()
    CrawlRequest(crawl_id=cid, school_id=1, base_url="https://u", mode="initial")
    CrawledPage(crawl_id=cid, school_id=1, source_url="u", canonical_url="u",
                raw_html="<html>", content_hash="h", fetched_at="2026-07-25T00:00:00Z",
                crawl_status="new")
    ExtractedChunk(school_id=1, source_url="u", content="c", content_hash="h",
                   entities=[], relations=[], extraction_status="complete")


def test_crawl_id_must_be_uuid():  # 공통 ID 규칙: 실행 추적 = UUID
    with pytest.raises(ValidationError):
        CrawlRequest(crawl_id="not-a-uuid", school_id=1, base_url="u", mode="initial")


def test_status_enum_enforced():  # Literal 화이트리스트
    with pytest.raises(ValidationError):
        CrawlRequest(crawl_id=uuid4(), school_id=1, base_url="u", mode="bogus")


def test_build_result_status_contract():
    BuildResult(school_id=1, source_url="u", content_hash="h", doc_id=101, status="complete")
    BuildResult(school_id=1, source_url="u", content_hash="h", status="failed",
                error_code="E_STORAGE")
    with pytest.raises(ValidationError):  # 성공인데 doc_id 없음
        BuildResult(school_id=1, source_url="u", content_hash="h", status="complete")
    with pytest.raises(ValidationError):  # 실패인데 error_code 없음
        BuildResult(school_id=1, source_url="u", content_hash="h", status="failed")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
