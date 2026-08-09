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
  "crawl_quality": {
    "status": "warning",
    "checked_at": "2026-07-24T12:30:00Z",
    "boards": [
      {
        "board": "일반공지",
        "listing_rows": 16,
        "title_ratio": 1.0,
        "date_ratio": 1.0,
        "checked_details": 3,
        "findings": []
      },
      {
        "board": "장학",
        "listing_rows": 0,
        "title_ratio": 0.0,
        "date_ratio": 0.0,
        "checked_details": 0,
        "findings": [
          {"code": "NO_LISTING_ROWS", "detail": "목록에서 공지를 한 줄도 읽지 못했다"}
        ]
      }
    ]
  },
  "created_at": "2026-07-24T12:00:00Z",
  "updated_at": "2026-07-24T12:30:00Z"
}
```

`crawl_quality`는 마지막 크롤의 수집 품질이다. `status`는 `unknown`(아직 판정 없음) · `ok` · `warning` 중 하나이며, 게시판 하나라도 문제가 있으면 `warning`이다.

파서가 사이트 구조와 어긋나도 크롤은 0건 성공으로 끝나 `status: "ready"`가 되므로, 상태값만으로는 정상과 구분되지 않는다. 이 섹션이 그 차이를 드러낸다. 판정 항목은 [`03_crawler.md`](03_crawler.md) §6-2에 정리돼 있다.

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
  ],
  "entity_ids": ["e_123"],
  "source_type": "graph"
}
```

내부적으로 RAG Engine의 `answer(school_id, question)`을 호출한다
([`07_graph-rag-engine.md`](07_graph-rag-engine.md)). 엔진은 **그래프 RAG → 문서 RAG**
순으로 2단 검색하며, `source_type`이 어느 단계가 답을 냈는지 알린다.

| `source_type` | 뜻 | `sources` |
|---|---|---|
| `"graph"` | 크롤링 공지 + 그래프 확장으로 답함 | 공지 제목·원문 URL |
| `"document"` | 업로드 첨부(수강편람 등)로 답함 | `"수강편람.pdf - 12페이지"` · `attachment://7` |
| `null` | 두 단계 모두 근거를 못 찾아 보류 | `[]` |

근거 청크가 없거나 유사도가 임계 미만이면 `answer`에 정보 부재 메시지를 반환한다.

학교가 아직 `ready`가 아니어도 **색인이 끝난 첨부가 하나라도 있으면** 질의를 받는다.
크롤링이 실패한 학교라도 올려둔 문서로는 답할 수 있어야 하기 때문이다.

---

### 2.4-2 근거 검색 (사용자 모델 경로)

```
POST /schools/{school_id}/retrieve
```

검색만 하고 **답변은 만들지 않는다**. 사용자가 자기 Gemini 키나 자기 PC의 Ollama를
답변 모델로 고른 경우, 프론트가 이 응답으로 브라우저에서 직접 모델을 부른다.

요청 본문은 `/query`와 같다.

**응답 `200 OK`**

```json
{
  "context": "[근거 1] 2026학년도 성적우수 장학금 안내\n출처: https://...\n본문…",
  "instruction": "…근거 기반 답변 지시문…\n\n[질문]\n성적우수 장학금 마감일이 언제야?",
  "sources": [{ "title": "2026학년도 …", "url": "https://www.yonsei.ac.kr/..." }],
  "entity_ids": ["e_123"],
  "source_type": "graph",
  "no_evidence_answer": "해당 정보를 찾지 못했습니다."
}
```

`context`·`sources`·`source_type`의 뜻은 `/query`와 같고, 2단 검색 순서도 같다
(RAG Engine의 `retrieve(school_id, question)`). `instruction`은 서버가 답변할 때 쓰는
프롬프트 그대로라, 모델을 누가 부르든 같은 문안·같은 근거로 답한다.
`source_type`이 `null`이면 프론트는 모델을 부르지 않고 `no_evidence_answer`를 그대로
보여준다 — 근거 없이 생성하지 않는 규칙(환각 방지)이 경로와 무관하게 유지된다.

