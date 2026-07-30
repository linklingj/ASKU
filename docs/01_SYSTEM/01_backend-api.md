# 백엔드 API (FastAPI)

프론트엔드와 내부 시스템을 잇는 **유일한 진입점**. 학교 등록·검색·질의·
크롤링 상태 조회를 REST 엔드포인트로 제공하고, 내부 모듈(Crawler·Graph RAG
Engine·Storage)에 작업을 위임한다. 비즈니스 로직을 직접 구현하지 않는다.

> **결정**
> - 프레임워크: **FastAPI** (비동기, 자동 OpenAPI 문서).
> - 인증·인가: MVP에서는 **없음**. 필요 시 API 키 또는 세션 방식을 추가한다.
> - 크롤링·인덱싱은 **비동기 작업**으로 처리한다. 등록 요청은 즉시 응답하고
>   진행 상태는 별도 엔드포인트로 폴링한다.

## 1. 책임 범위

### 하는 일

- 외부 요청(프론트엔드)을 받아 입력을 검증하고, 적절한 내부 모듈을 호출한다.
- 요청·응답 스키마를 정의하고 직렬화한다.
- 크롤링·인덱싱 같은 장시간 작업을 비동기로 트리거하고 상태를 추적한다.
- 에러를 일관된 형식으로 변환해 반환한다.

### 하지 않는 일

- HTML 수집·파싱 (→ Crawler)
- 엔티티·관계 추출 (→ Extractor)
- 그래프 구성·임베딩 (→ Graph Builder)
- 컨텍스트 조립·답변 생성 (→ Graph RAG Engine)
- DB 스키마 관리·직접 SQL 실행 (→ Storage)
- 재크롤링 주기 관리 (→ Scheduler)

## 2. 엔드포인트

### 2.1 학교 등록

```
POST /schools
```

새 학교를 등록하고 초기 크롤링을 비동기로 시작한다.

**요청**

```json
{
  "name": "연세대학교",
  "base_url": "https://www.yonsei.ac.kr/sc/254/subview.do",
  "crawl_schedule": "weekly"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| name | string | ✅ | 학교명 |
| base_url | string | ✅ | 공지·학사 기준 URL |
| crawl_schedule | string | — | 재크롤링 주기 (`daily`, `weekly` 등). 미지정 시 기본값 적용 |

**응답 `201 Created`**

```json
{
  "school_id": 1,
  "name": "연세대학교",
  "base_url": "https://www.yonsei.ac.kr/sc/254/subview.do",
  "crawl_schedule": "weekly",
  "status": "crawling",
  "created_at": "2026-07-24T12:00:00Z"
}
```

등록과 동시에 Crawler에 `CrawlRequest`(`mode: "initial"`)를 전달한다.
크롤링이 완료될 때까지 `status`는 `crawling` → `indexing` → `ready` 순으로 전이한다.

---

### 2.2 학교 검색

```
GET /schools?query={검색어}
```

등록된 학교를 이름으로 검색한다.

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| query | string | — | 학교명 검색어. 생략 시 전체 목록 반환 |

**응답 `200 OK`**

```json
{
  "schools": [
    {
      "school_id": 1,
      "name": "연세대학교",
      "status": "ready",
      "updated_at": "2026-07-24T12:30:00Z"
    }
  ]
}
```

---

### 2.3 학교 상세

```
GET /schools/{school_id}
```

학교의 상세 정보와 현재 상태를 반환한다.

**응답 `200 OK`**

```json
{
  "school_id": 1,
  "name": "연세대학교",
  "base_url": "https://www.yonsei.ac.kr/sc/254/subview.do",
  "crawl_schedule": "weekly",
  "status": "ready",
  "stats": {
    "document_count": 245,
    "entity_count": 1023,
    "last_crawled_at": "2026-07-24T12:30:00Z"
  },
  "created_at": "2026-07-24T12:00:00Z",
  "updated_at": "2026-07-24T12:30:00Z"
}
```

---

### 2.4 질문 (RAG 답변)

```
POST /schools/{school_id}/query
```

선택한 학교의 지식그래프를 기반으로 질문에 답변한다.

**요청**

```json
{
  "question": "성적우수 장학금 마감일이 언제야?"
}
```

**응답 `200 OK`**

```json
{
  "answer": "성적우수 장학금 마감일은 2026년 3월 15일입니다.",
  "sources": [
    {
      "title": "2026학년도 2학기 성적우수 장학금 안내",
      "url": "https://www.yonsei.ac.kr/..."
    }
  ]
}
```

내부적으로 Graph RAG Engine의 `answer(school_id, question)`을 호출한다
([`07_graph-rag-engine.md`](07_graph-rag-engine.md)).
근거 청크가 없거나 유사도가 임계 미만이면 `answer`에 정보 부재 메시지를 반환한다.

---

### 2.5 수동 재크롤링

```
POST /schools/{school_id}/recrawl
```

해당 학교의 크롤링을 수동으로 다시 실행한다.

**응답 `202 Accepted`**

```json
{
  "school_id": 1,
  "status": "crawling",
  "message": "재크롤링이 시작되었습니다."
}
```

Crawler에 `CrawlRequest`(`mode: "recrawl"`)를 전달한다.
이미 크롤링 중이면 `409 Conflict`를 반환한다.

---

### 2.6 크롤링·인덱싱 상태

```
GET /schools/{school_id}/status
```

현재 크롤링·인덱싱 진행 상태를 반환한다.

**응답 `200 OK`**

```json
{
  "school_id": 1,
  "status": "indexing",
  "stage": "indexing",
  "progress": 0.7,
  "detail": {
    "pages": 150,
    "chunks": 150,
    "entities": 120,
    "edges": 45
  },
  "message": "정보 추출 및 지식그래프 구축 중입니다.",
  "started_at": "2026-07-24T12:00:00Z"
}
```

| status / stage 값 | 의미 |
|---|---|
| `idle` | 작업 없음 |
| `crawling` | 페이지 수집 중 |
| `indexing` | 추출·임베딩·그래프 구축 중 |
| `ready` | 질의 가능 |
| `partial_failed` | 일부 실패, 질의는 가능 |
| `failed` | 전체 실패 |

### 2.7 코어 서브그래프 (프론트 소비용 확장)

```
GET /schools/{school_id}/graph
```

차수(degree) 상위 엔티티와 그 사이의 엣지를 반환한다. QA 화면의 네트워크 그래프 초기 로드용.

**응답 `200 OK`**

```json
{
  "nodes": [
    { "id": "e_123", "type": "장학금", "name": "국가장학금", "degree": 5, "doc_count": 3 }
  ],
  "edges": [
    { "source": "e_123", "target": "e_45", "relation": "담당" }
  ]
}
```

---

### 2.8 엔티티 상세 (프론트 소비용 확장)

```
GET /schools/{school_id}/entities/{entity_id}
```

노드 상세·이웃·근거 문서를 반환한다. QA 화면의 노드 선택 패널용.

**응답 `200 OK`**

```json
{
  "id": "e_123", "type": "장학금", "name": "국가장학금",
  "attributes": { "마감일": "3/15", "금액": "300만원" },
  "sources": [ { "title": "2026 교내 장학금 안내", "url": "https://…" } ],
  "neighbors": [ { "id": "e_45", "name": "학생지원팀", "relation": "담당" } ]
}
```

## 3. 학교 상태 전이

```
등록 요청 → crawling → indexing → ready
                 │          │
                 └→ failed   └→ partial_failed
                                     │
