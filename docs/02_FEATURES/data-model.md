# 데이터 모델 & 파이프라인 DTO (`models` / `schemas`)

모든 모듈이 공유하는 데이터 구조·공통 ID·불변식을 코드로 정의한다. 개념·DDL의 단일 기준은
[`01_SYSTEM/10_data-model.md`](../01_SYSTEM/10_data-model.md)·[`06_storage.md`](../01_SYSTEM/06_storage.md)이고,
이 문서는 **다른 모듈이 쓰는 공개 타입과 그 사용법·강제 지점**을 정리한다.

## 제공 타입

| 모듈 | 타입 | 성격 |
|---|---|---|
| `app/models.py` | `School` · `Document` · `Entity` · `Edge` | 영속 엔터티(DB 행 1:1). PK 있음. |
| `app/schemas.py` | `CrawlRequest` · `CrawledPage` · `CrawlFailure` | Crawler 계약(03) |
| | `ExtractedChunk` · `ExtractionFailure` | Extractor 계약(04) |
| | `BuildResult` | Graph Builder 계약(05) |
| | `ExtractedEntity` · `ExtractedRelation` · `Attachment` · `CrawlScope` | 하위 스키마 |

Pydantic v2 모델이라 JSON ↔ 객체 변환·검증이 자동이다.

```python
from app.models import Entity, EMBEDDING_DIM
from app.schemas import CrawlRequest, BuildResult

req = CrawlRequest(crawl_id=uuid4(), school_id=1, base_url="https://...", mode="initial")
result = BuildResult.model_validate(json_payload)   # 검증 실패 시 ValidationError
```

> DTO의 엔티티/관계(`ExtractedEntity`·`ExtractedRelation`)는 **이름 기반**이라 영속 ID가 없다.
> ID 부여·정규화·임베딩은 Graph Builder가 하고, 그 결과가 `models.py`의 `Entity`/`Edge`다.

## 공통 ID 규칙

| 용도 | 규칙 | 코드 |
|---|---|---|
| DB 기본키 | `BIGSERIAL` (insert 전 없음) | `models.py`: `*_id: int | None = None` |
| 실행 추적 | `crawl_id` = **UUID** | `schemas.py`: `crawl_id: UUID` (문자열이면 검증 거부) |
| 청크 식별 | `school_id + source_url + content_hash + chunk_index` | 증분 갱신·재처리 방지 키 |

## 공통 필드 강제

- `school_id`: 경계를 넘는 **모든** 모델에 필수(학교 격리). 빠지면 `ValidationError`.
- `source_url` / `content_hash`: 근거 추적·변경 감지 키. Document·DTO에 필수.
- `source_doc_ids`: Entity·Edge의 근거 문서. 비어 있으면 안 됨(아래 불변식 ①).

## 불변식 → 강제 지점

| # | 불변식 | 강제 방법 |
|---|---|---|
| ① | 근거 추적성 — 근거 없는 노드/엣지 금지 | **코드**: `Entity`/`Edge.source_doc_ids` `min_length=1` |
| ② | 학교 격리 — 학교 간 엣지·조회 없음 | **DB/조회**: 모든 쿼리 `school_id` 필터(Storage/RAG 책임). 모델은 `school_id` 필수화까지. |
| ③ | 엔티티 유일성 — `(school_id, norm_key)` 유일 | **DB**: UNIQUE 인덱스 + `upsert_entity` 병합([`06_storage.md`](../01_SYSTEM/06_storage.md)) |
| ④ | 임베딩 차원 = 1024 | **코드**: `Document.embedding` 길이 검증, 상수 `EMBEDDING_DIM` |

추가로 `BuildResult`는 `status`↔필드 계약을 강제한다: `failed`면 `error_code` 필수, 그 외엔 `doc_id` 필수.

## 자체 점검

`backend/tests/test_models.py` — 엔터티 불변식을 검증한다.

```bash
cd backend && python -m pytest tests/ -q
```