이 경로에서 서버는 **답변 생성을 하지 않으며 사용자 API 키를 받지도, 저장하지도
않는다.** 다만 검색 단계의 질문 엔티티 추출은 `/query`와 동일하게 서버 모델이 맡는다 —
그래프 확장 결과가 사용자의 모델 선택에 따라 달라지면 안 되기 때문이다.

---

### 2.4-1 첨부 문서 업로드

```
POST   /schools/{school_id}/attachments        # multipart/form-data, 필드명 files (복수 가능)
GET    /schools/{school_id}/attachments
DELETE /schools/{school_id}/attachments/{attachment_id}
```

수강편람 PDF·학칙 HWP 처럼 웹에서 크롤링되지 않는 문서를 사용자가 직접 올린다.
**학교 등록 직후에도, 이미 등록된 학교에도** 같은 경로로 올린다. 크롤러는 이 경로에
관여하지 않는다 — 첨부 본문 파싱은 여전히 크롤러 범위 밖이다([`03_crawler.md`](03_crawler.md)).

지원 형식: `.pdf` · `.hwp` · `.hwpx` · `.txt` · `.md`. 파일당 최대 50MB.

**응답 `202 Accepted`** — 파싱·임베딩은 백그라운드에서 이어진다.

```json
{
  "accepted": [
    {
      "attachment_id": 7,
      "filename": "2026_수강편람.pdf",
      "content_type": "application/pdf",
      "byte_size": 4823910,
      "page_count": 0,
      "chunk_count": 0,
      "status": "pending",
      "error_code": null,
      "uploaded_at": "2026-08-05T09:00:00Z"
    }
  ],
  "rejected": [
    { "filename": "캠퍼스맵.png", "code": "UNSUPPORTED_FILE_TYPE", "message": "지원하지 않는 파일 형식입니다. ..." }
  ]
}
```

- 검증에 걸린 파일은 `rejected`로 돌려주고 **나머지는 계속 처리한다** — 한 파일 때문에
  업로드 전체가 실패하지 않는다. 처리 가능한 파일이 하나도 없으면 `415 NO_SUPPORTED_ATTACHMENT`.
- 같은 학교에 같은 파일을 다시 올리면 새 첨부가 생기지 않고 기존 첨부를 다시 색인한다
  (파일 바이트 해시 기준, [`06_storage.md`](06_storage.md)).
- 색인 진행은 `GET .../attachments` 의 `status`(`pending` → `indexing` → `ready`/`failed`)로
  확인한다. 실패 시 `error_code`(예: `HWP_ENCRYPTED`, `EMPTY_CONTENT`)가 사유를 알린다.
  스캔 이미지만 있어 텍스트 계층이 없는 PDF 는 `EMPTY_CONTENT`로 실패한다(OCR 미지원).
- `DELETE`는 첨부와 그 청크를 함께 지운다(`204 No Content`). 지운 뒤에는 문서 RAG 검색
  대상에서 빠진다.

---

### 2.5 수동 재크롤링

```
POST /schools/{school_id}/recrawl?max_nodes=1200
```

해당 학교의 크롤링을 수동으로 다시 실행한다.

| 쿼리 | 기본값 | 설명 |
|---|---|---|
| `max_nodes` | `MAX_GRAPH_NODES` 환경변수(기본 `1200`) | 이 학교 그래프의 노드(엔티티) 상한. `1` 이상 |

**응답 `202 Accepted`**

```json
{
  "school_id": 1,
  "status": "crawling",
  "message": "재크롤링이 시작되었습니다."
}
```

Crawler에 `CrawlRequest`(`mode: "recrawl"`)를 전달한다.
이미 크롤링 중이면 `409 Conflict`를, `max_nodes` 가 1 미만이면 `400 INVALID_REQUEST` 를 반환한다.

