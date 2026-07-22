# 저장소 (Graph DB / Vector DB)

크롤링·추출·검색이 공유하는 영속 계층. **Postgres 하나**로 관계형 메타데이터,
벡터 검색(pgvector), 경량 그래프(엣지 테이블)를 모두 담는다.

> **결정**
> - Vector DB: Qdrant/Chroma 후보 → **pgvector**. 별도 프로세스 없이 Postgres 통합.
> - Graph DB: Neo4j → **Postgres `edges` 테이블**. 경량 그래프는 1-hop 조인이면 충분.
> - Neo4j는 그래프 순회가 실제 병목으로 측정될 때만 재검토한다(YAGNI).

## 1. 구성

| 관심사 | 수단 |
|---|---|
| 학교·문서·엔티티 메타데이터 | Postgres 테이블 |
| 청크 임베딩 검색 | pgvector 확장 (`vector` 컬럼 + HNSW 인덱스) |
| 엔티티 관계(그래프) | `edges` 테이블 + 인접 조인 |
| 원문 근거 | `documents.source_url` |

## 2. 스키마

임베딩 차원은 임베딩 모델(bge-m3)에 맞춰 **1024**. 모델 교체 시 함께 바뀐다
([`08_llm-provider.md`](08_llm-provider.md) 참고).

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 학교
CREATE TABLE schools (
  school_id       BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  base_url        TEXT NOT NULL,
  crawl_schedule  TEXT,                 -- 예: 'daily', 'weekly'
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);

-- 문서 = 검색·인용의 최소 단위(청크). 공지 1건 = 기본 1행.
-- 긴 문서는 여러 행으로 분할하되 source_url·title을 공유하고 chunk_index로 구분.
CREATE TABLE documents (
  doc_id        BIGSERIAL PRIMARY KEY,
  school_id     BIGINT NOT NULL REFERENCES schools(school_id),
  source_url    TEXT NOT NULL,          -- 원문 링크(근거 제공용)
  title         TEXT,
  content       TEXT NOT NULL,          -- 청크 본문
  chunk_index   INT DEFAULT 0,          -- 같은 원문 내 순번
  content_hash  TEXT NOT NULL,          -- 변경 감지용(본문 해시)
  embedding     vector(1024),
  crawled_at    TIMESTAMPTZ DEFAULT now()
);

-- 엔티티(노드)
CREATE TABLE entities (
  entity_id       BIGSERIAL PRIMARY KEY,
  school_id       BIGINT NOT NULL REFERENCES schools(school_id),
  type            TEXT NOT NULL,        -- 엔티티 타입 화이트리스트 → 04_extractor.md §2.2
  name            TEXT NOT NULL,
  norm_key        TEXT NOT NULL,        -- 정규화 키(중복 병합용). (school_id, norm_key) 유니크
  attributes      JSONB DEFAULT '{}',
  source_doc_ids  BIGINT[] DEFAULT '{}' -- 근거 문서
);

-- 엣지(관계)
CREATE TABLE edges (
  edge_id           BIGSERIAL PRIMARY KEY,
  school_id         BIGINT NOT NULL REFERENCES schools(school_id),
  source_entity_id  BIGINT NOT NULL REFERENCES entities(entity_id),
  target_entity_id  BIGINT NOT NULL REFERENCES entities(entity_id),
  relation          TEXT NOT NULL,      -- 관계 타입 화이트리스트 → 04_extractor.md §2.3
  source_doc_ids    BIGINT[] DEFAULT '{}'
);

CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON documents (school_id);
CREATE UNIQUE INDEX ON entities (school_id, norm_key);
CREATE INDEX ON edges (school_id, source_entity_id);
```

> 모든 노드·엣지·문서는 `source_doc_ids`/`source_url`로 원문과 연결돼 근거 추적이 된다.

## 3. 멀티 학교 격리

모든 조회 쿼리는 **`school_id` 필터를 필수**로 건다. 학교 간 데이터 오염 방지.
스키마/DB 분리는 하지 않는다(오버킬).

## 4. 공개 인터페이스

다른 모듈은 아래 함수로만 저장소에 접근한다(SQL 직접 노출 금지).

```
upsert_document(school_id, source_url, title, content, chunk_index, content_hash, embedding) -> doc_id
upsert_entity(school_id, type, name, norm_key, attributes, source_doc_ids) -> entity_id
upsert_edge(school_id, source_entity_id, target_entity_id, relation, source_doc_ids) -> edge_id

vector_search(school_id, query_embedding, k) -> [doc_id, source_url, title, content, score]
neighbors(school_id, entity_ids, hops=1) -> [entity, edge, source_doc_ids]
get_documents(doc_ids) -> [document]

doc_hash_exists(school_id, source_url, content_hash) -> bool   # 증분 갱신용
```

`upsert_*`는 유니크 키 충돌 시 갱신(증분 업데이트 지원).
관련: [`05_graph-builder.md`](05_graph-builder.md), [`07_graph-rag-engine.md`](07_graph-rag-engine.md),
[`10_data-model.md`](10_data-model.md).
