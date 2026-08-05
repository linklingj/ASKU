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
  school_id         BIGSERIAL PRIMARY KEY,
  name              TEXT NOT NULL,
  base_url          TEXT NOT NULL,
  crawl_schedule    TEXT,                 -- 예: 'daily', 'weekly'
  status            TEXT NOT NULL DEFAULT 'idle',
  crawl_started_at  TIMESTAMPTZ,          -- 수집·인덱싱 시작 시각
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now()
);

-- 첨부 문서 = 사용자가 학교에 직접 올린 파일(수강편람 PDF·학칙 HWP 등) 한 건.
-- 파일 바이트는 보관하지 않는다. 뽑아낸 텍스트를 documents 청크로만 남긴다.
CREATE TABLE attachments (
  attachment_id BIGSERIAL PRIMARY KEY,
  school_id     BIGINT NOT NULL REFERENCES schools(school_id),
  filename      TEXT NOT NULL,
  content_type  TEXT,
  byte_size     BIGINT NOT NULL,
  file_hash     TEXT NOT NULL,          -- 파일 바이트 sha256 (재업로드 멱등 키)
  page_count    INT NOT NULL DEFAULT 0, -- 인용 단위 수(PDF=페이지, HWP=구역)
  chunk_count   INT NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|indexing|ready|failed
  error_code    TEXT,                   -- status='failed' 일 때만
  uploaded_at   TIMESTAMPTZ DEFAULT now()
);

-- 문서 = 검색·인용의 최소 단위(청크). 공지 1건 = 기본 1행.
-- 긴 문서는 여러 행으로 분할하되 source_url·title을 공유하고 chunk_index로 구분.
CREATE TABLE documents (
  doc_id        BIGSERIAL PRIMARY KEY,
  school_id     BIGINT NOT NULL REFERENCES schools(school_id),
  source_url    TEXT NOT NULL,          -- 원문 링크. 첨부 청크는 attachment://{id}
  title         TEXT,                   -- 첨부 청크는 파일명
  content       TEXT NOT NULL,          -- 청크 본문
  chunk_index   INT DEFAULT 0,          -- 같은 원문 내 순번
  content_hash  TEXT NOT NULL,          -- 변경 감지용(본문 해시)
  embedding     vector(1024),
  crawled_at    TIMESTAMPTZ DEFAULT now(),
  miss_count    INT NOT NULL DEFAULT 0, -- 연속 미관측 횟수 (Scheduler 만료)
  expired_at    TIMESTAMPTZ,            -- 만료 확정 시각 (NULL이면 유효)
  source_type   TEXT NOT NULL DEFAULT 'web',  -- 'web'(크롤링) | 'attachment'(업로드)
  page          INT,                    -- 첨부의 페이지·구역 번호. 'web' 은 NULL
  attachment_id BIGINT REFERENCES attachments(attachment_id)  -- 'web' 은 NULL
);

-- 엔티티(노드)
CREATE TABLE entities (
  entity_id       BIGSERIAL PRIMARY KEY,
  school_id       BIGINT NOT NULL REFERENCES schools(school_id),
  type            TEXT NOT NULL,        -- 엔티티 타입 화이트리스트 → 04_extractor.md §2.2
  name            TEXT NOT NULL,
  norm_key        TEXT NOT NULL,        -- 정규화 키(중복 병합용). (school_id, norm_key) 유니크
  attributes      JSONB DEFAULT '{}',
  source_doc_ids  BIGINT[] NOT NULL     -- 근거 문서(최소 1개)
);

-- 엣지(관계)
CREATE TABLE edges (
  edge_id           BIGSERIAL PRIMARY KEY,
  school_id         BIGINT NOT NULL REFERENCES schools(school_id),
  source_entity_id  BIGINT NOT NULL REFERENCES entities(entity_id),
  target_entity_id  BIGINT NOT NULL REFERENCES entities(entity_id),
  relation          TEXT NOT NULL,      -- 관계 타입 화이트리스트 → 04_extractor.md §2.3
  source_doc_ids    BIGINT[] NOT NULL   -- 근거 문서(최소 1개)
);

CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON documents (school_id);
CREATE INDEX ON documents (school_id, source_type);   -- RAG 단계별 검색 풀 분리
CREATE INDEX ON documents (attachment_id);            -- 첨부 삭제 시 청크 정리
CREATE INDEX ON attachments (school_id);
CREATE UNIQUE INDEX ON attachments (school_id, file_hash);
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
upsert_document(school_id, source_url, title, content, chunk_index, content_hash, embedding,
                *, source_type='web', page=None, attachment_id=None) -> doc_id
upsert_entity(school_id, type, name, norm_key, attributes, source_doc_ids) -> entity_id
upsert_edge(school_id, source_entity_id, target_entity_id, relation, source_doc_ids) -> edge_id

vector_search(school_id, query_embedding, k, *, source_type=None)
                                               -> [doc_id, source_url, title, content, score]
entities_by_norm_keys(school_id, norm_keys) -> [entity]        # RAG 질의 엔티티 매핑용
neighbors(school_id, entity_ids, hops=1) -> [entity, edge, source_doc_ids]
get_documents(school_id, doc_ids) -> [document]

doc_hash_exists(school_id, source_url, content_hash) -> bool   # 증분 갱신용
doc_url_exists(school_id, source_url) -> bool                  # 변경 감지용

create_attachment(school_id, filename, content_type, byte_size, file_hash) -> attachment
get_attachment(school_id, attachment_id) -> attachment | None
list_attachments(school_id) -> [attachment]                    # 최신 업로드 순
update_attachment_status(school_id, attachment_id, status,
                         *, page_count=None, chunk_count=None, error_code=None) -> attachment | None