**크롤은 한 번에 하나만 돈다.** `409` 는 *같은 학교*의 중복만 막으므로, 서로 다른 학교에 연달아
요청하면 모두 `202` 로 접수되지만 실제 실행은 전역 락으로 직렬화된다. 크롤마다 bge-m3 임베더를
새로 만들기 때문에 동시에 돌면 모델이 그 수만큼 메모리에 겹쳐 뜨기 때문이다(11개 학교를 한꺼번에
돌려 24GB 를 넘긴 적이 있다). 차례를 기다리는 학교는 `GET /status` 의 `message` 가
"다른 학교 크롤이 끝나기를 기다리는 중입니다."로 표시돼, 끼어버린 크롤과 구분된다.
접수된 크롤은 FastAPI 스레드풀이 아니라 전용 워커 1개에서 실행된다 — 대기 중인 크롤이
동기 엔드포인트용 스레드를 붙잡아 API 전체가 멎는 것을 막기 위해서다.

**노드 상한** — 노드가 늘수록 그래프 조회·검색이 쓰는 메모리가 커지므로 학교당 총 노드 수를 제한한다.
인덱싱 중 청크를 하나 반영할 때마다 현재 노드 수를 세고, 상한에 닿으면 **남은 페이지를 더 추출하지 않고
멈춘다**(LLM 호출도 함께 아낀다). 상한 도달은 실패가 아니므로 상태는 `ready` 로 끝나고,
`GET /status` 의 `message` 로 중단 사실을 알린다. 노드 수는 build 결과를 더하지 않고 DB 로 세는데,
같은 엔티티가 여러 청크에 나오면 upsert 로 합쳐져 실제보다 부풀려지기 때문이다.

`POST /schools`(최초 등록)와 스케줄러 재크롤은 상한을 따로 받지 않고 기본값을 쓴다.

---

### 2.5-1 관리용 상태 리셋

```
POST /schools/{school_id}/reset-status
```

크롤링·인덱싱 도중 컨테이너가 죽으면(재배포·OOM 등) `crawling`/`indexing` 상태가 영구히 남아
이후 모든 `recrawl` 이 `409 CRAWL_IN_PROGRESS` 로 막힌다. 자동 판별 없이, 운영자가
`GET /schools/{id}/status` 로 오래 멈춰 있음을(`started_at` 이 크롤 예산 시간을 훨씬 넘김)
직접 확인한 뒤 호출하는 수동 복구 경로다.

**응답 `200 OK`**

```json
{
  "school_id": 1,
  "status": "failed",
  "message": "상태를 failed 로 되돌렸습니다. 다시 재크롤링을 시작할 수 있습니다."
}
```

현재 상태가 `crawling`/`indexing` 이 아니면(이미 정상 종료됐거나 애초에 끼지 않음) `409 NOT_STUCK` 을
반환하고 아무것도 바꾸지 않는다. 리셋 후 상태는 `failed` 로 남고, `POST /recrawl` 로 다시 시작한다.

> 인증 없음(MVP 전역 정책과 동일). 어떤 상태여도 강제로 되돌리는 관리용 엔드포인트이므로 공개 배포에서는
> 신뢰 경계를 넘기 전에 호출 주체를 제한하는 편이 좋다.

---

### 2.5-2 관리용 강제 완료

```
POST /schools/{school_id}/force-complete
```

`indexing`(끼었거나 오래 걸림) 또는 `partial_failed`(일부만 실패) 상태를 실제로 색인을
다시 돌리지 않고 `ready`(완료)로 강제 전이한다. 지금까지 쌓인 데이터로도 질의하기에
충분하다고 운영자가 판단했을 때 쓰는 수동 경로다.

**응답 `200 OK`**

```json
{
  "school_id": 1,
  "status": "ready",
  "message": "상태를 ready 로 강제 완료 처리했습니다. 지금까지 색인된 데이터로 질의할 수 있습니다."
}
```

현재 상태가 `indexing`/`partial_failed` 가 아니면 `409 NOT_ELIGIBLE` 을 반환하고 아무것도
바꾸지 않는다. `crawling`(아직 색인된 것이 없음)·`failed`(색인 결과 없음)·`ready`(이미 완료)에서
허용하면 데이터 없이 완료로 속일 수 있어 막는다.

