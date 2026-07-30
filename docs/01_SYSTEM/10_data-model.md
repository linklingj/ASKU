# 데이터 모델

시스템이 다루는 핵심 엔터티와 관계. 물리 스키마(DDL)는 [`06_storage.md`](06_storage.md),
여기서는 **개념 모델과 불변식**을 정의한다.

## 1. 엔터티

### School
| 필드 | 설명 |
|---|---|
| school_id | 고유 ID (PK) |
| name | 학교명 |
| base_url | 공지·학사 기준 URL |
| crawl_schedule | 재크롤링 주기 |
| status | 수집·인덱싱 진행 상태 (`idle`, `crawling`, `indexing`, `ready`, `partial_failed`, `failed`) |
| crawl_started_at | 최근 크롤링·인덱싱 시작 시각 |
| created_at / updated_at | 생성·갱신 시각 |

### Document (= 검색·인용의 최소 단위 / 청크)
| 필드 | 설명 |
|---|---|
| doc_id | 고유 ID (PK) |
| school_id | 소속 학교 |
| source_url | 원문 링크 (근거 제공용) |
| title / content | 제목 · 청크 본문 |
| chunk_index | 같은 원문 내 순번 (공지 1건=보통 0) |
| content_hash | 변경 감지용 본문 해시 |
| embedding | vector(1024) |
| crawled_at | 크롤링 시각 |
| miss_count | 연속 미관측 횟수 (Scheduler 만료 판정) |
| expired_at | 만료 확정 시각 (`NULL`이면 유효) |

> 공지 1건 = 기본 Document 1행. 긴 문서만 `source_url`을 공유하는 여러 행으로 분할.

### Entity (그래프 노드)
| 필드 | 설명 |
|---|---|
| entity_id | 고유 ID (PK) |
| school_id | 소속 학교 |
| type | 엔티티 타입 (전체 화이트리스트 → [`04_extractor.md`](04_extractor.md) §2.2) |
| name | 표시 이름 |
| norm_key | 정규화 키 — `(school_id, norm_key)` 유니크(중복 병합 기준) |
| attributes | 자유 속성(JSON) |
| source_doc_ids | 근거 문서 목록 |

### Edge (그래프 관계)
| 필드 | 설명 |
|---|---|
| edge_id | 고유 ID (PK) |
| school_id | 소속 학교 |
| source_entity_id / target_entity_id | 양 끝 노드 |
| relation | 관계 타입 (전체 화이트리스트 → [`04_extractor.md`](04_extractor.md) §2.3) |
| source_doc_ids | 근거 문서 목록 |

## 2. 관계 예시

- `공지` —안내→ `장학금`
- `장학금` —담당→ `부서`
- `담당자` —연락처→ `전화/이메일`
- `절차` —선행조건→ `절차`

> `마감일`·`금액` 등은 관계가 아니라 엔티티 `attributes`에 담는다([`04_extractor.md`](04_extractor.md) §2.1).

## 3. 불변식

- **근거 추적성**: 모든 Entity·Edge·Document는 `source_doc_ids`/`source_url`로 원문에 닿는다. 근거 없는 노드는 만들지 않는다.
- **학교 격리**: 모든 조회는 `school_id`로 필터한다. 학교 간 참조(엣지)는 없다.
- **엔티티 유일성**: 학교 내에서 `norm_key`가 같으면 같은 엔티티. 빌더가 병합한다([`05_graph-builder.md`](05_graph-builder.md)).
- **임베딩 차원 일치**: `Document.embedding` 차원 = 임베딩 모델 출력 차원(현재 1024). 모델 교체 시 재임베딩 필요.

관련: [`06_storage.md`](06_storage.md), [`05_graph-builder.md`](05_graph-builder.md), [`07_graph-rag-engine.md`](07_graph-rag-engine.md).
