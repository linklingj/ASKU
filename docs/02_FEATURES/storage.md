# Storage 기능

`backend/app/storage.py`는 Postgres/pgvector에 대한 유일한 저장소 접근 계층이다. Crawler, Graph Builder, Graph RAG는 DB 클라이언트나 SQL을 직접 사용하지 않고 `Storage` 공개 메서드를 사용한다.

## 환경 설정

`backend/.env.example`을 참고해 `DATABASE_URL`을 설정한다.

```text
postgresql+psycopg://<user>:<password>@<host>:5432/<database>
```

대상 Postgres에는 pgvector 확장이 설치돼 있어야 한다. 초기화 시 `Storage.create_schema()`가 `vector` 확장과 `schools`, `documents`, `entities`, `edges` 테이블·인덱스를 생성한다.

## 테스트

```bash
PYTHONPATH=backend python -m unittest discover -s backend/tests -v
```

실제 Postgres/pgvector 통합 검증 전에는 Docker 등으로 `DATABASE_URL`이 가리키는 테스트용 데이터베이스를 준비해야 한다.

## 공개 메서드

- `create_school(school) -> School`
- `upsert_document(...) -> doc_id`
- `upsert_entity(...) -> entity_id`
- `upsert_edge(...) -> edge_id`
- `doc_hash_exists(school_id, source_url, content_hash) -> bool`
- `doc_url_exists(school_id, source_url) -> bool`
- `vector_search(school_id, query_embedding, k) -> list[(Document, score)]`
- `neighbors(school_id, entity_ids, hops=1) -> list[Neighbor]`
- `get_documents(school_id, doc_ids) -> list[Document]`

모든 조회 메서드는 `school_id`를 필수로 받아 학교 간 데이터 혼합을 막는다.

## MVP 병합 정책

- 문서: `school_id + source_url + content_hash + chunk_index`이 같으면 같은 청크다.
- 엔티티: `(school_id, norm_key)`가 같으면 같은 엔티티다. 새 속성 값이 기존 같은 키를 갱신하고, 근거 문서 ID는 누적한다.
- 엣지: 양 끝 엔티티가 같은 학교 소속일 때만 저장한다. `school_id + source_entity_id + target_entity_id + relation`이 같으면 하나로 병합하고 근거 문서 ID를 누적한다.
- 자동 만료, 속성 이력, 실패 작업 재처리 큐는 Scheduler·운영 정책 단계에서 추가한다.
- 현재 MVP는 각 테이블의 유니크 키로 멱등성을 보장한다. 실행 이력과 요청 ID 추적은 후속 작업이다.
