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

- 페이지 단위 `content_hash` 비교와 `new`·`changed`·`unchanged` 분류는 **Crawler**가 Storage의 조회 인터페이스로 수행한다.
- Graph Builder는 Crawler가 전달한 신규·변경 청크를 모두 처리하고, Storage의 `school_id + source_url + content_hash + chunk_index` upsert를 최종 멱등 방어로 사용한다. 긴 문서의 여러 청크는 같은 페이지 해시를 공유하므로 Builder가 해시만으로 청크를 건너뛰지 않는다.
- 따라서 같은 해시는 Crawler 단계에서 **처리 건너뜀**(크롤링 결과 재추출·재임베딩 방지)되고, 바뀐 문서만 위 파이프라인을 다시 탄다. 삭제 감지·만료는 스케줄러 몫
  ([`09_scheduler.md`](09_scheduler.md)).

## 4. 공개 인터페이스

```
build(school_id, extracted_chunk) -> { doc_id, entity_ids, edge_ids }
```

- `extracted_chunk`는 Extractor가 만든 청크 본문·메타데이터와 `extract()` 반환값을 함께 담는다([`04_extractor.md`](04_extractor.md)).
- Builder는 `upsert_document`가 반환한 `doc_id`를 사용해 엔티티·엣지를 저장한다. 따라서 `doc_id`는 입력이 아니라 결과에 포함된다.
- 근거 추적: 이 문서에서 나온 모든 엔티티·엣지의 `source_doc_ids`에 생성된 `doc_id`를 넣는다.

관련: [`04_extractor.md`](04_extractor.md), [`06_storage.md`](06_storage.md), [`10_data-model.md`](10_data-model.md).

## 5. 책임 범위와 입출력

Graph Builder는 Extractor의 이름 기반 엔티티·관계를 Storage의 `documents`, `entities`, `edges` 레코드로 바꾼다. 문서 해시의 최초 계산·삭제 감지·재크롤링 일정·질의 답변은 담당하지 않는다.

### 입력: `ExtractedChunk`

Extractor §4의 `school_id`, `source_url`, `title`, `content`, `chunk_index`, `content_hash`, `crawled_at`, `entities`, `relations`, `extraction_status`를 받는다. `complete`와 `partial`은 처리하되, 경고가 있는 항목의 저장 기준은 현재 화이트리스트 검증 결과에 따른다.

### 출력: `BuildResult`

```json
{
  "school_id": 1,
  "source_url": "https://...",
  "content_hash": "sha256",
  "doc_id": 101,
  "entity_ids": [12, 13],
  "edge_ids": [33],
  "status": "complete | partial | failed",
  "warnings": []
}
```

성공 시 `school_id`, `source_url`, `content_hash`, `doc_id`, `status`가 필수다. 임베딩·엔티티·엣지 일부만 실패하면 성공한 ID와 `warnings`를 남긴 `partial` 결과를 반환한다. 입력 스키마 오류 또는 저장 불가 오류는 `error_code`, `retryable`, `warnings`가 포함된 실패 결과로 기록한다.

## 6. 연동과 오류 처리

| 대상 | 방향 | 계약 |
|---|---|---|
| Extractor | 이전 | `ExtractedChunk`를 받는다. |
| LLM Provider | 의존 | 로컬 bge-m3의 `embed(text) -> vector(1024)`를 호출한다. |
| Storage | 다음 | `upsert_document`, `upsert_entity`, `upsert_edge`만 호출한다. |
| Scheduler | 간접 | 만료가 확정된 문서를 재처리·만료 대상으로 받는다. |

- `school_id + source_url + content_hash + chunk_index`이 같은 청크는 Storage upsert가 멱등 처리한다. Builder의 입력 중복은 새 그래프 데이터를 만들지 않는다.
- 관계의 양 끝을 `norm_key`로 해소하지 못하면 해당 엣지만 건너뛰고 로그·경고를 남긴다.
- 임베딩 오류는 제한 재시도 대상이며, 모델·저장소의 부분 실패에서는 성공한 문서·엔티티와 실패한 벡터/엣지를 구분해 기록한다. 재시도 횟수와 보상 작업은 **미정**이다.
- 새 버전은 upsert로 반영한다. 삭제·만료의 최종 판정은 Scheduler가 하며, Builder는 확정된 만료 요청에 대해서만 기존 기여분을 비활성화 또는 삭제한다. 실제 보존 방식은 Storage의 미정 사항이다.
- `graph_builder_version`을 포함한 생성 이력·병합 판단을 남기는 방식을 권장하지만, 버전 저장 스키마는 아직 확정하지 않았다.

## 7. 확장 가능성과 미정 사항

- `school_id`와 `(school_id, norm_key)`를 모든 병합·조회 경계에 적용해 멀티학교 오염을 막는다.
- 새 엔티티·관계는 Extractor의 화이트리스트를 먼저 확장한 뒤 Builder가 그대로 수용한다.
- 정규화 사전과 임베딩 제공자는 교체 가능하다. 임베딩 기반 클러스터링·multi-hop/community 그래프는 현재 확정 범위가 아니다.
- 구현 전 팀은 동의어 사전의 소유·검토 방식, `partial` 결과의 최소 저장 기준, 문서·그래프 만료의 물리 삭제 여부, 재처리 큐와 생성 이력 보존 기간을 정해야 한다.