delete_attachment(school_id, attachment_id) -> bool            # 첨부 + 그 청크 함께 삭제
count_ready_attachments(school_id) -> int                      # 질의 가능 여부 판정용
```

`vector_search` 의 `source_type` 은 RAG 단계별 검색 풀을 가른다 — 그래프 RAG 는 `'web'`,
문서 RAG 는 `'attachment'` ([`07_graph-rag-engine.md`](07_graph-rag-engine.md)).
생략하면 구분 없이 전체를 검색한다.

`upsert_*`는 유니크 키 충돌 시 갱신(증분 업데이트 지원).
관련: [`05_graph-builder.md`](05_graph-builder.md), [`07_graph-rag-engine.md`](07_graph-rag-engine.md),
[`10_data-model.md`](10_data-model.md).

## 5. 책임 범위와 데이터 수명

Storage는 다음 데이터를 Postgres에 영속하고 공개 인터페이스로만 제공한다.

| 데이터 | 생산자 | 저장 위치·역할 |
|---|---|---|
| School | Backend API | `schools`: 학교 격리와 수집 주기 기준 |
| 첨부 파일 메타 | Backend API(업로드) | `attachments`: 파일 단위 색인 상태·집계 |
| 정제 청크·임베딩 | Graph Builder · 첨부 인제스터 | `documents`: 검색·인용의 최소 단위 |
| 엔티티·속성 | Graph Builder | `entities`: 그래프 노드와 근거 문서 목록 |
| 관계 | Graph Builder | `edges`: 1-hop 그래프 확장 |
| 실행·오류 이력 | 각 시스템 | 별도 실행 이력 저장소 또는 테이블 — 스키마는 미정 |

Crawler의 원본 HTML은 Extractor로 전달되는 처리 입력이다. 현재 확정 스키마는 원문 URL과 정제 청크를 근거로 보관하며, 원본 HTML·첨부파일 바이너리의 영구 저장은 확정되지 않았다. 이를 필요로 하면 원문 저장소 또는 별도 테이블을 추가하되 `source_url`과 해시 연결을 유지해야 한다.

Storage는 URL 수집·HTML 파싱·LLM 호출·엔티티 의미 판단·스케줄 결정을 하지 않는다. 모든 업무 시스템은 이 문서의 공개 인터페이스를 사용하며 SQL과 DB 클라이언트를 직접 노출하지 않는다.

## 6. Upsert, 삭제, 오류 처리

- `upsert_document`는 `school_id + source_url + content_hash + chunk_index` 기준으로 같은 청크를 중복 생성하지 않아야 한다. 실제 유니크 제약의 정확한 DDL은 구현 전 확정한다.
- `upsert_entity`는 `(school_id, norm_key)` 충돌 시 `source_doc_ids`를 중복 없이 누적하고, 같은 속성 키는 새 문서의 값으로 갱신한다. 속성 이력 관리는 MVP 범위 밖이다.
- `upsert_edge`는 두 엔티티가 요청 `school_id`에 모두 속할 때만 생성한다. `school_id + source_entity_id + target_entity_id + relation`을 중복 키로 사용하며, 같은 관계는 하나의 엣지로 병합하고 `source_doc_ids`에 근거 문서를 누적한다.
- MVP는 문서·엔티티·엣지의 유니크 키를 멱등 키로 사용해 중복 반영을 막는다. 실행 이력 테이블과 요청 ID 추적은 Crawler·Scheduler 도입 시 별도로 추가한다.
- pgvector 임베딩과 관계 테이블 반영이 일부만 성공하면 MVP에서는 오류를 호출자에게 전파한다. 자동 실행 이력, 보상·재처리 큐는 후속 작업으로 유보한다.
- Crawler의 단일 미관측만으로 삭제하지 않는다. Scheduler가 연속 미관측 N회(`documents.miss_count`) 뒤에 `expired_at`을 기록해 만료를 확정한다. 벡터 검색은 `expired_at IS NULL`인 문서만 반환한다. 만료 문서의 그래프 기여분 물리 삭제 여부는 **미정**이다.
- 미관측 집계(`record_url_observations`)는 `source_type='web'` 청크만 대상으로 한다. 업로드 첨부는 크롤러가 관측할 수 없어, 포함하면 재크롤링마다 미관측으로 집계돼 멀쩡한 첨부가 만료된다.
- 첨부는 만료 정책이 아니라 **사용자 삭제**로만 사라진다. `delete_attachment`가 첨부 행과 그 청크(`documents.attachment_id`)를 함께 지운다.
- `create_attachment`는 `(school_id, file_hash)` 충돌 시 새 행을 만들지 않고 기존 첨부를 `pending`으로 되돌린다. 같은 파일을 다시 올려도 청크는 `source_url = attachment://{id}` 기준으로 멱등 업서트되므로 중복이 쌓이지 않는다.

## 7. 조회·백업·확장

- RAG 조회는 모든 `vector_search`, `entities_by_norm_keys`, `neighbors`, `get_documents` 호출에 `school_id` 필터를 강제한다.
- 인덱스는 기존 HNSW 벡터 인덱스, `documents(school_id)`, `entities(school_id, norm_key)`, `edges(school_id, source_entity_id)`를 기본으로 한다. 해시·URL 조합 인덱스 추가 여부는 실제 데이터 규모를 측정해 결정한다.
- 백업 범위는 `schools`, `documents`, `entities`, `edges`, 실행 이력, 그리고 추후 도입될 원문 저장소다. 백업 주기, RPO/RTO, 복구 리허설, 접근 제어·개인정보 마스킹은 **미정**이다.
- 단일 Postgres + pgvector + edges 테이블은 현재 확정 구조다. 그래프 순회가 측정상 병목일 때만 Neo4j 등 별도 Graph DB를 재검토한다.
- 새 데이터 유형은 공통 `school_id`, 출처, 해시·버전 연결을 유지한 뒤 Extractor 화이트리스트와 Storage 스키마를 함께 변경한다.