> 인증 없음(§2.5-1 과 동일). 상태값만 바꿀 뿐 실제 데이터 완결성을 보장하지 않으므로,
> 남은 미색인 페이지가 있다는 것을 인지한 상태에서 호출해야 한다.

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
| 415 | `NO_SUPPORTED_ATTACHMENT` | 업로드에 처리 가능한 첨부가 하나도 없음 (`details.rejected`에 사유) |
| 404 | `ATTACHMENT_NOT_FOUND` | 삭제·조회 대상 첨부가 없음 |
| 500 | `INTERNAL_ERROR` | 서버 내부 오류 |
| 503 | `SCHOOL_NOT_READY` | 학교 데이터가 아직 준비되지 않음 (크롤링·인덱싱 중이고 색인된 첨부도 없을 때) |

## 5. 다른 시스템과의 연동

| 시스템 | 방향 | 설명 |
|---|---|---|
| Frontend | 이전 | REST 요청을 받는다. |
| Crawler | 다음 | 학교 등록·수동 재크롤링 시 `CrawlRequest`를 생성·전달한다 ([`03_crawler.md`](03_crawler.md)). |
| RAG Engine | 다음 | 질의 시 `HybridRAG.answer(school_id, question)`을 호출한다. 그래프→문서 단계 선택은 엔진 책임이다 ([`07_graph-rag-engine.md`](07_graph-rag-engine.md)). |
| 첨부 인제스터 | 다음 | 업로드 첨부를 백그라운드에서 파싱·청킹·임베딩해 `documents(source_type='attachment')`로 저장한다. |
| Storage | 양방향 | 학교 CRUD, 상태 조회·갱신을 공개 인터페이스로 요청한다 ([`06_storage.md`](06_storage.md)). |
| Scheduler | 간접 | Scheduler가 주기적으로 재크롤링 `CrawlRequest`를 만들 때 같은 경로를 탄다 ([`09_scheduler.md`](09_scheduler.md)). |

## 6. 비동기 처리

크롤링·인덱싱은 수십 초~수 분이 걸리므로 동기 응답으로 처리하지 않는다.

1. `POST /schools` 또는 `POST /schools/{id}/recrawl` 요청을 받는다.
2. `CrawlRequest`를 생성해 Crawler에 전달하고, 학교 상태를 `crawling`으로 갱신한다.
3. 즉시 `201` 또는 `202` 응답을 반환한다.
4. 프론트엔드는 `GET /schools/{id}/status`를 폴링해 진행 상태를 확인한다.
5. 파이프라인이 끝나면 상태가 `ready` 또는 `failed`로 전이된다.

> **실행 모델 및 진행도 추적 (MVP)**
> - 크롤링·인덱싱 비동기 작업은 FastAPI의 `BackgroundTasks`로 단일 프로세스 백그라운드 태스크로 실행된다.
> - 작업 중 실시간 세부 진행도(`pages`, `chunks`, `entities`, `edges`, `stage`, `progress`)는 메모리 진행도 맵(`_PROGRESS_MAP`)에서 추적되며, `GET /schools/{id}/status`에서 반환된다.
> - 서버 재시작 시에는 Storage의 DB 영속 상태(`status`, `crawl_started_at`) 및 집계 수치로 안전하게 폴백한다.
> - 추후 다중 워커/분산 처리 전환 시 Celery / Redis / DB 작업 큐 기반 저장소로 이관한다 ([`03_crawler.md`](03_crawler.md) §5).

## 7. 미정 사항

- 인증·인가 방식 (API 키, OAuth, 세션 등)
- 요청 속도 제한 (rate limiting) 정책
- 페이지네이션 방식 (`GET /schools` 목록이 많아질 때)
- 비동기 작업의 구현 방식 (백그라운드 태스크 vs 작업 큐)
- 상태 폴링 외 실시간 알림 (WebSocket·SSE) 도입 여부
- 채팅 이력 저장 및 대화 맥락 유지 여부
- CORS 허용 범위
