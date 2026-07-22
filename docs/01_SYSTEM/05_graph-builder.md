# 그래프 빌더

추출기 산출물(청크·엔티티·관계)을 받아 **정규화 → 임베딩 → 저장**한다.
지식그래프와 벡터 인덱스를 실제로 만드는 단계.

> **결정**
> - 엔티티 해소(중복 병합)는 **`(school_id, norm_key)` 정규화 키**로 단순 처리. 임베딩 기반 클러스터링은 안 함(YAGNI).
> - 임베딩은 **로컬 bge-m3**([`08_llm-provider.md`](08_llm-provider.md)).
> - 증분: **본문 해시 비교**로 변경 문서만 재처리.

## 1. 파이프라인

```
extracted(청크, 엔티티, 관계)
   │
   ├─ 1) 청크 임베딩       embed(chunk_text) → vector(1024)
   ├─ 2) 문서 upsert       upsert_document(... embedding ...)  → doc_id
   ├─ 3) 엔티티 정규화     name → norm_key, 기존 엔티티와 병합
   ├─ 4) 엔티티 upsert     upsert_entity(...)                  → entity_id
   └─ 5) 엣지 upsert       이름 참조를 entity_id로 해소 후 저장
```

모든 저장은 [`06_storage.md`](06_storage.md)의 `upsert_*` 인터페이스로만 한다.

## 2. 엔티티 정규화(해소)

- `norm_key = normalize(type, name)`: 공백·특수문자 제거, 소문자화, 동의어 사전(선택) 적용.
- `(school_id, norm_key)`가 같으면 **같은 엔티티**로 보고 `attributes`·`source_doc_ids`를 병합.
- 관계의 `source`/`target`는 이름 문자열 → 같은 `norm_key`로 매핑해 `entity_id`로 해소.
  매칭 실패 시 해당 엣지는 스킵하고 로그로 남긴다.

## 3. 증분 갱신

- 문서마다 `content_hash`를 계산해 `doc_hash_exists(school_id, source_url, content_hash)` 확인.
- 해시가 같으면 **처리 건너뜀**(임베딩·추출 재실행 안 함 → 비용 절감).
- 바뀐 문서만 위 파이프라인을 다시 태운다. 삭제 감지·만료는 스케줄러 몫
  ([`09_scheduler.md`](09_scheduler.md)).

## 4. 공개 인터페이스

```
build(school_id, doc_id, chunk_text, extracted) -> { entity_ids, edge_ids }
```

- `extracted`는 추출기의 `extract()` 반환값([`04_extractor.md`](04_extractor.md)).
- 근거 추적: 이 문서에서 나온 모든 엔티티·엣지의 `source_doc_ids`에 `doc_id`를 넣는다.

관련: [`04_extractor.md`](04_extractor.md), [`06_storage.md`](06_storage.md), [`10_data-model.md`](10_data-model.md).