재크롤링 → crawling → ...           ready (질의 가능)
```

- `crawling`: Crawler가 페이지를 수집 중.
- `indexing`: Extractor → Graph Builder 파이프라인이 동작 중.
- `ready`: 모든 처리 완료. 질의 가능.
- `partial_failed`: 일부 페이지 실패했으나 성공분으로 질의 가능.
- `failed`: 전체 파이프라인 실패. 재크롤링 필요.

## 4. 공통 에러 응답

모든 에러는 아래 형식으로 반환한다.

```json
{
  "error": {
    "code": "SCHOOL_NOT_FOUND",
    "message": "해당 학교를 찾을 수 없습니다.",
    "details": null
  }
}
```

| HTTP 상태 | 에러 코드 | 설명 |
|---|---|---|
| 400 | `INVALID_REQUEST` | 요청 본문 검증 실패 (필수 필드 누락 등) |
| 404 | `SCHOOL_NOT_FOUND` | 존재하지 않는 `school_id` |
| 409 | `CRAWL_IN_PROGRESS` | 이미 크롤링 진행 중인데 재크롤링 요청 |
| 422 | `INVALID_URL` | `base_url` 형식이 올바르지 않음 |
| 500 | `INTERNAL_ERROR` | 서버 내부 오류 |
| 503 | `SCHOOL_NOT_READY` | 학교 데이터가 아직 준비되지 않음 (크롤링·인덱싱 중 질의 시) |

## 5. 다른 시스템과의 연동

| 시스템 | 방향 | 설명 |
|---|---|---|
| Frontend | 이전 | REST 요청을 받는다. |
| Crawler | 다음 | 학교 등록·수동 재크롤링 시 `CrawlRequest`를 생성·전달한다 ([`03_crawler.md`](03_crawler.md)). |
| Graph RAG Engine | 다음 | 질의 시 `answer(school_id, question)`을 호출한다 ([`07_graph-rag-engine.md`](07_graph-rag-engine.md)). |
| Storage | 양방향 | 학교 CRUD, 상태 조회·갱신을 공개 인터페이스로 요청한다 ([`06_storage.md`](06_storage.md)). |
| Scheduler | 간접 | Scheduler가 주기적으로 재크롤링 `CrawlRequest`를 만들 때 같은 경로를 탄다 ([`09_scheduler.md`](09_scheduler.md)). |

## 6. 비동기 처리

크롤링·인덱싱은 수십 초~수 분이 걸리므로 동기 응답으로 처리하지 않는다.

1. `POST /schools` 또는 `POST /schools/{id}/recrawl` 요청을 받는다.
2. `CrawlRequest`를 생성해 Crawler에 전달하고, 학교 상태를 `crawling`으로 갱신한다.
3. 즉시 `201` 또는 `202` 응답을 반환한다.
4. 프론트엔드는 `GET /schools/{id}/status`를 폴링해 진행 상태를 확인한다.
5. 파이프라인이 끝나면 상태가 `ready` 또는 `failed`로 전이된다.

> Crawler → Extractor → Graph Builder 파이프라인의 각 단계 호출이 동기 API인지
> 작업 큐인지는 **미정**이다 ([`03_crawler.md`](03_crawler.md) §5).
> 어느 방식이든 Backend API는 상태 전이만 추적한다.

## 7. 미정 사항

- 인증·인가 방식 (API 키, OAuth, 세션 등)
- 요청 속도 제한 (rate limiting) 정책
- 페이지네이션 방식 (`GET /schools` 목록이 많아질 때)
- 비동기 작업의 구현 방식 (백그라운드 태스크 vs 작업 큐)
- 상태 폴링 외 실시간 알림 (WebSocket·SSE) 도입 여부
- 채팅 이력 저장 및 대화 맥락 유지 여부
- CORS 허용 범위
